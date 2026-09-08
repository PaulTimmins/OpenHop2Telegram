"""Telegram slash commands for inspecting the mesh.

Read-only by design, apart from the airtime `/telemetry` spends asking a node
for a fresh reading. Commands are only honoured from the configured chat, which
the Telegram client already filters, so anyone who can post there can run them.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("relay.commands")

# Telegram rejects messages over 4096 characters.
_MAX_MESSAGE = 3800

_SPARK = "▁▂▃▄▅▆▇█"

HELP = """Available commands:

/nodes [filter] — known nodes, newest adverts first
/battery [node] [days] — battery trend from the metrics log
/telemetry <node> — ask a node for a live reading
/help — this list"""


def sparkline(values: list[float]) -> str:
    """A one-line shape for a series, for when no plotting library is around."""
    if not values:
        return ""
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return _SPARK[0] * len(values)
    span = high - low
    return "".join(
        _SPARK[min(len(_SPARK) - 1, int((v - low) / span * len(_SPARK)))]
        for v in values
    )


def ago(epoch: Optional[float]) -> str:
    if not epoch:
        return "never"
    delta = int(time.time() - float(epoch))
    if delta < 0:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if delta >= size:
            return f"{delta // size}{unit}"
    return f"{delta}s"


def _clip(text: str) -> str:
    if len(text) <= _MAX_MESSAGE:
        return text
    return text[:_MAX_MESSAGE].rsplit("\n", 1)[0] + "\n… (truncated)"


def read_battery_series(
    path: str | Path, *, node: str = "", days: float = 7.0
) -> dict[str, list[tuple[float, float]]]:
    """Battery readings per node from the metrics CSV, oldest first.

    Rows with a blank battery are skipped rather than plotted as zero — a gap
    means the node didn't answer, not that it went flat.
    """
    cutoff = time.time() - days * 86400
    wanted = node.strip().lower()
    series: dict[str, list[tuple[float, float]]] = {}

    try:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("node") or "").strip()
                if wanted and wanted not in name.lower():
                    continue
                try:
                    when = float(row.get("epoch") or 0)
                    value = float(row.get("battery_mv") or "")
                except (TypeError, ValueError):
                    continue
                if when < cutoff or value <= 0:
                    continue
                series.setdefault(name, []).append((when, value))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return {}

    for points in series.values():
        points.sort()
    return series


def render_png(series: dict[str, list[tuple[float, float]]], days: float) -> Optional[bytes]:
    """A battery chart, or None when no plotting library is installed."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: no display on a Pi
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 - optional extra
        return None

    from datetime import datetime, timezone

    fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
    for name, points in sorted(series.items()):
        times = [datetime.fromtimestamp(t, timezone.utc) for t, _ in points]
        ax.plot(times, [v for _, v in points], marker="o", markersize=3, label=name)

    ax.set_title(f"Battery, last {days:g} day(s)")
    ax.set_ylabel("mV")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def summarise_battery(series: dict[str, list[tuple[float, float]]], days: float) -> str:
    if not series:
        return (
            f"No battery readings in the last {days:g} day(s). The metrics log "
            f"is written by the clock checker, so it fills in once that has run."
        )

    lines = [f"🔋 Battery, last {days:g} day(s)"]
    for name, points in sorted(series.items()):
        values = [v for _, v in points]
        first, last = values[0], values[-1]
        change = last - first
        arrow = "→" if abs(change) < 10 else ("↑" if change > 0 else "↓")
        lines.append(
            f"\n{name}\n"
            f"  {sparkline(values)}\n"
            f"  now {last:.0f}mV  {arrow} {change:+.0f}mV over {len(values)} sample(s)\n"
            f"  min {min(values):.0f}  max {max(values):.0f}"
        )
    return _clip("\n".join(lines))


class CommandRouter:
    """Parses and runs the slash commands."""

    def __init__(
        self,
        *,
        store: Any,
        telegram: Any,
        mesh_getter: Callable[[], Any],
        metrics_path: str,
        request_timeout: float = 30.0,
    ):
        self._store = store
        self._tg = telegram
        self._mesh = mesh_getter
        self._metrics_path = metrics_path
        self._timeout = request_timeout

    async def handle(self, text: str) -> bool:
        """Run `text` if it is a command. Returns True when it was one."""
        if not text.startswith("/"):
            return False

        word, _, rest = text[1:].partition(" ")
        # Telegram appends @botname when several bots share a group.
        name = word.split("@", 1)[0].strip().lower()
        args = rest.strip()

        handler = {
            "help": self._help,
            "start": self._help,
            "nodes": self._nodes,
            "battery": self._battery,
            "telemetry": self._telemetry,
        }.get(name)

        if handler is None:
            # Unknown commands are left alone: another bot in the group may own
            # them, and answering would be noise.
            log.debug("Ignoring unknown command %r", name)
            return False

        try:
            await handler(args)
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the loop
            log.exception("Command /%s failed", name)
            await self._say(f"⚠️ /{name} failed: {exc}")
        return True

    async def _say(self, text: str) -> None:
        if self._tg is not None:
            await self._tg.send_message(_clip(text))

    # --- /help ------------------------------------------------------------

    async def _help(self, args: str) -> None:
        await self._say(HELP)

    # --- /nodes -----------------------------------------------------------

    async def _nodes(self, args: str) -> None:
        records = dict(getattr(self._store, "nodes", {}) or {})
        if not records:
            await self._say("No nodes known yet.")
            return

        needle = args.strip().lower()
        rows = []
        for key, record in records.items():
            name = (record.get("name") or "").strip() or "(unnamed)"
            code = (record.get("type") or "?").upper()
            if needle and needle not in name.lower() and needle not in code.lower():
                continue
            rows.append((self._store.recency(record), key, name, code, record))

        if not rows:
            await self._say(f"No nodes matching {args.strip()!r}.")
            return

        rows.sort(reverse=True)  # most recently heard first
        header = f"📋 {len(rows)} node(s)"
        if needle:
            header += f" matching {args.strip()!r}"

        lines = [header]
        for recency, key, name, code, record in rows:
            mark = " (superseded)" if record.get("superseded_by") else ""
            lines.append(f"{code:<5} {name}{mark} — {ago(recency)} ago ({key[:6]})")
        await self._say("\n".join(lines))

    # --- /battery ---------------------------------------------------------

    async def _battery(self, args: str) -> None:
        node, days = "", 7.0
        for token in args.split():
            try:
                days = max(0.1, float(token))
            except ValueError:
                node = f"{node} {token}".strip()

        series = read_battery_series(self._metrics_path, node=node, days=days)
        text = summarise_battery(series, days)

        png = render_png(series, days) if series else None
        if png and hasattr(self._tg, "send_photo"):
            await self._tg.send_photo(png, caption=_clip(text), filename="battery.png")
            return
        if series and png is None:
            text += "\n\n(install matplotlib for a plotted chart)"
        await self._say(text)

    # --- /telemetry -------------------------------------------------------

    async def _telemetry(self, args: str) -> None:
        mesh = self._mesh()
        if mesh is None:
            await self._say("⚠️ The mesh node is offline right now.")
            return

        target = args.strip()
        if not target:
            await self._say("Usage: /telemetry <node name or key prefix>")
            return

        resolved = self._resolve(mesh, target)
        if resolved is None:
            await self._say(
                f"No node matching {target!r}. Try /nodes to see what's known."
            )
            return
        key, contact, name = resolved

        await self._say(f"📻 Asking {name} for a reading…")

        status = await self._request(mesh, "req_status_sync", contact, name)
        telemetry = await self._request(mesh, "req_telemetry_sync", contact, name)

        if status is None and telemetry is None:
            await self._say(
                f"⚠️ {name} ({key[:6]}) didn't answer. It may be out of range."
            )
            return

        lines = [f"📻 {name} ({key[:6]})"]
        if isinstance(status, dict):
            if status.get("bat"):
                lines.append(f"  battery {status['bat']}mV")
            if status.get("uptime"):
                lines.append(f"  uptime {int(status['uptime']) // 3600}h")
            for field, tag in (
                ("last_rssi", "RSSI"),
                ("last_snr", "SNR"),
                ("noise_floor", "noise"),
            ):
                if status.get(field) is not None:
                    lines.append(f"  {tag} {status[field]}")
        else:
            lines.append("  (no status reply)")

        lpp = telemetry.get("lpp") if isinstance(telemetry, dict) else telemetry
        if isinstance(lpp, list) and lpp:
            for entry in lpp:
                if isinstance(entry, dict):
                    lines.append(
                        f"  {entry.get('type', '?')} {entry.get('value')}"
                    )
        else:
            lines.append("  (no telemetry reply)")

        await self._say("\n".join(lines))

    def _resolve(self, mesh: Any, target: str):
        """Find a live contact by name or key prefix."""
        needle = target.strip().lower()
        contacts = getattr(mesh, "contacts", None)
        contacts = contacts if isinstance(contacts, dict) else {}

        def superseded(key: str) -> bool:
            record = self._store.get(key) if self._store else None
            return bool(record and record.get("superseded_by"))

        matches = [
            (key, contact)
            for key, contact in contacts.items()
            if not superseded(key)
            and (
                key.lower().startswith(needle)
                or needle in (contact.get("adv_name") or "").strip().lower()
            )
        ]
        if not matches:
            return None
        matches.sort(key=lambda kv: kv[1].get("last_advert") or 0, reverse=True)
        key, contact = matches[0]
        name = (contact.get("adv_name") or "").strip() or key[:12]
        return key, contact, name

    async def _request(self, mesh: Any, method: str, contact: Any, label: str) -> Any:
        fn = getattr(getattr(mesh, "commands", None), method, None)
        if fn is None:
            return None
        try:
            result = await asyncio.wait_for(fn(contact), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - silence is an answer here
            log.debug("%s: %s failed: %s", label, method, exc)
            return None
        if result is None:
            return None
        payload = getattr(result, "payload", None)
        if payload is not None:
            return payload
        return result if isinstance(result, (dict, list)) else None

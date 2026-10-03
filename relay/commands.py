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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("relay.commands")

try:  # pragma: no cover - trivial import guard
    from meshcore import EventType as _EventType

    EVENT_ERROR = _EventType.ERROR
    EVENT_LOGIN_SUCCESS = _EventType.LOGIN_SUCCESS
    EVENT_LOGIN_FAILED = _EventType.LOGIN_FAILED
    EVENT_TRACE_DATA = _EventType.TRACE_DATA
    EVENT_PATH_RESPONSE = _EventType.PATH_RESPONSE
except Exception:  # pragma: no cover
    EVENT_ERROR = EVENT_LOGIN_SUCCESS = EVENT_LOGIN_FAILED = object()
    EVENT_TRACE_DATA = EVENT_PATH_RESPONSE = object()

_SPARK = "▁▂▃▄▅▆▇█"

@dataclass(frozen=True)
class Command:
    """One slash command.

    Single source of truth: dispatch, /help and the list published to Telegram
    are all derived from this, so they can't drift apart.
    """

    name: str
    handler: str
    summary: str          # also the description Telegram shows; keep it short
    usage: str = ""       # argument hint, for /help only
    alias_of: str = ""    # aliases work but aren't published

    @property
    def is_alias(self) -> bool:
        return bool(self.alias_of)


COMMANDS: tuple[Command, ...] = (
    Command("nodes", "_nodes", "known nodes, newest adverts first", "[filter]"),
    Command("battery", "_battery", "battery trend from the metrics log",
            "[node] [days]"),
    Command("telemetry", "_telemetry", "ask a node for a live reading", "<node>"),
    Command("ping", "_ping", "is a node reachable, and how fast", "<node>"),
    Command("traceroute", "_traceroute", "the route to a node, hop by hop",
            "<node> [refresh]"),
    Command("advert", "_advert", "send a flood advert from this node",
            "[local]"),
    Command("proptest", "_proptest", "probe delivery from each relay",
            "[hours]"),
    Command("help", "_help", "list commands"),
    # Aliases and /start answer, but aren't published: Telegram shows /start
    # for every bot already, and duplicate entries just clutter autocomplete.
    Command("trace", "_traceroute", "alias for /traceroute", alias_of="traceroute"),
    Command("prop", "_proptest", "alias for /proptest", alias_of="proptest"),
    Command("start", "_help", "alias for /help", alias_of="help"),
)

# Telegram's rules for a published command name.
_TG_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def help_text() -> str:
    """The /help body, built from the registry."""
    lines = ["Available commands:", ""]
    for command in COMMANDS:
        if command.is_alias:
            continue
        args = f" {command.usage}" if command.usage else ""
        lines.append(f"/{command.name}{args} — {command.summary}")
    aliases = [c for c in COMMANDS if c.is_alias and c.name != "start"]
    if aliases:
        lines.append("")
        lines.append(
            "Also: " + ", ".join(f"/{c.name} (= /{c.alias_of})" for c in aliases)
        )
    return "\n".join(lines)


def published_commands() -> list[dict]:
    """The list to register with Telegram, in its expected shape.

    Aliases are left out, and anything that would be rejected is dropped here
    rather than failing the whole registration.
    """
    out = []
    for command in COMMANDS:
        if command.is_alias:
            continue
        if not _TG_NAME_RE.match(command.name):
            log.warning("Not publishing %r: invalid command name", command.name)
            continue
        description = command.summary.strip().replace("\n", " ")[:256]
        if not description:
            continue
        out.append({"command": command.name, "description": description})
    return out


HELP = help_text()

_BY_NAME = {c.name: c for c in COMMANDS}


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


def load_passwords(path: str | Path) -> dict[str, str]:
    """Admin passwords from the clock-checker config, keyed by name and pubkey.

    Missing or unreadable is not an error: without a password the commands
    simply fall back to a guest login, which still returns whatever the node
    is willing to share.
    """
    import json

    out: dict[str, str] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return out

    for node in raw.get("nodes", []) if isinstance(raw, dict) else []:
        if not isinstance(node, dict):
            continue
        password = str(node.get("password", ""))
        if not password:
            continue
        for field in ("name", "pubkey"):
            value = str(node.get(field, "")).strip().lower()
            if value:
                out[value] = password
    return out


def describe_path(contact: dict) -> str:
    """How the node is currently routed to, in words."""
    if not isinstance(contact, dict):
        return "unknown"
    length = contact.get("out_path_len")
    if length in (None, ""):
        return "unknown"
    if length < 0:
        return "flood (no stored route)"
    if length == 0:
        return "direct (0 hops)"
    return f"{length} hop(s)"


def path_hashes(contact: dict, default_bytes: int = 2) -> tuple[list[str], int]:
    """The stored route as per-hop hashes, plus the byte width they use.

    The width comes from the contact's own record where it has one: splitting a
    stored path at any other width yields nonsense hops. `default_bytes` is the
    configured radio setting, used only when the record doesn't say.
    """
    if not isinstance(contact, dict):
        return [], default_bytes
    raw = contact.get("out_path") or ""
    length = contact.get("out_path_len") or 0
    if not raw or length <= 0:
        return [], default_bytes

    mode = contact.get("out_path_hash_mode")
    hash_bytes = default_bytes if mode is None else int(mode) + 1
    width = hash_bytes * 2
    hops = [
        raw[i : i + width]
        for i in range(0, min(len(raw), width * length), width)
    ]
    return [h for h in hops if len(h) == width], hash_bytes


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
    return "\n".join(lines)


class CommandRouter:
    """Parses and runs the slash commands."""

    def __init__(
        self,
        *,
        store: Any,
        telegram: Any,
        mesh_getter: Callable[[], Any],
        metrics_path: str,
        path_hash_bytes: int = 2,
        sync_config_path: str = "time_sync.json",
        proptest_log: str = "proptest.csv",
        proptest_interval: float = 300.0,
        proptest_status: Any = None,
        advert_cooldown: float = 60.0,
        send_lock: Any = None,
        request_timeout: float = 30.0,
    ):
        self._store = store
        self._tg = telegram
        self._mesh = mesh_getter
        self._metrics_path = metrics_path
        self._path_hash_bytes = path_hash_bytes
        # Key prefixes are shown at the same width as a path hop hash, so a hop
        # printed by /traceroute can be matched against a node in /nodes.
        self._key_hex = max(2, path_hash_bytes * 2)
        self._sync_config_path = sync_config_path
        self._proptest_log = proptest_log
        self._proptest_interval = proptest_interval
        self._proptest_status = proptest_status
        self._advert_cooldown = advert_cooldown
        # Shared with the beacon, which briefly changes the node's flood scope.
        self._send_lock = send_lock
        self._last_advert = 0.0
        self._timeout = request_timeout

    async def handle(self, text: str) -> bool:
        """Run `text` if it is a command. Returns True when it was one."""
        if not text.startswith("/"):
            return False

        word, _, rest = text[1:].partition(" ")
        # Telegram appends @botname when several bots share a group.
        name = word.split("@", 1)[0].strip().lower()
        args = rest.strip()

        command = _BY_NAME.get(name)
        handler = getattr(self, command.handler, None) if command else None

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
            # The client splits anything over the limit, so nothing is cut.
            await self._tg.send_message(text)

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
            lines.append(f"{code:<5} {name}{mark} — {ago(recency)} ago ({key[:self._key_hex]})")
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
            await self._tg.send_photo(png, caption=text, filename="battery.png")
            return
        if series and png is None:
            text += "\n\n(install matplotlib for a plotted chart)"
        await self._say(text)

    # --- /telemetry -------------------------------------------------------

    def _password_for(self, key: str, name: str) -> str:
        """The configured admin password for a node, or '' for guest access."""
        passwords = load_passwords(self._sync_config_path)
        if not passwords:
            return ""
        candidates = [name.strip().lower()]
        candidates += [key[:n].lower() for n in (6, 12, len(key))]
        for candidate in candidates:
            if candidate in passwords:
                return passwords[candidate]
        # A configured prefix may be shorter than anything we tried.
        for configured, password in passwords.items():
            if configured and key.lower().startswith(configured):
                return password
        return ""

    async def _login(self, mesh: Any, contact: Any, password: str, name: str) -> str:
        """Best-effort login. Returns 'admin', 'guest' or 'none'.

        Never raises and never blocks the reading that follows: a node that
        refuses the login will still answer status and telemetry, and that
        partial answer is the point of asking.
        """
        commands = getattr(mesh, "commands", None)
        send = getattr(commands, "_send_login_raw", None) or getattr(
            commands, "send_login", None
        )
        waiter = getattr(commands, "wait_for_events", None)
        if send is None or waiter is None:
            return "none"

        kind = "admin" if password else "guest"
        try:
            sent = await asyncio.wait_for(send(contact, password), timeout=self._timeout)
            if sent is None or getattr(sent, "type", None) == EVENT_ERROR:
                log.debug("%s: login could not be sent", name)
                return "none"
            event = await waiter(
                [EVENT_LOGIN_SUCCESS, EVENT_LOGIN_FAILED], timeout=self._timeout
            )
            if getattr(event, "type", None) == EVENT_LOGIN_SUCCESS:
                return kind
            log.debug("%s: %s login not accepted", name, kind)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: login attempt failed: %s", name, exc)
        return "none"

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

        password = self._password_for(key, name)
        await self._say(
            f"📻 Asking {name} for a reading"
            f"{'' if password else ' (no password configured — trying as guest)'}…"
        )

        # Some nodes answer more once logged in. Without a configured password
        # a blank one is tried, and whatever comes back is reported either way.
        access = await self._login(mesh, contact, password, name)

        status = await self._request(mesh, "req_status_sync", contact, name)
        telemetry = await self._request(mesh, "req_telemetry_sync", contact, name)

        if status is None and telemetry is None:
            hint = "" if access != "none" else " The login wasn't accepted either."
            await self._say(
                f"⚠️ {name} ({key[:self._key_hex]}) didn't answer.{hint} It may be out of range."
            )
            return

        badge = {"admin": " [admin]", "guest": " [guest]"}.get(access, " [no login]")
        lines = [f"📻 {name} ({key[:self._key_hex]}){badge}"]
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

    # --- /proptest ----------------------------------------------------------

    async def _proptest(self, args: str) -> None:
        from .proptest import ProbeLog, bar, summarise

        hours = 24.0
        for token in args.split():
            try:
                hours = max(0.1, float(token))
            except ValueError:
                pass

        rows = ProbeLog(self._proptest_log).rows(hours=hours)
        results = summarise(rows, fallback_interval=self._proptest_interval)
        if not results:
            lines = [f"No probes heard in the last {hours:g}h."]
            status = self._proptest_status() if self._proptest_status else None
            if isinstance(status, dict):
                # Report the live state so it's clear which half is failing:
                # ours transmitting, or ours receiving.
                lines.append(
                    f"\nThis relay: id {status.get('id')!r}, channel "
                    f"{status.get('channel')!r} "
                    f"(index {status.get('channel_idx')})"
                )
                lines.append(
                    f"  enabled: {status.get('enabled')}   "
                    f"beaconing: {status.get('beaconing')}"
                )
                lines.append(
                    f"  probes sent this session: {status.get('sent')}   "
                    f"heard: {status.get('heard')}"
                )
                other = status.get("heard_other") or 0
                echo = status.get("own_echo") or 0
                if other or echo:
                    lines.append(
                        f"  other traffic on that channel: {other}   "
                        f"own beacons echoed back: {echo}"
                    )
                if not status.get("enabled"):
                    lines.append("\nSet PROPTEST_ENABLED=true to turn it on.")
                elif status.get("channel_idx") is None:
                    lines.append(
                        "\nThe channel isn't resolved, so nothing can be sent "
                        "or heard on it."
                    )
                elif status.get("heard_other") or status.get("own_echo"):
                    lines.append(
                        "\nWe are receiving on that channel — traffic has "
                        "arrived, just nothing that parsed as a peer probe. So "
                        "the link works and the senders are the question."
                    )
                elif status.get("sent") and not status.get("heard"):
                    lines.append(
                        "\nWe're transmitting but nothing has arrived on that "
                        "channel at all. Either no other relay is beaconing, "
                        "or this radio isn't receiving them — compare with "
                        "/proptest on another relay."
                    )
            await self._say("\n".join(lines))
            return

        lines = [f"📶 Probe delivery, last {hours:g}h"]
        for r in results:
            snr = f"  SNR {r.snr:+.1f}" if r.snr is not None else ""
            lines.append(
                f"\n{r.origin}\n"
                f"  {bar(r.rate)} {r.rate:.0f}%  ({r.heard}/{r.expected})\n"
                f"  last {ago(r.last)} ago{snr}"
            )
        lines.append(
            "\nThis is what reached us from each relay, so a low figure can "
            "be either end of the link."
        )
        await self._say("\n".join(lines))

    # --- /advert ------------------------------------------------------------

    async def _advert(self, args: str) -> None:
        """Transmit an advert, flooded by default.

        A flood advert reaches the whole mesh, which is the point when you've
        just moved an antenna, but it is also the noisiest thing this bot can
        do — hence the cooldown against a double tap.
        """
        mesh = self._mesh()
        if mesh is None:
            await self._say("⚠️ The mesh node is offline right now.")
            return

        wants_local = args.strip().lower() in ("local", "direct", "zero", "zerohop")
        flood = not wants_local

        since = time.monotonic() - self._last_advert
        if self._last_advert and since < self._advert_cooldown:
            await self._say(
                f"⏳ An advert went out {since:.0f}s ago. Wait "
                f"{self._advert_cooldown - since:.0f}s more, or lower "
                f"ADVERT_COOLDOWN."
            )
            return

        send = getattr(getattr(mesh, "commands", None), "send_advert", None)
        if send is None:
            await self._say("⚠️ This meshcore version can't send adverts.")
            return

        kind = "flood" if flood else "local (zero-hop)"
        try:
            if self._send_lock is not None:
                async with self._send_lock:
                    result = await asyncio.wait_for(
                        send(flood=flood), timeout=self._timeout
                    )
            else:
                result = await asyncio.wait_for(
                    send(flood=flood), timeout=self._timeout
                )
        except Exception as exc:  # noqa: BLE001
            await self._say(f"⚠️ Advert failed: {exc}")
            return

        if getattr(result, "type", None) == EVENT_ERROR:
            await self._say(f"⚠️ The node refused the advert: {result.payload}")
            return

        self._last_advert = time.monotonic()
        log.info("Sent a %s advert on request", kind)
        await self._say(
            f"📢 Sent a {kind} advert."
            + ("" if flood else " Only direct neighbours will hear it.")
        )

    # --- /ping ------------------------------------------------------------

    async def _ping(self, args: str) -> None:
        mesh = self._mesh()
        if mesh is None:
            await self._say("⚠️ The mesh node is offline right now.")
            return

        target = args.strip()
        if not target:
            await self._say("Usage: /ping <node name or key prefix>")
            return

        resolved = self._resolve(mesh, target)
        if resolved is None:
            await self._say(f"No node matching {target!r}. Try /nodes.")
            return
        key, contact, name = resolved

        await self._say(f"📡 Pinging {name}…")

        # Status is the cheapest thing most nodes answer; telemetry is the
        # fallback for those that don't serve it.
        started = time.monotonic()
        reply = await self._request(mesh, "req_status_sync", contact, name)
        via = "status"
        if reply is None:
            started = time.monotonic()
            reply = await self._request(mesh, "req_telemetry_sync", contact, name)
            via = "telemetry"

        elapsed = (time.monotonic() - started) * 1000
        route = describe_path(contact)

        if reply is None:
            await self._say(
                f"❌ {name} ({key[:self._key_hex]}) — no reply in {elapsed / 1000:.0f}s\n"
                f"  route: {route}"
            )
            return

        lines = [
            f"✅ {name} ({key[:self._key_hex]}) — {elapsed:.0f}ms via {via}",
            f"  route: {route}",
        ]
        if isinstance(reply, dict):
            for field, tag in (("last_rssi", "RSSI"), ("last_snr", "SNR")):
                if reply.get(field) is not None:
                    lines.append(f"  {tag} {reply[field]}")
        await self._say("\n".join(lines))

    # --- /traceroute --------------------------------------------------------

    async def _traceroute(self, args: str) -> None:
        mesh = self._mesh()
        if mesh is None:
            await self._say("⚠️ The mesh node is offline right now.")
            return

        target = args.strip()
        tokens = target.split()
        refresh = bool(tokens) and tokens[-1].lower() in (
            "refresh", "rediscover", "new", "relearn"
        )
        if refresh:
            target = " ".join(tokens[:-1]).strip()

        if not target:
            await self._say(
                "Usage: /traceroute <node name or key prefix> [refresh]"
            )
            return

        resolved = self._resolve(mesh, target)
        if resolved is None:
            await self._say(f"No node matching {target!r}. Try /nodes.")
            return
        key, contact, name = resolved

        if refresh:
            contact = await self._relearn_path(mesh, contact, name, key) or contact

        hops, hash_bytes = path_hashes(contact, self._path_hash_bytes)
        if not hops:
            await self._say(
                f"🛣 {name} ({key[:self._key_hex]}) — {describe_path(contact)}.\n"
                f"There's no stored route to trace. Asking the mesh to find one…"
            )
            await self._discover_path(mesh, contact, name, key)
            return

        await self._say(
            f"🛣 Tracing {name} via {len(hops)} hop(s): {' → '.join(hops)}…"
        )

        result = await self._trace(mesh, hops, name, hash_bytes)
        if result is None:
            await self._say(
                f"❌ No trace reply from {name} ({key[:self._key_hex]}). The route may be "
                f"stale — /ping will say whether it answers at all."
            )
            return

        lines = [f"🛣 {name} ({key[:self._key_hex]})"]
        if hash_bytes != self._path_hash_bytes:
            # The stored route predates a change of path hash width, so it is
            # traced at the width it was learned with, not the current one.
            lines.append(
                f"  (route stored at {hash_bytes}-byte hops, not the "
                f"{self._path_hash_bytes} now configured — "
                f"“/traceroute {name} refresh” re-learns it)"
            )
        nodes = result.get("path") or []
        for index, node in enumerate(nodes, start=1):
            node_hash = node.get("hash")
            snr = node.get("snr")
            snr_text = f"SNR {snr}" if snr is not None else ""
            if node_hash:
                lines.append(f"  {index}. {node_hash}  {snr_text}".rstrip())
            else:
                # The last entry is our own radio reporting what it heard back.
                lines.append(f"  {index}. (us)  {snr_text}".rstrip())
        if not nodes:
            lines.append("  (reply carried no hop details)")
        await self._say("\n".join(lines))

    async def _relearn_path(self, mesh: Any, contact: Any, name: str, key: str):
        """Forget the stored route and discover it again.

        A route learned before the node's path hash width changed is still
        usable, but it was recorded at the old width — re-learning it is what
        gets the hops back at the current size.
        """
        commands = getattr(mesh, "commands", None)
        reset = getattr(commands, "reset_path", None)
        discover = getattr(commands, "send_path_discovery_sync", None)
        if reset is None or discover is None:
            await self._say("This meshcore version can't re-learn a route.")
            return None

        await self._say(f"🔄 Forgetting the stored route to {name} and re-discovering…")
        try:
            await asyncio.wait_for(reset(contact), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: reset_path failed: %s", name, exc)

        try:
            result = await asyncio.wait_for(discover(contact), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001
            await self._say(f"❌ Route discovery failed: {exc}")
            return None

        payload = getattr(result, "payload", None) if result is not None else None
        if not isinstance(payload, dict) or not payload.get("out_path_len"):
            await self._say(
                f"❌ No route found to {name} ({key[:self._key_hex]}) — it may only "
                f"be reachable by flood."
            )
            return None

        width = payload.get("out_path_hash_len") or self._path_hash_bytes
        await self._say(
            f"✅ Re-learned a {payload['out_path_len']} hop route at "
            f"{width}-byte hops."
        )

        # Refresh the cached contact so the trace below uses the new route.
        try:
            await mesh.ensure_contacts(follow=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: could not refresh contacts: %s", name, exc)
        contacts = getattr(mesh, "contacts", None)
        if isinstance(contacts, dict) and key in contacts:
            return contacts[key]
        return None

    async def _trace(
        self, mesh: Any, hops: list[str], name: str, hash_bytes: int = 2
    ) -> Optional[dict]:
        """Send a trace along `hops` and wait for the matching reply."""
        import random

        commands = getattr(mesh, "commands", None)
        send = getattr(commands, "send_trace", None)
        if send is None:
            log.info("This meshcore version has no send_trace")
            return None

        tag = random.randint(1, 0xFFFFFFFF)
        # Pass flags explicitly rather than letting them be inferred, so the
        # width the node uses is the width these hops actually are. Trace flags
        # encode the width as 1 << s, which can't express 3 bytes; there the
        # library infers it from the path instead.
        flags = {1: 0, 2: 1, 4: 2}.get(hash_bytes)
        kwargs = {"tag": tag, "path": ",".join(hops)}
        if flags is not None:
            kwargs["flags"] = flags
        try:
            sent = await asyncio.wait_for(
                send(**kwargs), timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: trace could not be sent: %s", name, exc)
            return None
        if sent is None or getattr(sent, "type", None) == EVENT_ERROR:
            log.debug("%s: trace rejected: %s", name, getattr(sent, "payload", None))
            return None

        try:
            event = await mesh.wait_for_event(
                EVENT_TRACE_DATA,
                attribute_filters={"tag": tag},
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: waiting for trace failed: %s", name, exc)
            return None
        return getattr(event, "payload", None) if event else None

    async def _discover_path(self, mesh: Any, contact: Any, name: str, key: str) -> None:
        """Ask the mesh to find a route when none is stored."""
        commands = getattr(mesh, "commands", None)
        discover = getattr(commands, "send_path_discovery_sync", None)
        if discover is None:
            await self._say("Path discovery isn't available in this meshcore build.")
            return
        try:
            result = await asyncio.wait_for(discover(contact), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: path discovery failed: %s", name, exc)
            result = None

        payload = getattr(result, "payload", None) if result is not None else None
        if not isinstance(payload, dict):
            await self._say(f"❌ No route found to {name} ({key[:self._key_hex]}).")
            return

        out_len = payload.get("out_path_len")
        out_path = payload.get("out_path") or ""
        if not out_len:
            await self._say(f"🛣 {name} ({key[:self._key_hex]}) — reachable directly, no hops.")
            return
        await self._say(
            f"🛣 {name} ({key[:self._key_hex]}) — found a {out_len} hop route: {out_path}\n"
            f"Run /traceroute again for per-hop SNR."
        )

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
        name = (contact.get("adv_name") or "").strip() or key[: self._key_hex]
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

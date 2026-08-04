"""Check, and optionally correct, the clocks of remote MeshCore nodes.

A repeater or room server is asked for its time with the admin CLI `clock`
command, which needs a remote login first. If the node is behind and the config
allows it, `time <epoch>` pushes the correct time.

Firmware detail that shapes all of this: `time <epoch>` is accepted only when
the new value is strictly greater than the node's current clock, so a node
running *ahead* cannot be corrected over the mesh at all — it needs a power
cycle. We detect that case and report it rather than pretending to fix it.

The `clock` reply has minute resolution ("14:23 - 12/3/2025 UTC"), so a
perfectly-set node can still read up to 59s behind. Keep the tolerance above a
minute to avoid chasing that quantisation.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from meshcore import EventType

log = logging.getLogger("relay.timesync")

# "14:23 - 12/3/2025 UTC"  ->  hour, minute, day, month, year
_CLOCK_RE = re.compile(
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*-\s*"
    r"(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})"
)

# The reply to a rejected `time` command.
BACKWARDS_ERROR = "clock cannot go backwards"


class TimeSyncError(Exception):
    """A node could not be queried or corrected."""


@dataclass
class NodeResult:
    name: str
    status: str  # ok | corrected | behind | ahead | error | skipped
    drift: Optional[int] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "corrected", "skipped")


@dataclass
class NodeSpec:
    """One managed node from the config file."""

    name: str
    password: str = ""
    set_time: bool = False
    pubkey: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "NodeSpec":
        name = str(raw.get("name", "")).strip()
        pubkey = str(raw.get("pubkey", "")).strip()
        if not name and not pubkey:
            raise TimeSyncError("each node needs a 'name' or a 'pubkey'")
        return cls(
            name=name,
            password=str(raw.get("password", "")),
            set_time=bool(raw.get("set_time", False)),
            pubkey=pubkey,
        )


@dataclass
class SyncConfig:
    tolerance_seconds: int = 120
    reply_timeout: float = 45.0
    nodes: list[NodeSpec] = field(default_factory=list)
    # Collect metrics from nodes discovered by the relay, not just listed ones.
    metrics_for_known_nodes: bool = False
    metrics_node_types: frozenset[str] = frozenset({"REP", "ROOM"})

    @classmethod
    def from_dict(cls, raw: dict) -> "SyncConfig":
        nodes = [NodeSpec.from_dict(n) for n in raw.get("nodes", [])]
        tolerance = int(raw.get("tolerance_seconds", 120))
        types = raw.get("metrics_node_types")
        node_types = (
            frozenset(str(t).upper() for t in types)
            if isinstance(types, list) and types
            else frozenset({"REP", "ROOM"})
        )
        if tolerance < 60:
            log.warning(
                "tolerance_seconds=%d is below the clock reply's 60s resolution; "
                "expect nodes to look permanently skewed",
                tolerance,
            )
        return cls(
            tolerance_seconds=tolerance,
            reply_timeout=float(raw.get("reply_timeout", 45.0)),
            nodes=nodes,
            metrics_for_known_nodes=bool(raw.get("metrics_for_known_nodes", False)),
            metrics_node_types=node_types,
        )


def parse_clock(text: str) -> int:
    """Turn a `clock` reply into a UTC epoch. Raises if it can't be read."""
    match = _CLOCK_RE.search(text or "")
    if not match:
        raise TimeSyncError(f"could not parse clock reply: {text!r}")
    g = match.groupdict()
    try:
        return calendar.timegm(
            (
                int(g["year"]),
                int(g["month"]),
                int(g["day"]),
                int(g["hour"]),
                int(g["minute"]),
                0,
                0,
                0,
                0,
            )
        )
    except (ValueError, OverflowError) as exc:
        raise TimeSyncError(f"clock reply had impossible values: {text!r} ({exc})")


def classify(drift: int, tolerance: int) -> str:
    """drift = node clock minus true time, in seconds."""
    if abs(drift) <= tolerance:
        return "ok"
    return "ahead" if drift > 0 else "behind"


class NodeTimeSync:
    """Runs the query/correct cycle against one connected MeshCore node."""

    def __init__(
        self,
        mesh: Any,
        config: SyncConfig,
        *,
        dry_run: bool = False,
        metrics: Any = None,
        store: Any = None,
    ):
        self._mesh = mesh
        self._cfg = config
        self._dry_run = dry_run
        self._metrics = metrics
        self._store = store

    async def run(self) -> list[NodeResult]:
        results: list[NodeResult] = []
        for spec in self._cfg.nodes:
            label = spec.name or spec.pubkey[:12]

            # Without a contact there's nothing to query at all.
            try:
                contact = await self._resolve(spec)
            except TimeSyncError as exc:
                log.warning("%s: %s", label, exc)
                results.append(NodeResult(label, "error", detail=str(exc)))
                continue

            result = await self._safe_clock_cycle(spec, contact, label)
            results.append(result)

            # Metrics are collected whatever the clock outcome: telemetry needs
            # no admin login, so a node we can't manage can still be graphed.
            if self._metrics is not None:
                try:
                    await self._metrics.collect(
                        self._mesh, label, contact, result.drift
                    )
                except Exception:  # noqa: BLE001
                    log.exception("%s: metrics collection failed", label)

        results.extend(await self._discovered_metrics(results))
        return results

    async def _discovered_metrics(self, done: list[NodeResult]) -> list[NodeResult]:
        """Sample nodes the relay discovered but the config doesn't list.

        Telemetry needs no login, so anything the relay has seen can be graphed
        without being configured by hand.
        """
        if not (
            self._cfg.metrics_for_known_nodes
            and self._metrics is not None
            and self._store is not None
        ):
            return []

        covered = {r.name for r in done}
        extra: list[NodeResult] = []

        for pubkey, record in (self._store.nodes or {}).items():
            code = (record.get("type") or "").upper()
            if code not in self._cfg.metrics_node_types:
                continue
            label = (record.get("name") or "").strip() or pubkey[:12]
            if label in covered:
                continue  # already handled as a configured node

            contact = self._contact_for(pubkey)
            if contact is None:
                log.debug("%s: discovered but not in the node's contacts", label)
                continue
            try:
                await self._metrics.collect(self._mesh, label, contact, None)
                extra.append(
                    NodeResult(label, "skipped", detail="metrics only (discovered)")
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: discovered-node metrics failed: %s", label, exc)
                extra.append(NodeResult(label, "error", detail=repr(exc)))
        return extra

    def _contact_for(self, pubkey: str) -> Any:
        contacts = getattr(self._mesh, "contacts", None)
        if isinstance(contacts, dict):
            contact = contacts.get(pubkey)
            if contact is not None:
                return contact
        return None

    async def _safe_clock_cycle(
        self, spec: NodeSpec, contact: Any, label: str
    ) -> NodeResult:
        try:
            return await self._clock_cycle(spec, contact, label)
        except TimeSyncError as exc:
            log.warning("%s: %s", label, exc)
            return NodeResult(label, "error", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad node must not stop the rest
            log.exception("%s: unexpected failure", label)
            return NodeResult(label, "error", detail=repr(exc))

    async def _clock_cycle(self, spec: NodeSpec, contact: Any, label: str) -> NodeResult:
        if not spec.password:
            if spec.set_time:
                # They asked for the clock to be managed, so a missing password
                # is a misconfiguration rather than an intentional skip.
                raise TimeSyncError(
                    "set_time is on but no password is configured; "
                    "the admin CLI needs a login"
                )
            return NodeResult(
                label, "skipped", detail="no password; clock not checked"
            )

        await self._login(contact, spec.password, label)

        reply = await self._command(contact, "clock", label)
        node_epoch = parse_clock(reply)
        # Read the reference clock after the round trip so transit time counts
        # against the drift rather than being silently absorbed into it.
        now = int(time.time())
        drift = node_epoch - now
        status = classify(drift, self._cfg.tolerance_seconds)

        log.info("%s: clock %s (drift %+ds) -> %s", label, reply.strip(), drift, status)

        if status == "ok":
            return NodeResult(label, "ok", drift, reply.strip())

        if status == "ahead":
            # Firmware refuses to move a clock backwards, so this needs hands on
            # the hardware. Don't waste a transmission attempting it.
            return NodeResult(
                label,
                "ahead",
                drift,
                f"running {drift}s ahead; firmware refuses to set a clock "
                f"backwards, so this needs a power cycle at the node",
            )

        if not spec.set_time:
            return NodeResult(
                label, "behind", drift, f"{-drift}s behind (set_time is off)"
            )

        if self._dry_run:
            return NodeResult(
                label, "behind", drift, f"{-drift}s behind (dry run, not changed)"
            )

        return await self._correct(contact, label, drift)

    async def _correct(self, contact: Any, label: str, drift: int) -> NodeResult:
        # Re-read the clock as late as possible; `time` is only accepted when
        # strictly greater than the node's current value.
        target = int(time.time()) + 1
        reply = await self._command(contact, f"time {target}", label)

        if BACKWARDS_ERROR in reply:
            return NodeResult(
                label, "ahead", drift, f"node rejected the update: {reply.strip()}"
            )
        if "OK" not in reply.upper():
            raise TimeSyncError(f"unexpected reply to time command: {reply!r}")

        log.info("%s: clock corrected (%s)", label, reply.strip())
        return NodeResult(label, "corrected", drift, reply.strip())

    async def _resolve(self, spec: NodeSpec) -> Any:
        """Find the contact record for a configured node."""
        if spec.pubkey:
            contacts = getattr(self._mesh, "contacts", None) or {}
            for key, contact in contacts.items():
                if key.lower().startswith(spec.pubkey.lower()):
                    return contact
            # The store may hold the full key for a prefix we were given.
            if self._store is not None:
                found = self._store.find(spec.pubkey)
                if found:
                    contact = self._contact_for(found[0])
                    if contact is not None:
                        return contact
            # Fall back to the raw key; the library accepts a hex string.
            return spec.pubkey

        contact = self._mesh.get_contact_by_name(spec.name)
        if contact:
            return contact

        # Fall back to the relay's node store: it may know this name from an
        # advert even when the live contact lookup misses it.
        if self._store is not None:
            found = self._store.find(spec.name)
            if found:
                pubkey, _record = found
                contact = self._contact_for(pubkey)
                if contact is not None:
                    log.debug("%s: resolved via the node store", spec.name)
                    return contact
                raise TimeSyncError(
                    f"{spec.name!r} is in the node store ({pubkey[:12]}) but not in "
                    f"the node's contact list, so it can't be reached"
                )

        raise TimeSyncError(
            f"no contact named {spec.name!r} on the node "
            f"(names must match the advertised name exactly; "
            f"run scripts/list_nodes.py to see what's known)"
        )

    async def _login(self, contact: Any, password: str, label: str) -> None:
        try:
            result = await asyncio.wait_for(
                self._mesh.commands.send_login_sync(contact, password),
                timeout=self._cfg.reply_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeSyncError(
                f"login timed out after {self._cfg.reply_timeout:.0f}s "
                f"(node out of reach?)"
            )
        if result is None or getattr(result, "type", None) == EventType.ERROR:
            raise TimeSyncError(
                "login failed (wrong password, or the node is out of reach)"
            )
        log.debug("%s: logged in", label)

    async def _command(self, contact: Any, cmd: str, label: str) -> str:
        """Send an admin CLI command and return the node's text reply."""
        pubkey = self._pubkey_of(contact)
        filters = {"pubkey_prefix": pubkey[:12]} if pubkey else None

        result = await self._mesh.commands.send_cmd(contact, cmd)
        if getattr(result, "type", None) == EventType.ERROR:
            raise TimeSyncError(f"node refused command {cmd!r}: {result.payload}")

        event = await self._mesh.wait_for_event(
            EventType.CONTACT_MSG_RECV,
            attribute_filters=filters,
            timeout=self._cfg.reply_timeout,
        )
        if event is None:
            raise TimeSyncError(
                f"no reply to {cmd!r} within {self._cfg.reply_timeout:.0f}s"
            )
        return (event.payload or {}).get("text", "")

    @staticmethod
    def _pubkey_of(contact: Any) -> str:
        if isinstance(contact, dict):
            return str(contact.get("public_key", ""))
        if isinstance(contact, str):
            return contact
        return ""


def summarise(results: list[NodeResult]) -> str:
    """One-line-per-node report, suitable for logs or a Telegram message."""
    if not results:
        return "No nodes configured for time sync."

    icons = {
        "ok": "✅",
        "corrected": "\U0001F551",
        "behind": "⏱",
        "ahead": "⚠️",
        "error": "❌",
        "skipped": "⏭",
    }
    lines = []
    for r in results:
        drift = f" ({r.drift:+d}s)" if r.drift is not None else ""
        detail = f" — {r.detail}" if r.detail and r.status != "ok" else ""
        lines.append(f"{icons.get(r.status, '?')} {r.name}{drift}{detail}")
    return "\n".join(lines)

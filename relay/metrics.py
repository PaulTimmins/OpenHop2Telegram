"""Collect node health metrics and append them to a CSV for later graphing.

Two sources are combined per node:

* **Status** (`req_status_sync`) — battery millivolts, uptime, radio counters,
  noise floor, last RSSI/SNR, airtime.
* **Telemetry** (`req_telemetry_sync`) — whatever sensors the node publishes, as
  Cayenne LPP entries: temperature, voltage, current, percentage, illuminance,
  power.

Both are best-effort. A node that doesn't answer one of them (or needs a login
it wasn't given) still gets a row, with the missing columns left blank, so gaps
show up as gaps in a graph rather than losing the whole sample.

The column set is fixed so the file stays loadable by anything that expects a
stable header. Any LPP reading that doesn't map to a named column is preserved
verbatim in `telemetry_json`, so nothing is silently dropped.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from meshcore import EventType

log = logging.getLogger("relay.metrics")

# Stable CSV schema. Appending new names here is safe for new files, but an
# existing file keeps the header it was created with — start a new file if the
# columns change.
COLUMNS = [
    "timestamp_utc",
    "epoch",
    "node",
    "pubkey",
    "clock_drift_s",
    # power / environment
    "battery_mv",
    "battery_pct",
    "voltage_v",
    "current_a",
    "charge_state",
    "temperature_c",
    "humidity_pct",
    "illuminance_lux",
    "power_w",
    # radio / housekeeping
    "uptime_s",
    "tx_queue_len",
    "noise_floor",
    "last_rssi",
    "last_snr",
    "airtime_s",
    "rx_airtime_s",
    "nb_sent",
    "nb_recv",
    "sent_flood",
    "sent_direct",
    "recv_flood",
    "recv_direct",
    "direct_dups",
    "flood_dups",
    "full_evts",
    "recv_errors",
    # provenance
    "status_ok",
    "telemetry_ok",
    "telemetry_json",
]

# LPP type name -> CSV column. First occurrence of a type wins; everything is
# kept in telemetry_json regardless.
LPP_COLUMNS = {
    "temperature": "temperature_c",
    "humidity": "humidity_pct",
    "voltage": "voltage_v",
    "current": "current_a",
    "percentage": "battery_pct",
    "illuminance": "illuminance_lux",
    "power": "power_w",
}

# Status field -> CSV column.
STATUS_COLUMNS = {
    "bat": "battery_mv",
    "uptime": "uptime_s",
    "tx_queue_len": "tx_queue_len",
    "noise_floor": "noise_floor",
    "last_rssi": "last_rssi",
    "last_snr": "last_snr",
    "airtime": "airtime_s",
    "rx_airtime": "rx_airtime_s",
    "nb_sent": "nb_sent",
    "nb_recv": "nb_recv",
    "sent_flood": "sent_flood",
    "sent_direct": "sent_direct",
    "recv_flood": "recv_flood",
    "recv_direct": "recv_direct",
    "direct_dups": "direct_dups",
    "flood_dups": "flood_dups",
    "full_evts": "full_evts",
    "recv_errors": "recv_errors",
}

# Current magnitude below which the node is treated as neither charging nor
# discharging, to stop sensor noise flapping the state.
_CURRENT_DEADBAND_A = 0.02


def charge_state(current: Optional[float]) -> str:
    """Infer a charge state from LPP current.

    Sign convention is the node's, not ours: this assumes positive current means
    charge going in. Check it against a node you can observe before trusting the
    column — `current_a` is logged raw so it can always be re-derived.
    """
    if current is None:
        return ""
    if current > _CURRENT_DEADBAND_A:
        return "charging"
    if current < -_CURRENT_DEADBAND_A:
        return "discharging"
    return "idle"


def flatten_telemetry(lpp: Any) -> dict:
    """Map LPP entries onto named columns.

    Entries look like {"channel": 1, "type": "temperature", "value": 21.5}.
    """
    out: dict[str, Any] = {}
    if not isinstance(lpp, list):
        return out
    for entry in lpp:
        if not isinstance(entry, dict):
            continue
        column = LPP_COLUMNS.get(str(entry.get("type", "")).lower())
        value = entry.get("value")
        # Composite readings (gps, accelerometer) arrive as dict/list; those
        # belong in telemetry_json, not a scalar column.
        if not column or column in out or isinstance(value, (dict, list)):
            continue
        out[column] = value
    return out


class MetricsWriter:
    """Appends metric rows to a CSV, writing the header on first use."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)

    def write(self, row: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self._path.exists() or self._path.stat().st_size == 0
            with self._path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=COLUMNS, extrasaction="ignore", restval=""
                )
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            # Metrics are a nice-to-have; never fail the run over them.
            log.warning("Could not write metrics to %s: %s", self._path, exc)


class MetricsCollector:
    """Queries a node for status + telemetry and records one CSV row."""

    def __init__(self, writer: MetricsWriter, *, timeout: float = 30.0):
        self._writer = writer
        self._timeout = timeout

    async def collect(
        self,
        mesh: Any,
        label: str,
        contact: Any,
        drift: Optional[int] = None,
    ) -> dict:
        now = int(time.time())
        row: dict[str, Any] = {c: "" for c in COLUMNS}
        row.update(
            {
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "epoch": now,
                "node": label,
                "pubkey": self._pubkey(contact)[:12],
                "clock_drift_s": "" if drift is None else drift,
                "status_ok": "false",
                "telemetry_ok": "false",
            }
        )

        status = await self._request(mesh, "req_status_sync", contact, label)
        if isinstance(status, dict):
            row["status_ok"] = "true"
            for src, col in STATUS_COLUMNS.items():
                value = status.get(src)
                if value is not None:
                    row[col] = value

        telemetry = await self._request(mesh, "req_telemetry_sync", contact, label)
        if isinstance(telemetry, dict):
            lpp = telemetry.get("lpp")
            row["telemetry_ok"] = "true"
            row["telemetry_json"] = json.dumps(lpp, separators=(",", ":"))
            row.update(flatten_telemetry(lpp))

        row["charge_state"] = charge_state(_as_float(row.get("current_a")))

        self._writer.write(row)
        log.info(
            "%s: metrics bat=%smV temp=%s charge=%s (status=%s telemetry=%s)",
            label,
            row.get("battery_mv") or "?",
            row.get("temperature_c") or "?",
            row.get("charge_state") or "?",
            row["status_ok"],
            row["telemetry_ok"],
        )
        return row

    async def _request(self, mesh: Any, method: str, contact: Any, label: str) -> Any:
        fn = getattr(mesh.commands, method, None)
        if fn is None:
            log.debug("%s: %s unavailable in this meshcore version", label, method)
            return None
        try:
            # These wait on a reply from a node that may simply never answer, so
            # they need our own deadline: an unreachable node would otherwise
            # hang the whole run.
            result = await asyncio.wait_for(fn(contact), timeout=self._timeout)
        except asyncio.TimeoutError:
            log.info("%s: %s timed out after %.0fs", label, method, self._timeout)
            return None
        except Exception as exc:  # noqa: BLE001 - a silent node is normal
            log.debug("%s: %s failed: %s", label, method, exc)
            return None
        if result is None or getattr(result, "type", None) == EventType.ERROR:
            log.debug("%s: %s returned no usable payload", label, method)
            return None
        return result.payload

    @staticmethod
    def _pubkey(contact: Any) -> str:
        if isinstance(contact, dict):
            return str(contact.get("public_key", ""))
        return contact if isinstance(contact, str) else ""


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

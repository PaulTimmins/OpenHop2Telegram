"""Propagation testing over a dedicated channel.

Each relay beacons a tiny probe on its own schedule, and every relay logs the
probes it hears. What that measures is one direction of one link: the fraction
of a peer's transmissions that reached *us*. A peer showing 40% isn't
necessarily transmitting badly — we may simply be hearing it badly — which is
the useful thing to know when siting a node.

The probe carries the sender's own interval so reliability survives someone
changing theirs, and a sequence number purely for spotting duplicates and
restarts. Channel messages carry no sender key, so identity has to be in the
text like everything else on a channel.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("relay.proptest")

# Kept short: this rides in a LoRa payload alongside everything else.
PREFIX = "TGP"
COLUMNS = ["heard_epoch", "origin", "seq", "sent_epoch", "interval", "snr", "path_len"]


# Channel messages arrive as "<sender node name>: <text>" -- the node prepends
# its own advert name, since a channel message carries no sender key. So the
# probe body has to be located rather than assumed to start at character zero.
_BODY_RE = re.compile(r"(?i)\bTGP\|")


@dataclass
class Probe:
    origin: str
    seq: int
    sent: float
    interval: float
    # The transmitting node's advert name, when the prefix carried one. Kept
    # for the log line; `origin` is the relay's own id and is what we key on.
    node: str = ""


def format_probe(origin: str, seq: int, interval: float) -> str:
    """TGP|origin|seq|sent_epoch|interval"""
    clean = (origin or "node").replace("|", "-").strip()[:24]
    return f"{PREFIX}|{clean}|{seq}|{int(time.time())}|{int(interval)}"


def parse_probe(text: str) -> Optional[Probe]:
    """Read a probe, ignoring a sender-name prefix and any future fields."""
    raw = (text or "").strip()
    match = _BODY_RE.search(raw)
    if match is None:
        return None

    # Anything before the body is the node name the firmware prepended.
    node = raw[: match.start()].strip().rstrip(":").strip()
    parts = raw[match.start() :].split("|")
    if len(parts) < 3:
        return None

    origin = parts[1].strip()
    if not origin:
        return None

    def number(index: int, default: float) -> float:
        try:
            return float(parts[index])
        except (IndexError, TypeError, ValueError):
            return default

    return Probe(
        origin=origin,
        seq=int(number(2, 0)),
        sent=number(3, time.time()),
        interval=number(4, 0) or 0.0,
        node=node,
    )


@dataclass
class Reliability:
    origin: str
    heard: int
    expected: int
    first: float
    last: float
    snr: Optional[float]
    interval: float

    @property
    def rate(self) -> float:
        return 100.0 * self.heard / self.expected if self.expected else 0.0


class ProbeLog:
    """Append-only record of probes heard, with a stable CSV header."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)

    def ensure_file(self) -> None:
        """Create the log with its header if it isn't there yet."""
        try:
            if self._path.exists() and self._path.stat().st_size > 0:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=COLUMNS).writeheader()
            log.info("Probe log ready at %s", self._path)
        except OSError as exc:
            log.warning("Could not create %s: %s", self._path, exc)

    def record(
        self,
        probe: Probe,
        *,
        snr: Optional[float] = None,
        path_len: Optional[int] = None,
    ) -> None:
        row = {
            "heard_epoch": int(time.time()),
            "origin": probe.origin,
            "seq": probe.seq,
            "sent_epoch": int(probe.sent),
            "interval": int(probe.interval),
            "snr": "" if snr is None else snr,
            "path_len": "" if path_len is None else path_len,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            new = not self._path.exists() or self._path.stat().st_size == 0
            with self._path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=COLUMNS, extrasaction="ignore", restval=""
                )
                if new:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            log.warning("Could not append to %s: %s", self._path, exc)

    def rows(self, *, hours: float = 24.0) -> list[dict]:
        cutoff = time.time() - hours * 3600
        out: list[dict] = []
        try:
            with self._path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        if float(row.get("heard_epoch") or 0) >= cutoff:
                            out.append(row)
                    except (TypeError, ValueError):
                        continue
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("Could not read %s: %s", self._path, exc)
        return out


def _f(row: dict, key: str) -> Optional[float]:
    try:
        value = row.get(key)
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def summarise(rows: Iterable[dict], *, fallback_interval: float) -> list[Reliability]:
    """Per-origin delivery rate over the rows given.

    Expected is derived from the span actually covered and the sender's own
    interval, not from a sequence range: a peer that reboots restarts its
    counter, and counting the gap as loss would slander a healthy link.
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        origin = (row.get("origin") or "").strip()
        if origin:
            grouped.setdefault(origin, []).append(row)

    results: list[Reliability] = []
    for origin, entries in grouped.items():
        sent_times = sorted(t for t in (_f(r, "sent_epoch") for r in entries) if t)
        if not sent_times:
            continue

        intervals = [i for i in (_f(r, "interval") for r in entries) if i]
        interval = statistics.median(intervals) if intervals else fallback_interval
        interval = interval or fallback_interval or 300.0

        span = sent_times[-1] - sent_times[0]
        # One beacon covers no span, so a single sighting is 1 of 1.
        expected = max(1, round(span / interval) + 1)
        heard = len(entries)

        snrs = [s for s in (_f(r, "snr") for r in entries) if s is not None]
        results.append(
            Reliability(
                origin=origin,
                heard=heard,
                expected=max(expected, heard),
                first=sent_times[0],
                last=sent_times[-1],
                snr=statistics.median(snrs) if snrs else None,
                interval=interval,
            )
        )

    results.sort(key=lambda r: r.rate, reverse=True)
    return results


def bar(rate: float, width: int = 10) -> str:
    filled = int(round(rate / 100.0 * width))
    return "█" * filled + "░" * (width - filled)

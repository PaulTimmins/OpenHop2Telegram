"""Spot wardrivers announcing themselves on a channel.

Channel messages carry no sender public key — unlike direct messages, the
payload is just `channel_idx`, `text` and timing — so a wardriver can only be
identified from the text it sends.

The on-air #wardriving format isn't formally specified: MeshMapper describes it
as a short anonymous token, with GPS coordinates appended only when the operator
turns on "Broadcast My Coordinates". So the parser here is deliberately
tolerant — it pulls a trailing coordinate pair when one is present and treats
the rest as the sender's label — and `WARDRIVING_PATTERN` lets you replace it
with a regex once you've seen what your local traffic actually looks like.

Every message is logged raw at debug level for exactly that purpose.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("relay.wardriving")

# A trailing "lat lon" or "lat, lon" pair. Anchored to the end so a number in
# the middle of a name isn't mistaken for a position.
_COORD_RE = re.compile(
    r"[\s,(\[]+(?P<lat>[-+]?\d{1,3}\.\d+)\s*[,\s]\s*(?P<lon>[-+]?\d{1,3}\.\d+)"
    r"\s*[)\]]?\s*$"
)

# A trailing "(token)" or "[token]" after the name.
_TAG_RE = re.compile(r"^(?P<name>.*?)[\s]*[\(\[](?P<ident>[^)\]]+)[\)\]]\s*$")

# Names/labels longer than this are almost certainly not an identity.
_MAX_LABEL = 64


@dataclass
class Sighting:
    """One wardriver announcement parsed out of a channel message."""

    name: str
    ident: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    raw: str = ""

    @property
    def key(self) -> str:
        """Stable identity used for the quiet-period check."""
        return (self.ident or self.name).strip().lower()

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


def _valid_position(lat: float, lon: float) -> bool:
    # Reject out-of-range pairs rather than guessing at lon/lat ordering.
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def parse_sighting(text: str, pattern: Optional[re.Pattern] = None) -> Optional[Sighting]:
    """Turn a channel message into a Sighting, or None if it isn't one."""
    raw = (text or "").strip()
    if not raw:
        return None

    if pattern is not None:
        match = pattern.search(raw)
        if not match:
            return None
        groups = match.groupdict()
        lat, lon = _floats(groups.get("lat"), groups.get("lon"))
        name = (groups.get("name") or "").strip()
        ident = (groups.get("id") or groups.get("ident") or "").strip()
        if not (name or ident):
            return None
        return Sighting(name or ident, ident or name, lat, lon, raw)

    body = raw
    lat = lon = None

    coords = _COORD_RE.search(body)
    if coords:
        parsed_lat, parsed_lon = _floats(coords.group("lat"), coords.group("lon"))
        if (
            parsed_lat is not None
            and parsed_lon is not None
            and _valid_position(parsed_lat, parsed_lon)
        ):
            lat, lon = parsed_lat, parsed_lon
            body = body[: coords.start()].strip()
        # An out-of-range pair stays part of the label rather than being
        # reported as a position we don't actually have.

    body = body.strip(" ,;:-\t")
    if not body or len(body) > _MAX_LABEL:
        return None

    name, ident = body, body
    tagged = _TAG_RE.match(body)
    if tagged:
        tag_name = tagged.group("name").strip()
        tag_ident = tagged.group("ident").strip()
        if tag_name and tag_ident:
            name, ident = tag_name, tag_ident

    return Sighting(name, ident, lat, lon, raw)


def _floats(*values: Any) -> tuple:
    out = []
    for value in values:
        try:
            out.append(float(value)) if value not in (None, "") else out.append(None)
        except (TypeError, ValueError):
            out.append(None)
    return tuple(out)


def format_alert(sighting: Sighting) -> str:
    """The notification text. Coordinates are omitted when not broadcast."""
    line = f"\U0001F6F0 Wardriver seen: {sighting.name}"
    if sighting.ident and sighting.ident != sighting.name:
        line += f" ({sighting.ident})"
    if sighting.has_position:
        line += f" {sighting.lat} {sighting.lon}"
    return line


class WardriverLog:
    """When each wardriver was last heard, persisted across restarts."""

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._seen: dict[str, float] = {}

    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            seen = raw.get("last_seen")
            if isinstance(seen, dict):
                self._seen = {
                    str(k): float(v)
                    for k, v in seen.items()
                    if isinstance(v, (int, float))
                }
            log.info("Loaded %d wardriver(s) from %s", len(self._seen), self._path)
        except FileNotFoundError:
            log.info("No wardriver log at %s yet", self._path)
        except (json.JSONDecodeError, OSError, AttributeError, TypeError) as exc:
            log.warning("Could not read %s (%s); starting fresh", self._path, exc)

    def __len__(self) -> int:
        return len(self._seen)

    def last_seen(self, key: str) -> Optional[float]:
        return self._seen.get(key)

    def is_new_activity(self, key: str, quiet_seconds: float, now: Optional[float] = None) -> bool:
        """True when this wardriver hasn't been heard for the quiet period.

        A continuous conversation stays quiet; a return after the gap is news.
        """
        now = time.time() if now is None else now
        previous = self._seen.get(key)
        return previous is None or (now - previous) >= quiet_seconds

    def record(self, key: str, now: Optional[float] = None) -> None:
        self._seen[key] = time.time() if now is None else now
        self._save()

    def prune(self, older_than: float, now: Optional[float] = None) -> int:
        """Drop entries past their usefulness so the file can't grow forever."""
        now = time.time() if now is None else now
        stale = [k for k, ts in self._seen.items() if now - ts > older_than]
        for key in stale:
            del self._seen[key]
        if stale:
            self._save()
        return len(stale)

    def _save(self) -> None:
        payload = json.dumps(
            {"version": self.VERSION, "last_seen": self._seen}, indent=0, sort_keys=True
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("Could not persist wardriver log to %s: %s", self._path, exc)

"""Tracking of which mesh nodes we have already announced.

The node list is persisted so a restart doesn't re-announce nodes we have
already told the chat about, and so the contacts the node already knows on
first run are recorded silently instead of arriving as a burst of alerts.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

log = logging.getLogger("relay.nodes")

# Index matches the `type` byte on a contact record; see CONTACT_TYPENAMES in
# meshcore's parser ("NONE", "CLI", "REP", "ROOM", "SENS").
TYPE_CODES = {0: "NONE", 1: "CLI", 2: "REP", 3: "ROOM", 4: "SENS"}

TYPE_LABELS = {
    "NONE": "node",
    "CLI": "companion",
    "REP": "repeater",
    "ROOM": "room server",
    "SENS": "sensor",
}

TYPE_EMOJI = {
    "NONE": "\U0001F4E1",  # satellite dish
    "CLI": "\U0001F4F1",  # mobile phone
    "REP": "\U0001F5FC",  # tower
    "ROOM": "\U0001F3E0",  # house
    "SENS": "\U0001F321",  # thermometer
}


def type_code(contact: dict) -> str:
    """Short code ("REP", "CLI", ...) for a contact record."""
    return TYPE_CODES.get(contact.get("type"), "NONE")


def describe(contact: dict) -> str:
    """Human-readable one-liner for a newly seen node."""
    code = type_code(contact)
    label = TYPE_LABELS.get(code, "node")
    emoji = TYPE_EMOJI.get(code, "\U0001F4E1")

    name = (contact.get("adv_name") or "").strip() or "(unnamed)"
    pubkey = (contact.get("public_key") or "")[:6]

    line = f"{emoji} New {label} seen: {name}"
    if pubkey:
        line += f" ({pubkey})"

    lat = contact.get("adv_lat") or 0
    lon = contact.get("adv_lon") or 0
    # Nodes with location sharing off advertise 0,0 — don't report that as a fix.
    if lat or lon:
        line += f"\n\U0001F4CD {lat:.5f}, {lon:.5f}"

    return line


class SeenNodes:
    """A persistent set of public keys we have already announced."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._keys: set[str] = set()
        self._loaded = False

    def load(self) -> None:
        """Read the store from disk. A missing or corrupt file starts empty."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._keys = {str(k) for k in raw.get("seen", [])}
            log.info("Loaded %d known node(s) from %s", len(self._keys), self._path)
        except FileNotFoundError:
            log.info("No node store at %s yet; starting fresh", self._path)
        except (json.JSONDecodeError, OSError, AttributeError) as exc:
            # Losing this file only costs us duplicate announcements, so a bad
            # read should never stop the relay from starting.
            log.warning("Could not read %s (%s); starting fresh", self._path, exc)
        self._loaded = True

    def __contains__(self, pubkey: str) -> bool:
        return pubkey in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def is_empty(self) -> bool:
        return not self._keys

    def add(self, pubkey: str) -> bool:
        """Record a key. Returns True if it was new."""
        if not pubkey or pubkey in self._keys:
            return False
        self._keys.add(pubkey)
        self._save()
        return True

    def seed(self, pubkeys: Iterable[str]) -> int:
        """Record keys without announcing them. Returns how many were added."""
        new = {k for k in pubkeys if k} - self._keys
        if new:
            self._keys |= new
            self._save()
        return len(new)

    def _save(self) -> None:
        payload = json.dumps({"seen": sorted(self._keys)}, indent=0)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file in the same directory, then rename, so an
            # interrupted write can't truncate the existing store.
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
            except BaseException:
                # Don't leave a stray temp file behind on failure.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("Could not persist node store to %s: %s", self._path, exc)

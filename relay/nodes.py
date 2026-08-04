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
import time
from pathlib import Path
from typing import Any, Optional

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
    """A persistent record of the nodes we know about.

    Stores what each node is, not just its key, so the file is readable and can
    be reused by the clock checker and metrics collector.

    Version 1 of this file was a bare list of public keys. Those are migrated on
    load into entries with unknown names, which fill in as the nodes advertise
    again.
    """

    VERSION = 2

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._nodes: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> None:
        """Read the store from disk. A missing or corrupt file starts empty."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._nodes = self._parse(raw)
            log.info("Loaded %d known node(s) from %s", len(self._nodes), self._path)
        except FileNotFoundError:
            log.info("No node store at %s yet; starting fresh", self._path)
        except (json.JSONDecodeError, OSError, AttributeError, TypeError) as exc:
            # Losing this file only costs us duplicate announcements, so a bad
            # read should never stop the relay from starting.
            log.warning("Could not read %s (%s); starting fresh", self._path, exc)
        self._loaded = True

    @classmethod
    def _parse(cls, raw: Any) -> dict[str, dict]:
        nodes = raw.get("nodes")
        if isinstance(nodes, dict):
            return {str(k): dict(v) for k, v in nodes.items() if isinstance(v, dict)}

        # v1: {"seen": ["<pubkey>", ...]} — keep the keys, names unknown.
        legacy = raw.get("seen")
        if isinstance(legacy, list):
            log.info("Migrating %d node(s) from the v1 store format", len(legacy))
            return {str(k): {"name": "", "type": "", "first_seen": None} for k in legacy}
        return {}

    def __contains__(self, pubkey: str) -> bool:
        return pubkey in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def is_empty(self) -> bool:
        return not self._nodes

    @property
    def nodes(self) -> dict[str, dict]:
        """The stored records, keyed by full public key."""
        return dict(self._nodes)

    def get(self, pubkey: str) -> Optional[dict]:
        return self._nodes.get(pubkey)

    def find(self, needle: str) -> Optional[tuple[str, dict]]:
        """Look a node up by key prefix or by name, case-insensitively."""
        needle = (needle or "").strip().lower()
        if not needle:
            return None
        for key, record in self._nodes.items():
            if key.lower().startswith(needle):
                return key, record
        for key, record in self._nodes.items():
            if (record.get("name") or "").strip().lower() == needle:
                return key, record
        return None

    def add(self, pubkey: str, contact: Optional[dict] = None) -> bool:
        """Record a node. Returns True if it was new."""
        if not pubkey:
            return False
        if pubkey in self._nodes:
            # Known already, but a fresh advert may carry better details.
            if contact and self._merge(self._nodes[pubkey], contact, seen_now=True):
                self._save()
            return False
        self._nodes[pubkey] = self._record(contact, first_seen=time.time())
        self._save()
        return True

    def seed(self, contacts: Any) -> int:
        """Record existing contacts without announcing them.

        Accepts the node's contacts mapping (pubkey -> record) or a bare iterable
        of keys. Returns how many were added.
        """
        if isinstance(contacts, dict):
            items = list(contacts.items())
        else:
            items = [(k, None) for k in (contacts or [])]

        added = 0
        changed = False
        for key, contact in items:
            if not key:
                continue
            if key in self._nodes:
                if contact and self._merge(self._nodes[key], contact, seen_now=False):
                    changed = True
                continue
            # first_seen stays None: the node predates our tracking, so claiming
            # we first saw it now would be a lie in the data.
            self._nodes[key] = self._record(contact, first_seen=None)
            added += 1
        if added or changed:
            self._save()
        return added

    @staticmethod
    def _record(contact: Optional[dict], first_seen: Optional[float]) -> dict:
        contact = contact or {}
        return {
            "name": (contact.get("adv_name") or "").strip(),
            "type": TYPE_CODES.get(contact.get("type"), ""),
            "first_seen": int(first_seen) if first_seen else None,
            "last_seen": int(time.time()),
            "last_advert": contact.get("last_advert") or None,
            "lat": contact.get("adv_lat") or None,
            "lon": contact.get("adv_lon") or None,
        }

    @staticmethod
    def _merge(record: dict, contact: dict, *, seen_now: bool) -> bool:
        """Fill in or refresh details on an existing record."""
        changed = False
        name = (contact.get("adv_name") or "").strip()
        if name and record.get("name") != name:
            record["name"] = name
            changed = True
        code = TYPE_CODES.get(contact.get("type"), "")
        if code and record.get("type") != code:
            record["type"] = code
            changed = True
        for src, dst in (("last_advert", "last_advert"), ("adv_lat", "lat"), ("adv_lon", "lon")):
            value = contact.get(src)
            if value and record.get(dst) != value:
                record[dst] = value
                changed = True
        if seen_now:
            record["last_seen"] = int(time.time())
            changed = True
        return changed

    def _save(self) -> None:
        payload = json.dumps(
            {"version": self.VERSION, "nodes": self._nodes}, indent=1, sort_keys=True
        )
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

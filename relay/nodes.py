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

# Compared structurally so this module needn't import meshcore just for it.
try:  # pragma: no cover - trivial import guard
    from meshcore import EventType as _EventType

    EVENT_ERROR = _EventType.ERROR
except Exception:  # pragma: no cover
    EVENT_ERROR = object()

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

    VERSION = 3

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._nodes: dict[str, dict] = {}
        self._loaded = False
        # True when a store exists but we couldn't read or write it. That is
        # very different from having no store yet, and must not be mistaken for
        # a first run — see load().
        self.unusable_reason: Optional[str] = None
        self._warned_write = False

    def load(self) -> None:
        """Read the store from disk.

        A missing file is a genuine first run. A file we cannot read is not:
        treating the two alike would silently re-seed every contact on every
        start and swallow nodes that appeared while we were down, which is
        exactly the failure this flag exists to make visible.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._nodes = self._parse(raw)
            log.info("Loaded %d known node(s) from %s", len(self._nodes), self._path)
        except FileNotFoundError:
            log.info("No node store at %s yet; starting fresh", self._path)
        except PermissionError as exc:
            self.unusable_reason = (
                f"cannot read {self._path} ({exc.strerror}). It is probably owned "
                f"by another user — running the scripts as root recreates these "
                f"files as root-owned. New-node alerts will be unreliable until "
                f"the daemon can read and write it."
            )
            log.error("%s", self.unusable_reason)
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            # Corrupt content: the file is ours, it's just unreadable as JSON.
            # Losing it only costs duplicate announcements.
            log.warning("Could not parse %s (%s); starting fresh", self._path, exc)
        except OSError as exc:
            self.unusable_reason = f"cannot read {self._path} ({exc})"
            log.error("%s", self.unusable_reason)
        else:
            self._check_writable()
        self._loaded = True

    def _check_writable(self) -> None:
        """Confirm we can actually persist, before relying on it.

        A store that reads fine but can't be written back would look healthy
        while quietly losing every update.
        """
        try:
            probe = self._path.parent / f".{self._path.name}.wtest"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            self.unusable_reason = (
                f"cannot write next to {self._path} ({exc}). Updates will be "
                f"lost, so nodes may be announced again after a restart."
            )
            log.error("%s", self.unusable_reason)

    @classmethod
    def _parse(cls, raw: Any) -> dict[str, dict]:
        nodes = raw.get("nodes")
        if isinstance(nodes, dict):
            parsed = {
                str(k): dict(v) for k, v in nodes.items() if isinstance(v, dict)
            }
            if int(raw.get("version") or 0) < 3:
                # Up to v2, last_seen was stamped whenever a node was recorded,
                # including from the node's contact list, so it says nothing
                # about the node still being alive. Drop it and fall back to
                # last_advert; real observations will repopulate it.
                cleared = 0
                for record in parsed.values():
                    if record.pop("last_seen", None) is not None:
                        cleared += 1
                if cleared:
                    log.info(
                        "Upgrading node store: cleared %d unreliable last_seen "
                        "value(s); recency now comes from advert data",
                        cleared,
                    )
            return parsed

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

    @staticmethod
    def recency(record: dict) -> float:
        """When this node was last known to have advertised.

        Both inputs are advert evidence: `last_advert` is the node's own record
        of the contact's last advert, and `last_seen` is our timestamp from
        actually observing one. Neither is written merely because a node appears
        in a contact list, or a dead node would look alive forever and never be
        pruned.

        A node with a fast clock can report an advert time in the future, which
        would otherwise make it permanently the freshest thing in the store, so
        those are ignored.
        """
        horizon = time.time() + 3600
        best = 0.0
        for field in ("last_seen", "last_advert"):
            value = record.get(field)
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value > horizon:
                continue
            best = max(best, value)
        return best

    def find(self, needle: str) -> Optional[tuple[str, dict]]:
        """Look a node up by key prefix or by name, case-insensitively.

        Names are not unique: reflashing a node gives it a new keypair while the
        operator keeps the same name, leaving a live entry and a dead one. When
        several match, the most recently heard wins — the stale key would just
        time out.
        """
        needle = (needle or "").strip().lower()
        if not needle:
            return None

        matches = [
            (key, record)
            for key, record in self._nodes.items()
            if key.lower().startswith(needle)
        ]
        if not matches:
            matches = [
                (key, record)
                for key, record in self._nodes.items()
                if (record.get("name") or "").strip().lower() == needle
            ]
        if not matches:
            return None

        # A superseded entry is a dead key kept only so it isn't re-announced;
        # never resolve a name to it while a live match exists.
        live = [kv for kv in matches if not kv[1].get("superseded_by")]
        if live:
            matches = live

        if len(matches) > 1:
            matches.sort(key=lambda kv: self.recency(kv[1]), reverse=True)
            log.info(
                "%r matches %d nodes; using the most recent (%s)",
                needle,
                len(matches),
                matches[0][0][:12],
            )
        return matches[0]

    def dedupe_by_name(self, min_gap: float = 86400.0) -> int:
        """Mark older duplicates of a name as superseded by the newest.

        A name collision almost always means the node was reflashed: the old key
        is dead and will never advertise again, but it still shadows the live one
        in name lookups.

        The stale entry is *marked*, not deleted. The node's own contact list
        still holds that key, so removing it from the store would make the next
        reconciliation see an unknown contact and announce the dead node as new
        — then mark it again on the following start, once per restart forever.
        Keeping the record means it stays known; `find` just stops choosing it.

        Only duplicates at least `min_gap` seconds behind the survivor are
        marked. Two nodes both currently active genuinely are two nodes, and
        `find` picking the most recent handles that ambiguity on its own.
        """
        by_name: dict[str, list[str]] = {}
        for key, record in self._nodes.items():
            name = (record.get("name") or "").strip().lower()
            if name:
                by_name.setdefault(name, []).append(key)

        dropped = 0
        for name, keys in by_name.items():
            if len(keys) < 2:
                continue
            keys.sort(key=lambda k: self.recency(self._nodes[k]), reverse=True)
            keep = keys[0]
            newest = self.recency(self._nodes[keep])
            stale = [
                k
                for k in keys[1:]
                if newest - self.recency(self._nodes[k]) >= min_gap
                and self._nodes[k].get("superseded_by") != keep
            ]
            if not stale:
                log.debug(
                    "%r maps to %d nodes; nothing new to supersede", name, len(keys)
                )
                continue
            log.info(
                "%r resolves to %s; superseding %d stale duplicate(s): %s",
                name,
                keep[:12],
                len(stale),
                ", ".join(k[:12] for k in stale),
            )
            for key in stale:
                self._nodes[key]["superseded_by"] = keep
                dropped += 1

        if dropped:
            self._save()
        return dropped

    def forget(self, pubkey: str) -> bool:
        """Drop a record entirely. Only safe once the node no longer holds it."""
        if pubkey not in self._nodes:
            return False
        del self._nodes[pubkey]
        self._save()
        return True

    def superseded(self) -> dict[str, dict]:
        """Records marked as replaced by a newer key of the same name."""
        return {k: v for k, v in self._nodes.items() if v.get("superseded_by")}

    def add(
        self, pubkey: str, contact: Optional[dict] = None, *, heard: bool = True
    ) -> bool:
        """Record a node. Returns True if it was new.

        `heard` says whether this came from actually observing an advert. Pass
        False when the node merely turned up in the contact list, so a dead node
        found there isn't recorded as freshly alive.
        """
        if not pubkey:
            return False
        if pubkey in self._nodes:
            # Known already, but a fresh advert may carry better details.
            if contact and self._merge(self._nodes[pubkey], contact, seen_now=heard):
                self._save()
            return False
        self._nodes[pubkey] = self._record(
            contact, first_seen=time.time() if heard else None, heard=heard
        )
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
    def _record(
        contact: Optional[dict],
        first_seen: Optional[float],
        *,
        heard: bool = False,
    ) -> dict:
        contact = contact or {}
        return {
            "name": (contact.get("adv_name") or "").strip(),
            "type": TYPE_CODES.get(contact.get("type"), ""),
            "first_seen": int(first_seen) if first_seen else None,
            # Only set when we actually observed an advert. Being present in the
            # node's contact list is not evidence the node is still alive.
            "last_seen": int(time.time()) if heard else None,
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
            if not self._warned_write:
                self._warned_write = True
                self.unusable_reason = f"cannot write {self._path} ({exc})"
                log.error(
                    "Could not persist the node store to %s (%s). Everything "
                    "learned this session will be lost on restart.",
                    self._path,
                    exc,
                )
            else:
                log.debug("Node store still unwritable: %s", exc)


async def remove_superseded_contacts(
    mesh: Any,
    store: SeenNodes,
    *,
    dry_run: bool = False,
    older_than: float = 14 * 86400,
) -> list[tuple[str, str, str]]:
    """Ask the node to forget conflicting contacts that went silent weeks ago.

    Marking a duplicate superseded stops it winning name lookups here, but the
    dead key stays in the node's contact list, where it keeps shadowing the live
    one for anything that asks the node directly. Removing it there is the
    actual cleanup.

    Two conditions must both hold, because either alone would be too eager:

    * the key is **superseded** — a same-named key at least a day newer exists,
      so this is a real conflict rather than an idle node; and
    * it hasn't been heard from in `older_than` seconds, so a node that is
      merely quiet for a few days is left alone.

    A successful removal also drops the record, since the node can no longer
    offer the key back and re-announce it as new.

    Returns (pubkey, name, outcome) for each one considered.
    """
    if older_than <= 0:
        return []
    contacts = getattr(mesh, "contacts", None)
    if not isinstance(contacts, dict):
        log.warning("No contact list available; not removing anything")
        return []

    remove = getattr(getattr(mesh, "commands", None), "remove_contact", None)
    if remove is None:
        log.warning("This meshcore version can't remove contacts")
        return []

    now = time.time()
    results: list[tuple[str, str, str]] = []
    for pubkey, record in store.superseded().items():
        name = record.get("name") or pubkey[:12]
        if pubkey not in contacts:
            log.debug("%s (%s): already gone from the node", name, pubkey[:12])
            continue

        # A conflicting key that is still transmitting is not ours to delete.
        age = now - store.recency(record)
        if age < older_than:
            log.debug(
                "%s (%s): conflicting but heard %.1f days ago; leaving it",
                name,
                pubkey[:12],
                age / 86400,
            )
            continue

        if dry_run:
            log.info(
                "Would ask the node to forget %s (%s): superseded by %s and "
                "silent for %.0f days",
                name,
                pubkey[:12],
                (record.get("superseded_by") or "")[:12],
                age / 86400,
            )
            results.append((pubkey, name, "would remove"))
            continue

        try:
            result = await remove(contacts[pubkey])
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            log.warning("Could not remove %s (%s): %s", name, pubkey[:12], exc)
            results.append((pubkey, name, f"failed: {exc}"))
            continue

        if getattr(result, "type", None) == EVENT_ERROR:
            log.warning(
                "Node refused to remove %s (%s): %s",
                name,
                pubkey[:12],
                getattr(result, "payload", None),
            )
            results.append((pubkey, name, "refused"))
            continue

        log.info(
            "Node forgot %s (%s): superseded and silent for %.0f days",
            name,
            pubkey[:12],
            age / 86400,
        )
        # Keep the in-memory contact list honest: anything still holding the
        # removed key would try to address a contact the node no longer has.
        contacts.pop(pubkey, None)
        store.forget(pubkey)
        results.append((pubkey, name, "removed"))

    return results

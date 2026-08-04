#!/usr/bin/env python3
"""Show what the nodes in the node store actually are.

Reads seen_nodes.json and, unless --offline, joins it against the node's live
contact list so names and types are filled in even for keys recorded before the
store kept metadata. Also flags which nodes the clock checker is configured for.

    python3 scripts/list_nodes.py                 # table of known nodes
    python3 scripts/list_nodes.py --offline       # store only, no connection
    python3 scripts/list_nodes.py --json          # machine-readable
    python3 scripts/list_nodes.py --time-sync-template > nodes.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from relay.config import Config  # noqa: E402
from relay.nodes import TYPE_LABELS, SeenNodes, type_code  # noqa: E402

log = logging.getLogger("list_nodes")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--store", default=None, help="path to the node store")
    p.add_argument(
        "--time-sync-config",
        default="time_sync.json",
        help="checked to show which nodes are already configured",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="don't connect; show only what the store already holds",
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument(
        "--time-sync-template",
        action="store_true",
        help="emit a time_sync.json 'nodes' block for every node found",
    )
    p.add_argument(
        "--host",
        default=None,
        help="companion host (default: TIMESYNC_HOST, else OPENHOP_HOST)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="companion port (default: TIMESYNC_PORT, else OPENHOP_PORT)",
    )
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args()


def ago(epoch) -> str:
    if not epoch:
        return "-"
    delta = int(time.time()) - int(epoch)
    if delta < 0:
        return "in the future"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if delta >= size:
            return f"{delta // size}{unit} ago"
    return f"{delta}s ago"


async def live_contacts(host: str, port: int) -> dict:
    """Fetch the node's contact list, keyed by public key."""
    from meshcore import MeshCore

    log.info("Connecting to %s:%s", host, port)
    mesh = await MeshCore.create_tcp(host, port)
    try:
        await mesh.ensure_contacts()
        contacts = getattr(mesh, "contacts", None)
        if not isinstance(contacts, dict):
            result = await mesh.commands.get_contacts()
            payload = getattr(result, "payload", None)
            contacts = payload if isinstance(payload, dict) else {}
        return dict(contacts)
    finally:
        await mesh.disconnect()


def configured_names(path: str) -> set[str]:
    """Names and key prefixes already present in the clock-checker config."""
    file = pathlib.Path(path)
    if not file.exists():
        return set()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    out: set[str] = set()
    for node in raw.get("nodes", []):
        if isinstance(node, dict):
            for key in ("name", "pubkey"):
                value = str(node.get(key, "")).strip().lower()
                if value:
                    out.add(value)
    return out


def merge(store: SeenNodes, contacts: dict) -> list[dict]:
    """Combine stored records with live contact data."""
    rows: list[dict] = []
    keys = set(store.nodes) | set(contacts)

    for key in keys:
        record = store.get(key) or {}
        contact = contacts.get(key) or {}

        name = (contact.get("adv_name") or "").strip() or (record.get("name") or "")
        code = type_code(contact) if contact else (record.get("type") or "")
        rows.append(
            {
                "pubkey": key,
                "name": name,
                "type": code,
                "type_label": TYPE_LABELS.get(code, "node" if code else ""),
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
                "last_advert": contact.get("last_advert") or record.get("last_advert"),
                "lat": contact.get("adv_lat") or record.get("lat"),
                "lon": contact.get("adv_lon") or record.get("lon"),
                "in_store": key in store,
                "on_node": key in contacts,
            }
        )

    # Infrastructure first, then by name, so repeaters are easy to pick out.
    order = {"REP": 0, "ROOM": 1, "SENS": 2, "CLI": 3, "": 4}
    rows.sort(key=lambda r: (order.get(r["type"], 9), (r["name"] or "").lower()))
    return rows


def print_table(rows: list[dict], configured: set[str]) -> None:
    if not rows:
        print("No nodes known yet. The relay records them as they advertise.")
        return

    print(
        f"{'NAME':<26} {'TYPE':<12} {'KEY':<14} {'LAST ADVERT':<12} "
        f"{'WHERE':<10} CLOCK CFG"
    )
    print("-" * 92)
    for r in rows:
        where = {
            (True, True): "both",
            (True, False): "store",
            (False, True): "node",
        }[(r["in_store"], r["on_node"])]
        known = (r["name"] or "").strip().lower() in configured or any(
            r["pubkey"].startswith(c) for c in configured if c
        )
        print(
            f"{(r['name'] or '(unnamed)')[:25]:<26} "
            f"{(r['type_label'] or '?')[:11]:<12} "
            f"{r['pubkey'][:12]:<14} "
            f"{ago(r['last_advert']):<12} "
            f"{where:<10} "
            f"{'yes' if known else '-'}"
        )

    print(f"\n{len(rows)} node(s).")
    unconfigured = [
        r
        for r in rows
        if r["type"] in ("REP", "ROOM")
        and (r["name"] or "").strip().lower() not in configured
    ]
    if unconfigured:
        print(
            f"{len(unconfigured)} repeater/room server(s) not in the clock config. "
            f"Use --time-sync-template to generate entries."
        )


def print_template(rows: list[dict]) -> None:
    """A time_sync.json nodes block, ready to paste and edit."""
    nodes = []
    for r in rows:
        entry: dict = {}
        if r["name"]:
            entry["name"] = r["name"]
        else:
            entry["pubkey"] = r["pubkey"][:12]
        # Only infrastructure is worth managing a clock on; companions come and
        # go. set_time stays false so nothing is changed until it's reviewed.
        entry["password"] = "CHANGEME" if r["type"] in ("REP", "ROOM") else ""
        entry["set_time"] = False
        entry["_type"] = r["type_label"] or "unknown"
        nodes.append(entry)
    print(json.dumps({"tolerance_seconds": 120, "nodes": nodes}, indent=2))


async def run(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    store = SeenNodes(args.store or cfg.seen_nodes_file)
    store.load()

    contacts: dict = {}
    if not args.offline:
        try:
            contacts = await live_contacts(
                args.host or cfg.timesync_host, args.port or cfg.timesync_port
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Could not reach the node ({exc}); showing the store only.",
                file=sys.stderr,
            )

    rows = merge(store, contacts)

    if args.json:
        print(json.dumps(rows, indent=2))
    elif args.time_sync_template:
        print_template(rows)
    else:
        print_table(rows, configured_names(args.time_sync_config))
    return 0


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()

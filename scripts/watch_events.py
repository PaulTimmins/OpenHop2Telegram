#!/usr/bin/env python3
"""Dump every event a MeshCore node emits, with the channel table.

For answering "did that message arrive, and on what channel index" without
guessing. Subscribes to everything rather than the handful of event types the
relay cares about, so an event arriving under a different type or index is
visible rather than silently unhandled.

    python3 scripts/watch_events.py --port 5002
    python3 scripts/watch_events.py --port 5002 --only channel_message

Use a different endpoint from the running relay, or --pause it: two companion
clients on one endpoint split the node's message queue between them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from meshcore import EventType, MeshCore  # noqa: E402

from relay.config import Config  # noqa: E402
from relay.coordination import Coordinator  # noqa: E402
from relay.logging_setup import configure  # noqa: E402

# Printed compactly when present; everything else is dumped as JSON.
INTERESTING = (
    "channel_idx",
    "text",
    "pubkey_prefix",
    "SNR",
    "RSSI",
    "path_len",
    "txt_type",
    "public_key",
    "adv_name",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument(
        "--only",
        default="",
        help="comma-separated event value(s) to show, e.g. channel_message",
    )
    p.add_argument(
        "--seconds", type=float, default=0.0, help="stop after this long (0 = forever)"
    )
    p.add_argument(
        "--pause",
        action="store_true",
        help="ask the running relay to release the node first. OpenHop's "
        "companion server takes one client per endpoint, so without this a "
        "watcher on the relay's port is dropped straight away.",
    )
    p.add_argument("--pause-timeout", type=float, default=90.0)
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args()


async def show_channels(mesh: MeshCore) -> None:
    """The index -> name map, which is what an index in an event means."""
    print("Channels on this node:")
    found = False
    for idx in range(16):
        try:
            result = await mesh.commands.get_channel(idx)
        except Exception as exc:  # noqa: BLE001
            print(f"  {idx}: <error {exc}>")
            break
        if getattr(result, "type", None) == EventType.ERROR:
            continue
        payload = result.payload or {}
        name = (payload.get("channel_name") or "").strip()
        if name:
            found = True
            print(f"  index {idx}: {name!r}  (hash {payload.get('channel_hash')})")
    if not found:
        print("  (none reported)")
    print()


async def main() -> int:
    args = parse_args()
    configure(args.log_level)
    cfg = Config.from_env()

    host = args.host or cfg.timesync_host
    port = args.port or cfg.timesync_port
    wanted = {w.strip().lower() for w in args.only.split(",") if w.strip()}

    coord = Coordinator(cfg.lock_dir)
    paused = False
    if args.pause:
        pid = coord.relay_pid()
        if pid is None:
            print("No relay is holding the node; connecting directly.")
        else:
            print(f"Asking the relay (pid {pid}) to release the node …")
            coord.request_pause()
            waited = 0.0
            while waited < args.pause_timeout and not coord.relay_has_released():
                await asyncio.sleep(0.5)
                waited += 0.5
            if not coord.relay_has_released():
                coord.release_request()
                print(
                    f"Relay didn't release within {args.pause_timeout:.0f}s.",
                    file=sys.stderr,
                )
                return 1
            paused = True
            print(f"Released after {waited:.0f}s.\n")
    elif coord.relay_pid() and (host, port) == (cfg.openhop_host, cfg.openhop_port):
        print(
            "WARNING: the relay is connected on this endpoint. OpenHop allows "
            "one companion client per port, so this will be dropped "
            "immediately. Use --pause, or a different --port.\n",
            file=sys.stderr,
        )

    print(f"Connecting to {host}:{port} …")
    try:
        mesh = await MeshCore.create_tcp(host, port)
    except Exception:
        if paused:
            coord.release_request()
        raise
    try:
        await mesh.start_auto_message_fetching()
        await show_channels(mesh)

        seen: dict[str, int] = {}

        async def on_any(event) -> None:
            kind = getattr(getattr(event, "type", None), "value", str(event.type))
            if kind == "disconnected":
                # Worth calling out: on this endpoint it usually means another
                # companion client took the single slot.
                print(
                    "\n*** disconnected — another companion client probably "
                    "took the slot on this endpoint ***"
                )
            seen[kind] = seen.get(kind, 0) + 1
            if wanted and kind not in wanted:
                return

            payload = getattr(event, "payload", None)
            stamp = time.strftime("%H:%M:%S")
            if isinstance(payload, dict):
                brief = {k: payload[k] for k in INTERESTING if k in payload}
                extra = {k: v for k, v in payload.items() if k not in INTERESTING}
                line = ", ".join(f"{k}={v!r}" for k, v in brief.items())
                print(f"[{stamp}] {kind}: {line}")
                if extra:
                    print(f"           other: {json.dumps(extra, default=str)[:200]}")
            else:
                print(f"[{stamp}] {kind}: {payload!r}")

        # None = every event type, so nothing is missed by not asking for it.
        mesh.subscribe(None, on_any)
        print("Watching. Send something on the channel now. Ctrl-C to stop.\n")

        if args.seconds > 0:
            await asyncio.sleep(args.seconds)
        else:
            await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if seen:
            print("\nEvent counts:")
            for kind, count in sorted(seen.items(), key=lambda kv: -kv[1]):
                print(f"  {count:5d}  {kind}")
        try:
            await mesh.stop_auto_message_fetching()
        except Exception:  # noqa: BLE001
            pass
        try:
            await mesh.disconnect()
        finally:
            if paused:
                coord.release_request()
                print("Node handed back to the relay.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)

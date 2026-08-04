#!/usr/bin/env python3
"""Check (and optionally fix) the clocks of configured MeshCore nodes.

Meant to be run periodically from a systemd timer or cron, separately from the
relay daemon.

    python3 scripts/sync_node_time.py --dry-run     # report only, change nothing
    python3 scripts/sync_node_time.py               # correct nodes set_time=true
    python3 scripts/sync_node_time.py --notify      # also post a Telegram summary

Nodes and their admin passwords come from a JSON file (default
time_sync.json; see time_sync.example.json). Exit status is 0 when every node
is within tolerance or was corrected, 1 otherwise, so a timer can alert on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from meshcore import MeshCore  # noqa: E402

from relay.config import Config  # noqa: E402
from relay.metrics import MetricsCollector, MetricsWriter  # noqa: E402
from relay.telegram import TelegramClient  # noqa: E402
from relay.timesync import (  # noqa: E402
    NodeTimeSync,
    SyncConfig,
    TimeSyncError,
    summarise,
)

log = logging.getLogger("sync_node_time")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--config",
        default="time_sync.json",
        help="path to the node list (default: time_sync.json)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report drift but never change a clock",
    )
    p.add_argument(
        "--notify",
        action="store_true",
        help="post the summary to the Telegram chat from .env",
    )
    p.add_argument(
        "--quiet-when-ok",
        action="store_true",
        help="with --notify, only message when something needs attention",
    )
    p.add_argument(
        "--metrics",
        metavar="CSV",
        default="metrics.csv",
        help="append battery/temperature/radio metrics to this CSV "
        "(default: metrics.csv)",
    )
    p.add_argument(
        "--no-metrics",
        action="store_true",
        help="skip metric collection; each node costs two extra requests",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def load_sync_config(path: str) -> SyncConfig:
    file = pathlib.Path(path)
    if not file.exists():
        raise SystemExit(
            f"No node list at {file}. Copy time_sync.example.json to {file} "
            f"and list the nodes you want checked."
        )
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{file} is not valid JSON: {exc}")
    try:
        return SyncConfig.from_dict(raw)
    except TimeSyncError as exc:
        raise SystemExit(f"{file}: {exc}")


async def run(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    sync_cfg = load_sync_config(args.config)

    if not sync_cfg.nodes:
        log.info("No nodes configured; nothing to do.")
        return 0

    log.info(
        "Connecting to OpenHop at %s:%s ...", cfg.openhop_host, cfg.openhop_port
    )
    mesh = await MeshCore.create_tcp(cfg.openhop_host, cfg.openhop_port)

    try:
        # Command replies arrive as messages, so they only reach us while
        # message fetching is running.
        await mesh.start_auto_message_fetching()
        # Populates the contact cache that name lookups rely on.
        await mesh.ensure_contacts()

        collector = (
            None
            if args.no_metrics
            else MetricsCollector(MetricsWriter(args.metrics))
        )
        if collector is not None:
            log.info("Logging metrics to %s", args.metrics)

        results = await NodeTimeSync(
            mesh, sync_cfg, dry_run=args.dry_run, metrics=collector
        ).run()
    finally:
        try:
            await mesh.stop_auto_message_fetching()
        except Exception:  # noqa: BLE001
            pass
        await mesh.disconnect()

    report = summarise(results)
    print(report)

    failed = [r for r in results if not r.ok]

    if args.notify and (failed or not args.quiet_when_ok):
        header = "\U0001F551 Node clock check"
        if args.dry_run:
            header += " (dry run)"
        tg = TelegramClient(cfg.telegram_bot_token, cfg.telegram_chat_id)
        try:
            await tg.send_message(f"{header}\n{report}")
        finally:
            await tg.close()

    return 1 if failed else 0


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()

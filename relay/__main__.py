"""Entry point: `python -m relay`."""

from __future__ import annotations

import asyncio
import logging

from .bridge import Bridge
from .config import Config
from .logging_setup import configure


def main() -> None:
    cfg = Config.from_env()
    configure(cfg.log_level)
    try:
        asyncio.run(Bridge(cfg).run())
    except KeyboardInterrupt:
        logging.getLogger("relay").info("Shutting down")


if __name__ == "__main__":
    main()

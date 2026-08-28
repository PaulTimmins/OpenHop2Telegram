"""Logging setup shared by the daemon and the scripts."""

from __future__ import annotations

import logging

# httpx logs every request URL at INFO, and the Telegram bot token is part of
# that URL — so those lines would write the token into the journal, where it
# survives in logs that get shared when asking for help. Nothing below WARNING
# from these libraries is worth that, so they stay quiet regardless of our own
# level, including at DEBUG.
_NOISY = ("httpx", "httpcore")


def configure(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

"""Retry helper for requests that can simply go unanswered.

Over LoRa a lost packet is ordinary, not exceptional, so a single attempt makes
a marginal link look like a dead one. These retries apply only to *silence* —
a node that answers with a refusal has told us something definitive, and asking
again would just spend airtime to hear it a second time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("relay.retrying")


async def until_answered(
    action: Callable[[], Awaitable[Any]],
    *,
    attempts: int,
    delay: float,
    what: str,
    label: str,
    before_retry: Optional[Callable[[int], Awaitable[None]]] = None,
) -> Any:
    """Call `action` until it returns something other than None.

    `action` returns None to mean "no answer, worth another go", and may raise
    to mean "answered, and the answer was final" — that propagates immediately
    rather than being retried.

    `before_retry` runs between attempts, for escalation such as resetting a
    stale route before the last try.
    """
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        result = await action()
        if result is not None:
            if attempt > 1:
                log.info("%s: %s answered on attempt %d", label, what, attempt)
            return result

        if attempt >= attempts:
            break

        log.info(
            "%s: no reply to %s (attempt %d of %d); retrying in %.0fs",
            label,
            what,
            attempt,
            attempts,
            delay,
        )
        if before_retry is not None:
            try:
                await before_retry(attempt)
            except Exception as exc:  # noqa: BLE001 - escalation is best-effort
                log.debug("%s: retry escalation failed: %s", label, exc)
        if delay > 0:
            await asyncio.sleep(delay)

    if attempts > 1:
        log.info("%s: %s went unanswered after %d attempts", label, what, attempts)
    return None

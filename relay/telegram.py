"""Minimal async Telegram Bot API client (send + long-poll)."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import httpx

log = logging.getLogger("relay.telegram")


class TelegramClient:
    """Just enough of the Bot API: send a message, and long-poll for updates."""

    def __init__(self, token: str, chat_id: str, *, poll_timeout: int = 50):
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = str(chat_id)
        self._poll_timeout = poll_timeout
        # Read timeout must exceed the long-poll timeout.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(poll_timeout + 15))
        self._offset: Optional[int] = None

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict:
        """Verify the token; returns the bot's own account info."""
        return await self._call("getMe")

    async def send_message(self, text: str) -> None:
        if not text:
            return
        try:
            await self._call(
                "sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never let one send kill the relay
            log.warning("Failed to send message to Telegram: %s", exc)

    async def poll_messages(self) -> AsyncIterator[dict]:
        """Yield incoming message objects for the configured chat, forever.

        Uses getUpdates long polling. Only text messages from the configured
        chat are yielded; the bot's own outgoing messages never appear here.
        """
        # Skip the backlog: prime the offset to "now" so we don't replay
        # messages that arrived while the relay was offline.
        await self._prime_offset()

        while True:
            try:
                result = await self._call(
                    "getUpdates",
                    json={
                        "offset": self._offset,
                        "timeout": self._poll_timeout,
                        "allowed_updates": ["message"],
                    },
                )
            except httpx.TimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates failed (%s); backing off", exc)
                await asyncio.sleep(3)
                continue

            for update in result:
                self._offset = update["update_id"] + 1
                message = update.get("message")
                if not message or "text" not in message:
                    continue
                if str(message.get("chat", {}).get("id")) != self._chat_id:
                    continue
                yield message

    async def _prime_offset(self) -> None:
        try:
            updates = await self._call("getUpdates", json={"timeout": 0, "offset": -1})
            if updates:
                self._offset = updates[-1]["update_id"] + 1
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not prime Telegram offset: %s", exc)

    async def _call(self, method: str, *, json: Optional[dict] = None):
        resp = await self._client.post(f"{self._base}/{method}", json=json or {})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}: {data}")
        return data["result"]

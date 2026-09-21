"""Minimal async Telegram Bot API client (send + long-poll)."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import httpx

log = logging.getLogger("relay.telegram")

# Telegram rejects text over 4096 characters and captions over 1024. Both are
# counted in UTF-16 code units, so an emoji costs two; the margin keeps a
# message full of them from tipping over a limit that looks satisfied here.
MAX_TEXT = 3900
MAX_CAPTION = 1000


def split_message(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Break text into sendable parts, preferring line boundaries.

    Long output is split rather than cut off: a node list or a trace is only
    useful whole. Paragraph and line breaks are used where they fall, and a
    single line longer than the limit is hard-split rather than dropped.
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    current = ""

    for line in text.split("\n"):
        # A single line too long to ever fit has to be broken mid-line.
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = line

    if current:
        parts.append(current)
    return parts


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
        """Send text, splitting it across messages if it exceeds the limit."""
        parts = split_message(text)
        if len(parts) > 1:
            log.debug("Splitting a %d character message into %d parts",
                      len(text), len(parts))
        for part in parts:
            try:
                await self._call(
                    "sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": part,
                        "disable_web_page_preview": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - one failure must not lose the rest
                log.warning("Failed to send message to Telegram: %s", exc)

    async def send_location(self, latitude: float, longitude: float) -> None:
        """Drop a map pin. Telegram renders this as an interactive map."""
        try:
            lat, lon = float(latitude), float(longitude)
        except (TypeError, ValueError):
            return
        # Out-of-range values would be rejected by the API; skip quietly rather
        # than turning a nice-to-have pin into a visible error.
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            log.debug("Refusing to pin an out-of-range position: %s, %s", lat, lon)
            return
        try:
            await self._call(
                "sendLocation",
                json={"chat_id": self._chat_id, "latitude": lat, "longitude": lon},
            )
        except Exception as exc:  # noqa: BLE001 - a missing pin must not lose the alert
            log.warning("Failed to send location pin: %s", exc)

    async def send_photo(
        self, image: bytes, *, caption: str = "", filename: str = "chart.png"
    ) -> None:
        """Upload an image, following it with any caption too long to attach.

        Captions have a much smaller limit than messages, so rather than cut
        one short the overflow is sent as ordinary messages after the photo.
        """
        attached, overflow = caption, ""
        if len(caption) > MAX_CAPTION:
            parts = split_message(caption, MAX_CAPTION)
            attached, overflow = parts[0], "\n".join(parts[1:])

        try:
            resp = await self._client.post(
                f"{self._base}/sendPhoto",
                data={"chat_id": self._chat_id, "caption": attached},
                files={"photo": (filename, image, "image/png")},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("Photo upload failed (%s); sending text instead", exc)
            await self.send_message(caption)
            return

        if overflow:
            await self.send_message(overflow)

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

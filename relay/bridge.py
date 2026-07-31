"""Wires an OpenHop/MeshCore node to a Telegram chat, in both directions."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from meshcore import MeshCore, EventType

from .config import Config
from .nodes import SeenNodes, describe, type_code
from .telegram import TelegramClient

log = logging.getLogger("relay.bridge")

# How many channel slots to probe when resolving a channel by name.
_MAX_CHANNEL_SCAN = 16


class Bridge:
    def __init__(self, config: Config):
        self._cfg = config
        self._mesh: Optional[MeshCore] = None
        self._tg: Optional[TelegramClient] = None
        self._channel_idx: int = config.channel_index
        self._seen = SeenNodes(config.seen_nodes_file)

    async def run(self) -> None:
        cfg = self._cfg

        self._tg = TelegramClient(cfg.telegram_bot_token, cfg.telegram_chat_id)
        me = await self._tg.get_me()
        log.info("Telegram connected as @%s", me.get("username", "?"))

        log.info("Connecting to OpenHop at %s:%s ...", cfg.openhop_host, cfg.openhop_port)
        self._mesh = await MeshCore.create_tcp(
            cfg.openhop_host, cfg.openhop_port, auto_reconnect=True
        )
        log.info("Connected to OpenHop MeshCore node")

        self._channel_idx = await self._resolve_channel_index()
        log.info(
            "Relaying channel %r (index %d)  [%s]",
            cfg.channel_name,
            self._channel_idx,
            cfg.direction,
        )

        # Subscribe before auto-fetch starts, or messages drained in between
        # arrive with no handler attached and are lost.
        if cfg.relay_mesh_to_tg:
            self._mesh.subscribe(EventType.CHANNEL_MSG_RECV, self._on_mesh_message)

        if cfg.notify_new_nodes:
            await self._prime_seen_nodes()
            self._mesh.subscribe(EventType.NEW_CONTACT, self._on_new_contact)

        # Auto-fetch pulls queued messages off the node and emits *_MSG_RECV events.
        await self._mesh.start_auto_message_fetching()

        tasks = []
        if cfg.relay_tg_to_mesh:
            tasks.append(asyncio.create_task(self._telegram_loop(), name="tg-poll"))

        await self._tg.send_message(
            f"✅ Relay online — bridging mesh channel "
            f"“{cfg.channel_name}” ↔ this chat."
        )

        try:
            if tasks:
                await asyncio.gather(*tasks)
            else:
                # mesh_to_tg only: nothing to actively poll, just idle.
                await asyncio.Event().wait()
        finally:
            await self.aclose()

    # --- mesh -> telegram -------------------------------------------------

    async def _on_mesh_message(self, event) -> None:
        payload = event.payload or {}
        if payload.get("channel_idx") != self._channel_idx:
            return

        text = (payload.get("text") or "").strip()
        if not text:
            return

        sender = self._format_sender(payload)
        log.info("mesh -> tg: %s%s", f"{sender}: " if sender else "", text)
        assert self._tg is not None
        await self._tg.send_message(f"\U0001F4E1 {sender}: {text}" if sender else f"\U0001F4E1 {text}")

    @staticmethod
    def _format_sender(payload: dict) -> str:
        prefix = payload.get("pubkey_prefix") or payload.get("sender") or ""
        if isinstance(prefix, (bytes, bytearray)):
            prefix = prefix.hex()
        prefix = str(prefix)
        return prefix[:6] if prefix else ""

    # --- new node announcements -------------------------------------------

    async def _prime_seen_nodes(self) -> None:
        """Record the node's existing contacts so we only announce genuinely new ones.

        Without this, the first run would announce every contact the node
        already knows about.
        """
        assert self._mesh is not None
        self._seen.load()
        first_run = self._seen.is_empty

        contacts = await self._fetch_contacts()
        added = self._seen.seed(contacts.keys())

        if first_run and added:
            log.info("Recorded %d existing contact(s) without announcing", added)
            if self._cfg.announce_seed_summary and self._tg is not None:
                await self._tg.send_message(
                    f"\U0001F5C2 Tracking {added} known node(s); "
                    f"you'll get an alert when a new one appears."
                )
        elif added:
            log.info("Recorded %d contact(s) new to the store without announcing", added)

    async def _fetch_contacts(self) -> dict:
        """Best-effort fetch of the node's contact list, keyed by public key."""
        assert self._mesh is not None
        try:
            result = await self._mesh.commands.get_contacts()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch contacts (%s); starting with none", exc)
            return {}

        if getattr(result, "type", None) == EventType.ERROR:
            log.warning("Node returned an error for get_contacts: %s", result.payload)
            return {}

        payload = result.payload
        return payload if isinstance(payload, dict) else {}

    async def _on_new_contact(self, event) -> None:
        contact = event.payload or {}
        pubkey = contact.get("public_key") or ""
        if not pubkey:
            return

        code = type_code(contact)
        if code not in self._cfg.notify_node_types:
            log.debug("Ignoring new %s node (filtered out): %s", code, pubkey[:6])
            # Still record it, so enabling the type later doesn't backfill alerts.
            self._seen.add(pubkey)
            return

        if not self._seen.add(pubkey):
            return  # already announced

        text = describe(contact)
        log.info("new node -> tg: %s", text.replace("\n", " | "))
        if self._tg is not None:
            await self._tg.send_message(text)

    # --- telegram -> mesh -------------------------------------------------

    async def _telegram_loop(self) -> None:
        assert self._tg is not None and self._mesh is not None
        async for message in self._tg.poll_messages():
            text = (message.get("text") or "").strip()
            if not text or text.startswith("/"):
                continue  # skip empty messages and bot commands

            body = self._format_outgoing(message, text)
            log.info("tg -> mesh: %s", body)
            try:
                result = await self._mesh.commands.send_chan_msg(self._channel_idx, body)
                if getattr(result, "type", None) == EventType.ERROR:
                    log.warning("Mesh send error: %s", result.payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to send to mesh: %s", exc)

    def _format_outgoing(self, message: dict, text: str) -> str:
        who = (message.get("from", {}) or {}).get("first_name", "").strip()
        parts = []
        if self._cfg.tg_to_mesh_prefix:
            parts.append(self._cfg.tg_to_mesh_prefix)
        if who:
            parts.append(f"{who}:")
        parts.append(text)
        body = " ".join(parts)
        if len(body) > self._cfg.mesh_max_chars:
            body = body[: self._cfg.mesh_max_chars - 1].rstrip() + "…"
        return body

    # --- channel resolution ----------------------------------------------

    async def _resolve_channel_index(self) -> int:
        """Find the index of the channel whose name matches config, case-insensitively.

        Falls back to the configured index if the name can't be resolved.
        """
        assert self._mesh is not None
        wanted = self._cfg.channel_name.strip().lower()
        if not wanted:
            return self._cfg.channel_index

        for idx in range(_MAX_CHANNEL_SCAN):
            try:
                result = await self._mesh.commands.get_channel(idx)
            except Exception:  # noqa: BLE001
                break
            if getattr(result, "type", None) == EventType.ERROR:
                continue
            payload = result.payload or {}
            name = (
                payload.get("channel_name")
                or payload.get("name")
                or ""
            ).strip()
            if name.lower() == wanted:
                return idx

        log.warning(
            "Could not resolve channel %r by name; using index %d",
            self._cfg.channel_name,
            self._cfg.channel_index,
        )
        return self._cfg.channel_index

    # --- teardown ---------------------------------------------------------

    async def aclose(self) -> None:
        if self._mesh is not None:
            try:
                await self._mesh.stop_auto_message_fetching()
                await self._mesh.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._tg is not None:
            await self._tg.close()

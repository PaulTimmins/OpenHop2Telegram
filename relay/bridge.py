"""Wires an OpenHop/MeshCore node to a Telegram chat, in both directions."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from meshcore import MeshCore, EventType

from .config import Config
from .coordination import Coordinator
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
        self._coord = Coordinator(config.lock_dir)
        # Set by the DISCONNECTED handler / health check to wake the supervisor.
        self._lost: Optional[asyncio.Event] = None
        self._paused = False
        self._seeded = False
        self._announced_offline = False

    async def run(self) -> None:
        cfg = self._cfg

        self._tg = TelegramClient(cfg.telegram_bot_token, cfg.telegram_chat_id)
        me = await self._tg.get_me()
        log.info("Telegram connected as @%s", me.get("username", "?"))

        # Lets maintenance tools tell that a relay is holding the node.
        self._coord.write_pid()

        # The Telegram side is independent of the mesh link, so it keeps running
        # across mesh reconnects rather than being torn down with each session.
        tg_task = (
            asyncio.create_task(self._telegram_loop(), name="tg-poll")
            if cfg.relay_tg_to_mesh
            else None
        )

        try:
            await self._supervise()
        finally:
            if tg_task is not None:
                tg_task.cancel()
                try:
                    await tg_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self.aclose()

    async def _supervise(self) -> None:
        """Keep a mesh session alive forever, reconnecting with backoff.

        meshcore's own auto-reconnect gives up after a handful of fast attempts,
        which is not enough to survive the node being reconfigured or rebooted.
        This loop retries indefinitely instead.
        """
        cfg = self._cfg
        delay = cfg.reconnect_min_delay
        first = True

        while True:
            try:
                await self._connect_session()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Connection to %s:%s failed (%s); retrying in %.0fs",
                    cfg.openhop_host,
                    cfg.openhop_port,
                    exc,
                    delay,
                )
                await self._announce_offline(str(exc))
                await asyncio.sleep(delay)
                delay = min(delay * 2, cfg.reconnect_max_delay)
                continue

            # Connected: reset the backoff so the next outage starts short.
            delay = cfg.reconnect_min_delay
            await self._announce_online(first)
            first = False

            try:
                await self._await_disconnect()
            finally:
                await self._teardown_session()

            if self._paused:
                # A tool asked for the node; hand it over and wait our turn.
                await self._wait_out_pause()
                continue

            log.warning("Mesh connection lost; reconnecting in %.0fs", delay)
            await self._announce_offline("connection lost")
            await asyncio.sleep(delay)

    async def _wait_out_pause(self) -> None:
        """Stay off the node until the pause request is withdrawn."""
        self._paused = False
        log.info("Released the node; waiting for %s to be removed", self._coord.pause_file)
        if self._cfg.notify_connection_events and self._tg is not None:
            await self._tg.send_message(
                "\U0001F503 Handed the node to a maintenance task; back shortly."
            )

        self._coord.mark_released()
        try:
            while self._coord.pause_requested():
                await asyncio.sleep(1)
        finally:
            self._coord.clear_released()
        log.info("Pause lifted; reconnecting")

    async def _connect_session(self) -> None:
        """Open a mesh session and attach every subscription it needs."""
        cfg = self._cfg
        self._lost = asyncio.Event()

        log.info("Connecting to OpenHop at %s:%s ...", cfg.openhop_host, cfg.openhop_port)
        # A high attempt count lets the library ride out brief blips on its own;
        # anything worse falls through to _supervise, which never gives up.
        self._mesh = await MeshCore.create_tcp(
            cfg.openhop_host,
            cfg.openhop_port,
            auto_reconnect=True,
            max_reconnect_attempts=1_000_000,
        )
        log.info("Connected to OpenHop MeshCore node")

        self._mesh.subscribe(EventType.DISCONNECTED, self._on_disconnected)

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
            # Only seed once per process: on later reconnects the store is
            # already populated, and re-seeding would hide nodes that appeared
            # while we were offline.
            await self._prime_seen_nodes(announce_summary=not self._seeded)
            self._seeded = True
            self._mesh.subscribe(EventType.NEW_CONTACT, self._on_new_contact)

        # Auto-fetch pulls queued messages off the node and emits *_MSG_RECV events.
        await self._mesh.start_auto_message_fetching()

    async def _await_disconnect(self) -> None:
        """Block until the link drops, polling the node if a health check is on."""
        assert self._lost is not None
        interval = self._cfg.healthcheck_interval
        if interval <= 0:
            # Health checks off, but we still have to notice a pause request.
            while not self._lost.is_set():
                if self._coord.pause_requested():
                    log.info("Pause requested; releasing the node")
                    self._paused = True
                    return
                try:
                    await asyncio.wait_for(self._lost.wait(), timeout=2.0)
                    return
                except asyncio.TimeoutError:
                    pass
            return

        # Poll often enough that a pause request is noticed promptly, rather than
        # only at the health-check interval.
        tick = min(interval, 2.0)
        elapsed = 0.0

        while not self._lost.is_set():
            if self._coord.pause_requested():
                log.info("Pause requested; releasing the node")
                self._paused = True
                return
            try:
                await asyncio.wait_for(self._lost.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass

            elapsed += tick
            if elapsed >= interval:
                elapsed = 0.0
                if not await self._healthy():
                    log.warning("Health check failed; treating the link as down")
                    return

    async def _healthy(self) -> bool:
        """Ask the node for its time as a cheap liveness probe.

        A TCP session can stay open while the node stops answering (this is what
        a silent stall looks like), so waiting for a socket error isn't enough.
        """
        if self._mesh is None:
            return False
        try:
            result = await asyncio.wait_for(self._mesh.commands.get_time(), timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.debug("Health check error: %s", exc)
            return False
        return getattr(result, "type", None) != EventType.ERROR

    async def _on_disconnected(self, event) -> None:
        payload = getattr(event, "payload", None) or {}
        reason = payload.get("reason", "unknown")
        if payload.get("reason") == "manual_disconnect":
            return  # our own shutdown
        log.warning("Node reported disconnect (reason=%s)", reason)
        if self._lost is not None:
            self._lost.set()

    async def _teardown_session(self) -> None:
        """Drop the current mesh session so the next attempt starts clean."""
        mesh, self._mesh = self._mesh, None
        if mesh is None:
            return
        try:
            await mesh.stop_auto_message_fetching()
        except Exception:  # noqa: BLE001
            pass
        try:
            await mesh.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # --- connection notifications -----------------------------------------

    async def _announce_online(self, first: bool) -> None:
        self._announced_offline = False
        if self._tg is None:
            return
        if first:
            await self._tg.send_message(
                f"✅ Relay online — bridging mesh channel "
                f"“{self._cfg.channel_name}” ↔ this chat."
            )
        elif self._cfg.notify_connection_events:
            await self._tg.send_message("✅ Reconnected to the mesh node.")

    async def _announce_offline(self, reason: str) -> None:
        # Only the first failure in an outage is reported, so a node that stays
        # down doesn't produce a message per retry.
        if self._announced_offline or self._tg is None:
            return
        self._announced_offline = True
        if self._cfg.notify_connection_events:
            await self._tg.send_message(
                f"⚠️ Lost the mesh node ({reason}). Retrying until it's back."
            )

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

    async def _prime_seen_nodes(self, announce_summary: bool = True) -> None:
        """Record the node's existing contacts so we only announce genuinely new ones.

        Without this, the first run would announce every contact the node
        already knows about.
        """
        assert self._mesh is not None
        if not self._seeded:
            self._seen.load()
        first_run = self._seen.is_empty and announce_summary

        contacts = await self._fetch_contacts()
        # Pass the full records so the store keeps names and types, not just keys.
        added = self._seen.seed(contacts)

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
            self._seen.add(pubkey, contact)
            return

        if not self._seen.add(pubkey, contact):
            return  # already announced

        text = describe(contact)
        log.info("new node -> tg: %s", text.replace("\n", " | "))
        if self._tg is not None:
            await self._tg.send_message(text)

    # --- telegram -> mesh -------------------------------------------------

    async def _telegram_loop(self) -> None:
        assert self._tg is not None
        async for message in self._tg.poll_messages():
            text = (message.get("text") or "").strip()
            if not text or text.startswith("/"):
                continue  # skip empty messages and bot commands

            # This task outlives any single mesh session, so the node may be
            # down right now. Say so rather than dropping the message silently.
            mesh = self._mesh
            if mesh is None:
                log.warning("Dropping Telegram message; mesh node is offline")
                await self._tg.send_message(
                    "⚠️ Not sent — the mesh node is offline right now."
                )
                continue

            body = self._format_outgoing(message, text)
            log.info("tg -> mesh: %s", body)
            try:
                result = await mesh.commands.send_chan_msg(self._channel_idx, body)
                if getattr(result, "type", None) == EventType.ERROR:
                    log.warning("Mesh send error: %s", result.payload)
                    await self._tg.send_message(f"⚠️ Mesh rejected that: {result.payload}")
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to send to mesh: %s", exc)
                await self._tg.send_message(f"⚠️ Failed to send that to the mesh: {exc}")

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
        self._coord.clear_pid()
        self._coord.clear_released()
        await self._teardown_session()
        if self._tg is not None:
            await self._tg.close()
            self._tg = None

#!/usr/bin/env python3
"""Print the chat id of any chat your bot can see.

Usage:
    python3 scripts/get_chat_id.py            # reads TELEGRAM_BOT_TOKEN (or .env)
    python3 scripts/get_chat_id.py <token>

Add the bot to the group first, then send "/start@YourBotName" in that group.
Stop the relay before running this — Telegram only allows one getUpdates
consumer at a time, so they would fight over updates.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# Update kinds that carry a chat we might care about.
_MESSAGE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")


def _token_from_env_file() -> str:
    """Best-effort read of TELEGRAM_BOT_TOKEN from a sibling .env (no deps)."""
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN"):
            _, _, value = line.partition("=")
            return value.strip().strip("'\"")
    return ""


def resolve_token(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1].strip()
    return (os.getenv("TELEGRAM_BOT_TOKEN") or _token_from_env_file()).strip()


def call(token: str, method: str) -> object:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise SystemExit(f"Telegram returned HTTP {exc.code} for {method}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Telegram: {exc.reason}")
    if not data.get("ok"):
        raise SystemExit(f"Telegram API error: {data}")
    return data["result"]


def main() -> None:
    token = resolve_token(sys.argv)
    if not token:
        raise SystemExit(
            "No bot token. Pass it as an argument, set TELEGRAM_BOT_TOKEN, "
            "or put it in .env"
        )

    me = call(token, "getMe")
    username = me.get("username", "?")
    print(f"Bot: @{username}\n")

    updates = call(token, "getUpdates")
    if not updates:
        print("No updates yet. Do this, then re-run:")
        print("  1. Create the group and add the bot to it")
        print(f"  2. Send this in the group:  /start@{username}")
        print("     (bots can't see ordinary group chatter unless privacy mode")
        print("      is disabled, but commands always get through)")
        return

    seen: dict[str, dict] = {}
    for update in updates:
        for key in _MESSAGE_KEYS:
            chat = (update.get(key) or {}).get("chat")
            if chat:
                seen[str(chat["id"])] = chat

    if not seen:
        print("Updates exist, but none carried a chat. Send /start in the chat.")
        return

    print(f"{'CHAT ID':<18} {'TYPE':<12} NAME")
    print("-" * 56)
    for chat_id, chat in seen.items():
        name = (
            chat.get("title")
            or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            )
            or chat.get("username")
            or ""
        )
        print(f"{chat_id:<18} {chat.get('type', '?'):<12} {name}")

    print("\nSet TELEGRAM_CHAT_ID in .env to the id you want.")
    if any(c.get("type") == "channel" for c in seen.values()):
        print(
            "\nNote: 'channel' entries are broadcast channels. This relay only "
            "listens for 'message' updates, so use a group, not a channel."
        )


if __name__ == "__main__":
    main()

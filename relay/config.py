"""Configuration loaded from environment variables (optionally a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


DIRECTIONS = {"both", "mesh_to_tg", "tg_to_mesh"}


@dataclass(frozen=True)
class Config:
    openhop_host: str
    openhop_port: int
    channel_name: str
    channel_index: int
    telegram_bot_token: str
    telegram_chat_id: str
    direction: str
    tg_to_mesh_prefix: str
    mesh_max_chars: int
    log_level: str

    @property
    def relay_mesh_to_tg(self) -> bool:
        return self.direction in ("both", "mesh_to_tg")

    @property
    def relay_tg_to_mesh(self) -> bool:
        return self.direction in ("both", "tg_to_mesh")

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        def require(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise SystemExit(f"Missing required environment variable: {key}")
            return val

        direction = os.getenv("RELAY_DIRECTION", "both").strip().lower()
        if direction not in DIRECTIONS:
            raise SystemExit(
                f"RELAY_DIRECTION must be one of {sorted(DIRECTIONS)}, got {direction!r}"
            )

        return cls(
            openhop_host=os.getenv("OPENHOP_HOST", "127.0.0.1").strip(),
            openhop_port=int(os.getenv("OPENHOP_PORT", "4000")),
            channel_name=os.getenv("MESH_CHANNEL_NAME", "General").strip(),
            channel_index=int(os.getenv("MESH_CHANNEL_INDEX", "0")),
            telegram_bot_token=require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=require("TELEGRAM_CHAT_ID"),
            direction=direction,
            tg_to_mesh_prefix=os.getenv("TG_TO_MESH_PREFIX", "[tg]"),
            mesh_max_chars=int(os.getenv("MESH_MAX_CHARS", "140")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )

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

# Contact type codes used by MeshCore, plus friendly aliases accepted in config.
NODE_TYPES = {"NONE", "CLI", "REP", "ROOM", "SENS"}
NODE_TYPE_ALIASES = {
    "node": "NONE",
    "unknown": "NONE",
    "companion": "CLI",
    "client": "CLI",
    "repeater": "REP",
    "room": "ROOM",
    "roomserver": "ROOM",
    "sensor": "SENS",
}


def _parse_bool(value: str, default: bool) -> bool:
    value = value.strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _parse_node_types(raw: str) -> frozenset[str]:
    """Parse NOTIFY_NODE_TYPES into a set of contact type codes."""
    raw = raw.strip()
    if not raw or raw.lower() == "all":
        return frozenset(NODE_TYPES)

    result: set[str] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        code = token.upper()
        if code in NODE_TYPES:
            result.add(code)
            continue
        alias = NODE_TYPE_ALIASES.get(token.lower().replace(" ", "").replace("_", ""))
        if alias:
            result.add(alias)
        else:
            raise SystemExit(
                f"Unknown value {token!r} in NOTIFY_NODE_TYPES. Use 'all' or a "
                f"comma-separated list of {sorted(NODE_TYPES)} "
                f"(aliases: {sorted(NODE_TYPE_ALIASES)})."
            )
    if not result:
        raise SystemExit("NOTIFY_NODE_TYPES was set but parsed to nothing.")
    return frozenset(result)


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
    notify_new_nodes: bool
    notify_node_types: frozenset[str]
    seen_nodes_file: str
    announce_seed_summary: bool
    reconnect_min_delay: float
    reconnect_max_delay: float
    healthcheck_interval: float
    notify_connection_events: bool
    lock_dir: str
    # Endpoint the maintenance scripts use. Defaults to the relay's, but can
    # point somewhere else (a second companion port, a proxy, another node) so
    # the scripts don't share the relay's message queue.
    timesync_host: str
    timesync_port: int

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
            notify_new_nodes=_parse_bool(os.getenv("NOTIFY_NEW_NODES", ""), True),
            notify_node_types=_parse_node_types(os.getenv("NOTIFY_NODE_TYPES", "all")),
            seen_nodes_file=os.getenv("SEEN_NODES_FILE", "seen_nodes.json").strip(),
            announce_seed_summary=_parse_bool(
                os.getenv("ANNOUNCE_SEED_SUMMARY", ""), True
            ),
            reconnect_min_delay=float(os.getenv("RECONNECT_MIN_DELAY", "5")),
            reconnect_max_delay=float(os.getenv("RECONNECT_MAX_DELAY", "300")),
            healthcheck_interval=float(os.getenv("HEALTHCHECK_INTERVAL", "120")),
            notify_connection_events=_parse_bool(
                os.getenv("NOTIFY_CONNECTION_EVENTS", ""), True
            ),
            lock_dir=os.getenv("LOCK_DIR", ".").strip() or ".",
            timesync_host=(
                os.getenv("TIMESYNC_HOST", "").strip()
                or os.getenv("OPENHOP_HOST", "127.0.0.1").strip()
            ),
            timesync_port=int(
                os.getenv("TIMESYNC_PORT", "").strip()
                or os.getenv("OPENHOP_PORT", "4000")
            ),
        )

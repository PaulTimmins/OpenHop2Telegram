"""Build a channel message ourselves, so the sender name can be ours.

The companion protocol's channel-send command (``CMD_SEND_CHANNEL_TXT_MSG``)
carries a channel index, a timestamp and text -- and nothing else. The node
fills in the sender itself from its own advert name, which is why a Telegram
message lands on the mesh as ``PaulHouse: [tg] Paul: hi``. There is no field
we can set to change that.

What there is, on a node new enough to have it, is ``CMD_SEND_RAW_PACKET``:
hand over a finished on-air packet and it goes out verbatim. Build the group
datagram here and the sender string is whatever we put in it. The alternative
-- renaming the node around every send -- writes its config file per message
and leaves a window where the node would advertise the wrong identity to the
whole mesh.

Verbatim cuts both ways: the node applies no flood scope and no path hash
width to a raw packet, so both are set here instead. The format is mirrored
from openhop_core (``protocol/packet_builder.py`` ``create_group_datagram``,
``protocol/transport_keys.py``), which mirrors the firmware's
``BaseChatMesh::sendGroupMessage``.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from typing import Optional

try:  # pycryptodome arrives as a meshcore dependency; guarded so a stripped
    from Crypto.Cipher import AES  # install degrades to the normal send path.
except ImportError:  # pragma: no cover - depends on the environment
    AES = None  # type: ignore[assignment]

# --- wire constants, from openhop_core/protocol/constants.py ---------------

PAYLOAD_TYPE_GRP_TXT = 0x05
ROUTE_TYPE_TRANSPORT_FLOOD = 0x00
ROUTE_TYPE_FLOOD = 0x01
PAYLOAD_VER_1 = 0x00

PH_ROUTE_MASK = 0x03
PH_TYPE_SHIFT = 2
PH_VER_SHIFT = 6
PATH_HASH_SIZE_SHIFT = 6

CIPHER_BLOCK_SIZE = 16
CIPHER_MAC_SIZE = 2

# Firmware BaseChatMesh.h caps "<sender>: <text>" at this many bytes, and
# MeshCore.h caps the whole packet payload. Both are checked rather than
# trimmed to: silently truncating is the thing this relay refuses to do.
MAX_TEXT_LEN = 160
MAX_PACKET_PAYLOAD = 184

TXT_TYPE_PLAIN = 0x00

# A path_len byte encodes the hash width in its top two bits as width - 1.
# Width 4 is reserved -- openhop_core's is_valid_path_len rejects it, so a
# packet built at that width would be refused by the node that has to parse
# it back out of the frame.
RAW_PATH_HASH_BYTES = (1, 2, 3)


class Unsupported(Exception):
    """Raised when this packet can't be built, so the caller can fall back."""


def crypto_available() -> bool:
    """Whether the AES implementation needed to encrypt a channel is present."""
    return AES is not None


def channel_hash(secret: bytes) -> int:
    """The one-byte channel tag receivers match against their own channels.

    Mirrors openhop_core: a 32-byte secret whose second half is all zeros is
    hashed over the first half only, which is how a 16-byte key stored wide
    still produces the hash its 16-byte form would.
    """
    if len(secret) >= 32 and secret[16:32] == b"\x00" * 16:
        material = secret[:16]
    elif len(secret) > 32:
        material = secret[:32]
    else:
        material = secret
    return hashlib.sha256(material).digest()[0]


def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB with zero padding to the block size, as the firmware does."""
    if AES is None:  # pragma: no cover - guarded by crypto_available()
        raise Unsupported("pycryptodome is not installed")
    pad = (CIPHER_BLOCK_SIZE - (len(data) % CIPHER_BLOCK_SIZE)) % CIPHER_BLOCK_SIZE
    return AES.new(key, AES.MODE_ECB).encrypt(data + b"\x00" * pad)


def transport_code(scope_key: bytes, payload_type: int, payload: bytes) -> int:
    """Scope code for a flood packet, keyed on the payload it is carrying.

    Codes 0 and 0xFFFF are reserved, so they are nudged aside exactly as the
    firmware does -- a code off by one from the sender's would be dropped by
    every repeater enforcing the scope.
    """
    if len(scope_key) != 16:
        raise Unsupported(f"scope key must be 16 bytes, got {len(scope_key)}")
    digest = hmac.new(scope_key, bytes([payload_type]) + payload, hashlib.sha256).digest()
    code = struct.unpack("<H", digest[:2])[0]
    if code == 0:
        return 1
    if code == 0xFFFF:
        return 0xFFFE
    return code


def scope_key_for(name: str) -> bytes:
    """Derive a scope's transport key from its name, as the node would."""
    canonical = name if name.startswith("#") else f"#{name}"
    return hashlib.sha256(canonical.encode("ascii")).digest()[:16]


def room_for_text(sender_name: str) -> int:
    """Bytes of message text that will fit alongside this sender name."""
    return max(MAX_TEXT_LEN - len(f"{sender_name}: ".encode("utf-8")), 0)


def build(
    secret: bytes,
    sender_name: str,
    text: str,
    *,
    timestamp: int,
    path_hash_bytes: int = 1,
    scope_key: Optional[bytes] = None,
) -> bytes:
    """Return the on-air bytes of one encrypted channel message.

    Raises Unsupported for anything the caller should handle by falling back
    to the node's own channel send rather than by dropping the message.
    """
    if not crypto_available():
        raise Unsupported("pycryptodome is not installed")
    if len(secret) < 16:
        raise Unsupported(f"channel secret must be at least 16 bytes, got {len(secret)}")
    if path_hash_bytes not in RAW_PATH_HASH_BYTES:
        raise Unsupported(
            f"a raw packet can't carry {path_hash_bytes}-byte path hashes "
            f"(the width is encoded in two bits and 4 is reserved)"
        )

    content = f"{sender_name}: ".encode("utf-8") + text.encode("utf-8")
    if len(content) > MAX_TEXT_LEN:
        raise Unsupported(
            f"{len(content)} bytes of sender and text exceeds the {MAX_TEXT_LEN} "
            f"byte on-air limit"
        )

    tag = channel_hash(secret)
    wide = (bytes(secret) + b"\x00" * 32)[:32]
    plaintext = struct.pack("<I", timestamp) + bytes([TXT_TYPE_PLAIN]) + content
    ciphertext = _aes_ecb_encrypt(wide[:16], plaintext)
    mac = hmac.new(wide, ciphertext, hashlib.sha256).digest()[:CIPHER_MAC_SIZE]
    payload = bytes([tag]) + mac + ciphertext

    if len(payload) > MAX_PACKET_PAYLOAD:
        raise Unsupported(
            f"payload of {len(payload)} bytes exceeds the {MAX_PACKET_PAYLOAD} byte cap"
        )

    route = ROUTE_TYPE_FLOOD
    codes = b""
    if scope_key:
        # Scoping replaces the route type and prepends two 16-bit codes, the
        # second reserved for a home region the firmware doesn't use yet.
        route = ROUTE_TYPE_TRANSPORT_FLOOD
        codes = struct.pack(
            "<HH", transport_code(scope_key, PAYLOAD_TYPE_GRP_TXT, payload), 0
        )

    header = (
        (route & PH_ROUTE_MASK)
        | (PAYLOAD_TYPE_GRP_TXT << PH_TYPE_SHIFT)
        | (PAYLOAD_VER_1 << PH_VER_SHIFT)
    )
    # Zero hops: this packet is being originated, so the width is ours to set.
    path_len = (path_hash_bytes - 1) << PATH_HASH_SIZE_SHIFT

    return bytes([header]) + codes + bytes([path_len]) + payload

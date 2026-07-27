"""
The Kademlia XOR metric over 256-bit Canonical Agent-IDs.

IDs are 64-hex-char strings (256-bit SHA-256, per AGTP-IDENTIFIERS).
Distance is the integer value of the bitwise XOR of two IDs; the bucket
index of a key relative to a node is the position of the most significant
differing bit (0..255), which is how a routing table decides which k-bucket
a peer belongs in.
"""

from __future__ import annotations

ID_BITS = 256
ID_HEX_LEN = 64


class InvalidNodeID(ValueError):
    """Raised when an ID is not a 64-hex-char (256-bit) string."""


def to_int(node_id: str) -> int:
    """Parse a 64-hex-char ID to its 256-bit integer value."""
    if not isinstance(node_id, str):
        raise InvalidNodeID(f"id must be a string, got {type(node_id).__name__}")
    text = node_id.strip().lower()
    if len(text) != ID_HEX_LEN:
        raise InvalidNodeID(f"id must be {ID_HEX_LEN} hex chars, got {len(text)}")
    try:
        return int(text, 16)
    except ValueError as exc:
        raise InvalidNodeID(f"id is not hex: {node_id!r}") from exc


def xor_distance(a: str, b: str) -> int:
    """XOR distance between two IDs (0 == identical, larger == farther)."""
    return to_int(a) ^ to_int(b)


def bucket_index(node_id: str, key: str) -> int:
    """
    The k-bucket index for ``key`` relative to ``node_id``: the position of
    the most significant bit at which the two IDs differ, in ``[0, 255]``.
    Identical IDs have no differing bit; that is caller-handled (a node is
    never in its own routing table) — we return -1 to signal it.
    """
    d = xor_distance(node_id, key)
    if d == 0:
        return -1
    return d.bit_length() - 1

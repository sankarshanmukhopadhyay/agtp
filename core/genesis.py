"""
Agent Genesis: the permanent identity record for an AGTP agent.

An **Agent Genesis** is the governance-layer document produced at
ACTIVATE time. It establishes the agent's identity, owner, and
trust posture, and is signed by the issuing registrar. The hash of
its canonical JSON form is the agent's **Canonical Agent-ID** —
the value carried in every ``Agent-ID`` header.

Terminology and field semantics follow AGTP-LOG-00 §2 (which renames
the v06 draft's "Birth Certificate" to "Agent Genesis"). The fields
below are the daemon-load-bearing subset of AGTP §6.7's Birth
Certificate schema plus the AGTP-LOG identity taxonomy.

## Lifecycle

1. **Issued** by a registrar (governance platform) at ACTIVATE time.
   Self-issued ("self-signed") Geneses are valid for development;
   Trust Tier 1 requires a registrar-signed Genesis.
2. **Hashed** with SHA-256 over the RFC 8785-style canonical JSON of
   the document, **excluding the ``signature`` field**. The result is
   the Canonical Agent-ID.
3. **Signed** by the issuer's Ed25519 key. The ``signature`` field
   carries the base64url-encoded signature of the canonical hash.
4. **Served** by the agent's daemon at the ``/genesis`` path so
   verifiers can fetch + verify the binding to the Agent Cert.
5. **Permanent** for the life of the agent. A Genesis is never
   reissued; revocation marks the existing Genesis revoked rather
   than minting a new one (Phase 8 / AGTP-LOG).

## Why exclude signature from the hash

The signature is computed *over* the hash; including the signature
field in the hash would be self-referential. RFC 8785 canonicalization
keeps the rest deterministic so two implementations always agree on
the bytes being signed.

## Why the hash is the Agent-ID

It makes identity tamper-evident: any change to the Genesis changes
the Agent-ID, which propagates through every request header, every
Attribution-Record, every audit log entry. An attacker who alters
a single field of the Genesis produces a different agent — they
cannot mutate identity-in-place.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


GENESIS_VERSION = "agtp-genesis/1"


class GenesisError(Exception):
    """Base class for Agent Genesis errors."""


class GenesisFormatError(GenesisError):
    """Raised when a Genesis document is structurally invalid."""


class GenesisSignatureError(GenesisError):
    """Raised when a Genesis document's signature does not verify."""


# ---------------------------------------------------------------------------
# Allowed values.
# ---------------------------------------------------------------------------

VALID_TRUST_TIERS = (1, 2, 3)

VALID_VERIFICATION_PATHS = frozenset({
    "dns-anchored",
    "log-anchored",
    "hybrid",
    "self-signed",  # development convenience; not Tier 1
})

VALID_ARCHETYPES = frozenset({
    "assistant",
    "analyst",
    "executor",
    "orchestrator",
    "monitor",
})

# Note: ``role`` (agent vs merchant) is deliberately NOT a Genesis
# field. Genesis is identity-only — permanent, immutable, and
# hash-bound to the Agent-ID. Role is a manifest-level capability
# attribute that may change over an agent's life (an agent acquires
# merchant capabilities, retires merchant capabilities, etc.) and
# therefore lives on the AgentDocument. Putting role here would
# force every capability change to mint a new Agent-ID, which
# defeats the AGTP-LOG §2 immutability guarantee.


# ---------------------------------------------------------------------------
# Dataclass.
# ---------------------------------------------------------------------------


# Canonical field order. The Genesis JSON emits these in order and
# every encoder/decoder agrees on the layout. ``signature`` is
# intentionally last so callers reading the file can spot it without
# scrolling.
_FIELD_ORDER: List[str] = [
    "agtp_genesis_version",
    "name",
    "owner_id",
    "principal_id",
    "agent_public_key",
    "archetype",
    "governance_zone",
    "trust_tier",
    "verification_path",
    "issued_at",
    "issuer",
    "issuer_public_key",
    "signature",
]


@dataclass
class AgentGenesis:
    """The permanent identity document for an AGTP agent.

    See module docstring for lifecycle and field semantics.

    The ``agent_id`` is NOT a field — it's derived. Use
    :meth:`canonical_agent_id` to compute it.
    """

    name: str
    """Human-readable label. Example: ``"lauren"``."""

    owner_id: str
    """Legal owner — the entity (corp / individual) that registered
    this agent. Surfaced on responses as the ``Owner-ID`` header."""

    principal_id: str
    """Default principal — the human or service the agent acts on
    behalf of when no per-request Principal-ID is supplied."""

    agent_public_key: str
    """The agent's Ed25519 public key, PEM-encoded. This key is the
    long-term identity key tied to this Genesis. The Agent Cert's
    cert-pubkey is independent (renewable); cryptographic binding
    happens via the registrar's signature on this Genesis, not via
    key equality."""

    issued_at: str
    """ISO 8601 UTC timestamp of issuance. Permanent — never changes
    on renewal because Geneses don't renew."""

    issuer: str
    """Identifier of the registrar that issued this Genesis. Examples:
    ``"registrar.agtp.io"``, ``"self"`` (for self-signed dev
    Geneses)."""

    issuer_public_key: str
    """PEM-encoded Ed25519 public key the verifier uses to validate
    :attr:`signature`. For self-signed Geneses, this equals
    :attr:`agent_public_key`."""

    archetype: Optional[str] = None
    """Behavioral archetype: assistant / analyst / executor /
    orchestrator / monitor. Optional; mirrors the
    ``archetype`` X.509 extension when set."""

    governance_zone: Optional[str] = None
    """Governance zone identifier (e.g., ``zone:finance``). Optional."""

    trust_tier: int = 2
    """1 = Verified, 2 = Org-Asserted, 3 = Experimental. Defaults to
    2 because Tier 1 requires the registrar to have completed a
    verification path (DNS / log / hybrid); a bare-issuance Genesis
    is Tier 2."""

    verification_path: str = "self-signed"
    """``dns-anchored`` / ``log-anchored`` / ``hybrid`` / ``self-signed``."""

    signature: str = ""
    """Base64url-encoded Ed25519 signature over the canonical JSON
    of this Genesis with the ``signature`` field set to ``""``.
    Empty string means unsigned (not yet finalized)."""

    agtp_genesis_version: str = GENESIS_VERSION
    """Schema version. Future revisions bump this to coordinate
    parser migrations."""

    # ----- Serialization -----

    def to_dict(self) -> Dict[str, Any]:
        """Return the Genesis as a dict in canonical field order."""
        raw = asdict(self)
        out: Dict[str, Any] = {}
        for key in _FIELD_ORDER:
            value = raw.get(key)
            if value is None:
                continue  # optional fields elided
            out[key] = value
        return out

    def to_canonical_json(self, *, exclude_signature: bool = False) -> str:
        """Return RFC 8785-style canonical JSON: sorted keys, no
        whitespace. The form the hash and signature cover.

        When ``exclude_signature`` is true, the ``signature`` field
        is dropped before serialization. This is the form that
        :meth:`canonical_agent_id` hashes and that :meth:`sign`
        signs over.
        """
        d = self.to_dict()
        if exclude_signature:
            d.pop("signature", None)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def to_pretty_json(self) -> str:
        """Pretty-printed JSON in canonical field order. The form
        the CLI emits to disk."""
        return json.dumps(self.to_dict(), indent=2)

    # ----- Hashing / Agent-ID derivation -----

    def canonical_agent_id(self) -> str:
        """Compute the Canonical Agent-ID = sha256(canonical JSON
        excluding signature) hex-encoded. This is THE Agent-ID that
        rides on the wire for any agent backed by this Genesis."""
        canonical = self.to_canonical_json(exclude_signature=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ----- Signing / verification -----

    def sign(self, issuer_private_key: Ed25519PrivateKey) -> None:
        """Sign this Genesis in place with the issuer's Ed25519 key.

        After this call, :attr:`signature` is populated. The
        signature covers the canonical JSON form with ``signature``
        excluded.

        The caller is responsible for setting :attr:`issuer_public_key`
        to the public-key counterpart of ``issuer_private_key`` before
        calling sign — otherwise verifiers won't know which key to
        use.
        """
        if not self.issuer_public_key:
            raise GenesisFormatError(
                "issuer_public_key must be set before signing"
            )
        canonical = self.to_canonical_json(exclude_signature=True)
        sig = issuer_private_key.sign(canonical.encode("utf-8"))
        self.signature = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")

    def verify(self) -> None:
        """Verify the embedded signature against
        :attr:`issuer_public_key`.

        Raises :class:`GenesisSignatureError` on any failure (missing
        signature, malformed key, bad signature). Returns ``None`` on
        success.
        """
        if not self.signature:
            raise GenesisSignatureError("Genesis is unsigned")
        try:
            issuer_pub = _load_pem_public_key(self.issuer_public_key)
        except Exception as exc:  # noqa: BLE001
            raise GenesisSignatureError(
                f"issuer_public_key is not a valid Ed25519 PEM key: {exc}"
            ) from exc
        try:
            padded = self.signature + "=" * (-len(self.signature) % 4)
            sig_bytes = base64.urlsafe_b64decode(padded)
        except (ValueError, TypeError) as exc:
            raise GenesisSignatureError(
                f"signature is not valid base64url: {exc}"
            ) from exc
        canonical = self.to_canonical_json(exclude_signature=True)
        try:
            issuer_pub.verify(sig_bytes, canonical.encode("utf-8"))
        except InvalidSignature as exc:
            raise GenesisSignatureError(
                "Genesis signature did not verify against issuer_public_key"
            ) from exc


# ---------------------------------------------------------------------------
# Loading.
# ---------------------------------------------------------------------------


def parse_genesis(data: Dict[str, Any]) -> AgentGenesis:
    """Build an :class:`AgentGenesis` from a parsed dict.

    Validates required fields, enumerated values (trust_tier,
    verification_path, archetype), and version compatibility. Raises
    :class:`GenesisFormatError` for any structural failure. Does NOT
    verify the signature — call :meth:`AgentGenesis.verify` after
    loading if you need that.
    """
    if not isinstance(data, dict):
        raise GenesisFormatError(
            f"Genesis must be a JSON object, got {type(data).__name__}"
        )
    version = data.get("agtp_genesis_version") or GENESIS_VERSION
    if version != GENESIS_VERSION:
        raise GenesisFormatError(
            f"unsupported agtp_genesis_version: {version!r} "
            f"(this implementation handles {GENESIS_VERSION!r})"
        )

    required = (
        "name", "owner_id", "principal_id", "agent_public_key",
        "issued_at", "issuer", "issuer_public_key",
    )
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise GenesisFormatError(
            f"Genesis missing required field(s): {missing}"
        )

    trust_tier = data.get("trust_tier", 2)
    if trust_tier not in VALID_TRUST_TIERS:
        raise GenesisFormatError(
            f"trust_tier must be one of {VALID_TRUST_TIERS}; got {trust_tier!r}"
        )
    # ``role`` was briefly part of the Genesis schema during Phase 7
    # development. It's now AgentDocument-only — Genesis is identity,
    # role is capability. Silently ignore the field if present in a
    # legacy file so old fixtures don't crash boot.
    data.pop("role", None) if isinstance(data, dict) else None
    verification_path = data.get("verification_path", "self-signed")
    if verification_path not in VALID_VERIFICATION_PATHS:
        raise GenesisFormatError(
            f"verification_path must be one of "
            f"{sorted(VALID_VERIFICATION_PATHS)}; got {verification_path!r}"
        )
    archetype = data.get("archetype")
    if archetype is not None and archetype not in VALID_ARCHETYPES:
        raise GenesisFormatError(
            f"archetype must be one of {sorted(VALID_ARCHETYPES)}; "
            f"got {archetype!r}"
        )

    return AgentGenesis(
        name=str(data["name"]),
        owner_id=str(data["owner_id"]),
        principal_id=str(data["principal_id"]),
        agent_public_key=str(data["agent_public_key"]),
        archetype=archetype,
        governance_zone=data.get("governance_zone"),
        trust_tier=int(trust_tier),
        verification_path=str(verification_path),
        issued_at=str(data["issued_at"]),
        issuer=str(data["issuer"]),
        issuer_public_key=str(data["issuer_public_key"]),
        signature=str(data.get("signature") or ""),
        agtp_genesis_version=version,
    )


def load_genesis_json(text: str) -> AgentGenesis:
    """Parse a JSON-encoded Genesis document. Raises
    :class:`GenesisFormatError` on malformed JSON or schema."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenesisFormatError(f"invalid JSON: {exc}") from exc
    return parse_genesis(data)


# ---------------------------------------------------------------------------
# Cert-binding helper.
# ---------------------------------------------------------------------------


def verify_cert_genesis_binding(
    *,
    genesis: AgentGenesis,
    subject_agent_id: str,
) -> None:
    """Verify a cert's ``subject-agent-id`` extension matches a
    Genesis document.

    Used by the chain inspector, audit tools, and
    ``mod_agent_cert``-aware Scope-Enforcement Points to confirm
    that a presented Agent Cert is genuinely bound to a particular
    Genesis. The check is:

      1. The Genesis's own signature verifies against its
         ``issuer_public_key``.
      2. ``sha256(Genesis canonical JSON sans signature) ==
         subject_agent_id``.

    Raises :class:`GenesisSignatureError` or :class:`GenesisFormatError`
    on any failure. Returns ``None`` on success.

    Note this helper does NOT verify the issuer's identity (i.e.,
    that ``issuer_public_key`` belongs to a trusted registrar).
    Trust-anchor verification is the caller's responsibility — the
    inspector consults a known-registrars list; an enterprise SEP
    pins a single CA.
    """
    genesis.verify()
    expected = genesis.canonical_agent_id()
    if expected != subject_agent_id.lower():
        raise GenesisFormatError(
            f"Genesis hash {expected!r} does not match "
            f"subject-agent-id extension {subject_agent_id!r}"
        )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _load_pem_public_key(pem_text: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key from PEM text. Raises on any failure."""
    key = serialization.load_pem_public_key(pem_text.encode("utf-8"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(
            f"expected Ed25519 public key, got {type(key).__name__}"
        )
    return key


def utc_now_iso() -> str:
    """Current UTC time as an AGTP-conventional ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def public_key_pem(public_key: Ed25519PublicKey) -> str:
    """Encode an Ed25519 public key as a PEM string suitable for the
    ``agent_public_key`` / ``issuer_public_key`` fields."""
    pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem_bytes.decode("ascii")


__all__ = [
    "AgentGenesis",
    "GENESIS_VERSION",
    "GenesisError",
    "GenesisFormatError",
    "GenesisSignatureError",
    "VALID_ARCHETYPES",
    "VALID_TRUST_TIERS",
    "VALID_VERIFICATION_PATHS",
    "load_genesis_json",
    "parse_genesis",
    "public_key_pem",
    "utc_now_iso",
    "verify_cert_genesis_binding",
]

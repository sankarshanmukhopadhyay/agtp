"""
Agent Document — the canonical identity record served by AGTP agents.

Reference: draft-hood-independent-agtp (v07 draft) plus the Interaction
Model design note. The Agent Document is the protocol's authoritative
representation of an agent's identity, the skills it offers, and the
methods it requires from peer agents and infrastructure.

Schema versions
---------------
``document_version: "v2"`` is the current schema (this file). The v2
shape replaces v1's ``capabilities`` field with two complementary
declarations:

  * ``skills``   - human-readable prose describing what the agent does.
  * ``requires`` - structured needs: methods it consumes, scopes it
                   needs, and a wildcards flag for orchestrators that
                   accept any method.

v1 documents continue to load. ``from_dict`` detects the older shape
and routes to ``from_dict_v1_compat``, which lifts ``capabilities``
into ``requires.methods`` and seeds ``skills`` from the description.
A migrated document carries ``document_version="v1-migrated"`` so
operators can choose to rewrite the source file.

Media types
-----------
    application/vnd.agtp.identity+json    canonical wire format
    application/vnd.agtp.identity+yaml    human-editable form
    application/vnd.agtp.manifest+json    server-level manifest
                                          (returned by DISCOVER without
                                           an Agent-ID header)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


CONTENT_TYPE_JSON = "application/vnd.agtp.identity+json"
CONTENT_TYPE_YAML = "application/vnd.agtp.identity+yaml"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"
CONTENT_TYPE_MANIFEST_JSON = "application/vnd.agtp.manifest+json"

# Document-type discriminators (emitted as ``X-AGTP-Document-Type``).
# The header lets a renderer dispatch on what a response IS before
# parsing the body. Three canonical document types today:
#
#   agtp.agent.document   Agent Document — the per-agent identity
#                         returned by DESCRIBE (Form 1/1a).
#   agtp.server.manifest  Server Manifest — operator / methods /
#                         hosted_agents / hosted_protocols / apis /
#                         policy. Returned by server-level DISCOVER
#                         (Form 2) against an AGTP server.
#   agtp.server.identity  Application-server identity — the shape an
#                         application-typed server (e.g. MCP-on-AGTP
#                         gateway) returns from DESCRIBE/DISCOVER
#                         target=server. Carries an ``application``
#                         block whose ``type`` is mirrored on
#                         ``X-AGTP-Application``.
HEADER_DOCUMENT_TYPE = "X-AGTP-Document-Type"
HEADER_APPLICATION = "X-AGTP-Application"
HEADER_APPLICATION_VERSION = "X-AGTP-Application-Version"

DOC_TYPE_AGENT_DOCUMENT = "agtp.agent.document"
DOC_TYPE_SERVER_MANIFEST = "agtp.server.manifest"
DOC_TYPE_SERVER_IDENTITY = "agtp.server.identity"

DOCUMENT_VERSION_V2 = "v2"
DOCUMENT_VERSION_V1_MIGRATED = "v1-migrated"


# Canonical key order for serialization. Wire format follows this
# ordering so agent.json files are byte-stable across implementations.
FIELD_ORDER = [
    "agtp_version",
    "document_version",
    "agent_id",
    "name",
    "role",
    "principal",
    "principal_id",
    "description",
    "status",
    "skills",
    "requires",
    "scopes_accepted",
    "trust_tier",
    "verification_path",
    "trust_warning",
    "trust_score",
    "trust_score_computed_at",
    "owner_id",
    "issued_at",
    "issuer",
    "manifest_issuer",
    "manifest_issuer_public_key",
    "manifest_signature",
    "policies",
]


# Trust posture enumerations. Mirrors core.genesis on purpose so the
# two stay aligned; AgentDocument validates against the same vocab.
VALID_TRUST_TIERS = (1, 2, 3)

VALID_VERIFICATION_PATHS = frozenset({
    # AGTP-TRUST §verification-path canonical enum:
    "dns-anchored",   # Tier 1 — DNS record + CA chain
    "log-anchored",   # Tier 1 — transparency-log inclusion proof
    "hybrid",         # Tier 1 — DNS + blockchain anchor
    "org-asserted",   # Tier 2 — organization-signed, no external proof
    # Code-only extension. Marks dev / local / test deployments
    # where no organizational attestation exists. Treated as Tier 2
    # for trust-posture purposes; distinguished from "org-asserted"
    # only so operators can see at a glance that the AgentDocument
    # was issued without any external signing authority.
    "self-signed",
})

# Identity role per draft-hood-agtp-merchant-identity-02. ``agent``
# is the default; ``merchant`` agents gate inbound PURCHASE through
# mod_merchant.
VALID_ROLES = frozenset({
    "agent",
    "merchant",
})

DEFAULT_TRUST_TIER = 2
DEFAULT_VERIFICATION_PATH = "self-signed"
DEFAULT_ROLE = "agent"

# Per draft-hood-independent-agtp §6.2: every Tier 2 Agent Document
# MUST carry a trust_warning field declaring the verification status.
# The daemon auto-populates this when the AgentDocument doesn't set
# it explicitly.
TIER_2_TRUST_WARNING = "verification-incomplete"


@dataclass
class RequiresDeclaration:
    """
    Methods, scopes, and wildcard policy the agent declares as
    inbound-handleable. ``methods`` is the dispatch surface; ``scopes``
    list the authority tokens the agent expects to be presented;
    ``wildcards`` is true for orchestrators that accept any method.
    """

    methods: List[str] = field(default_factory=list)
    scopes: List[str] = field(default_factory=list)
    wildcards: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "methods": list(self.methods),
            "scopes": list(self.scopes),
            "wildcards": bool(self.wildcards),
        }


@dataclass
class AgentDocument:
    """The v2 Agent Document."""

    agtp_version: str
    agent_id: str
    name: str
    principal: str
    principal_id: str
    description: str
    status: str                # "active" | "suspended" | "retired" | "deprecated"
    skills: List[str]
    requires: RequiresDeclaration
    scopes_accepted: List[str]
    issued_at: str             # ISO 8601 UTC
    issuer: str
    document_version: str = DOCUMENT_VERSION_V2
    # Phase 5 trust posture. Defaults match the most conservative
    # interpretation: org-asserted (tier 2), no verification path
    # claimed. The daemon's loader auto-populates from the agent's
    # Agent Genesis when one is loaded alongside and the
    # AgentDocument doesn't declare these fields explicitly.
    trust_tier: int = DEFAULT_TRUST_TIER
    verification_path: str = DEFAULT_VERIFICATION_PATH
    trust_warning: str = ""
    # AGTP-TRUST §trust-score: behavioral trust score in [0.0, 1.0]
    # with at least 2 decimal precision. Distinct from trust_tier
    # (which is the binary-ish identity-verification posture);
    # trust_score is a continuous behavioral metric optionally
    # computed by governance platforms over time. Optional;
    # implementations that compute it MUST also populate
    # trust_score_computed_at. v00 daemon ships the slot for
    # spec conformance but does not compute scores itself.
    trust_score: Optional[float] = None
    # ISO 8601 UTC timestamp (Z-suffixed) when trust_score was
    # computed. MUST be present when trust_score is present;
    # otherwise the score is meaningless to relying parties that
    # enforce freshness.
    trust_score_computed_at: str = ""
    # Phase 4 cross-reference: when the agent has a Genesis, this is
    # its owner_id (the legal entity that registered the agent).
    # Surfaced on responses as the Owner-ID header by the daemon.
    # Empty string when no Genesis backs this agent (transport-only
    # identity).
    owner_id: str = ""
    # Phase 7 role per draft-hood-agtp-merchant-identity-02. Mirrors
    # AgentGenesis.role. ``agent`` (default) follows the standard
    # dispatch path; ``merchant`` triggers mod_merchant's PURCHASE
    # gate when that module is loaded.
    role: str = DEFAULT_ROLE
    # Per-agent policy overrides. Keyed by policy domain:
    #   ``oauth``  — overrides for the server-wide
    #                ``[policies.oauth]`` block (one agent on a
    #                multi-tenant server may require Bearer tokens
    #                while another doesn't).
    #   ``rcns``   — reserved for future per-agent RCNS overrides.
    #
    # Free-form by design: the daemon reads the keys it knows
    # about and ignores the rest, so application-specific
    # policies can ride here without schema bumps. Empty by
    # default — Pattern 1 deployments don't notice the field.
    policies: Dict[str, Any] = field(default_factory=dict)
    # Tier 3.2 — optional registrar attestation of the AgentDocument
    # itself (separate from the Agent Genesis attestation). A signed
    # manifest lets verifiers confirm the document's mutable fields
    # (skills, requires, trust_tier claim, etc.) reflect what the
    # registrar attested — not just what the operator unilaterally
    # wrote. Empty strings = unsigned manifest (operator-only
    # assertion, the default).
    manifest_issuer: str = ""
    manifest_issuer_public_key: str = ""
    """PEM-encoded Ed25519 public key. Verifier uses this to check
    the signature."""
    manifest_signature: str = ""
    """base64url-encoded Ed25519 signature over the canonical-JSON
    form of this document with manifest_signature set to "" (so the
    signature isn't self-referential)."""

    def __post_init__(self) -> None:
        if self.trust_tier not in VALID_TRUST_TIERS:
            raise ValueError(
                f"trust_tier must be one of {VALID_TRUST_TIERS}; "
                f"got {self.trust_tier!r}"
            )
        if self.verification_path not in VALID_VERIFICATION_PATHS:
            raise ValueError(
                f"verification_path must be one of "
                f"{sorted(VALID_VERIFICATION_PATHS)}; "
                f"got {self.verification_path!r}"
            )
        if self.role not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_ROLES)}; got {self.role!r}"
            )
        # Per §6.2: Tier 2 documents MUST carry trust_warning.
        # Auto-populate when the operator hasn't set it explicitly so
        # the wire shape always satisfies the spec.
        if self.trust_tier == 2 and not self.trust_warning:
            self.trust_warning = TIER_2_TRUST_WARNING
        # AGTP-TRUST §trust-score: range [0.0, 1.0]; out-of-range
        # MUST be rejected. trust_score_computed_at MUST accompany
        # any populated score.
        if self.trust_score is not None:
            if not (0.0 <= self.trust_score <= 1.0):
                raise ValueError(
                    f"trust_score must be in [0.0, 1.0]; "
                    f"got {self.trust_score!r}"
                )
            if not self.trust_score_computed_at:
                raise ValueError(
                    "trust_score is set but trust_score_computed_at "
                    "is empty; both fields are required together"
                )

    @property
    def is_migrated(self) -> bool:
        """True for documents auto-converted from v1 at load time."""
        return self.document_version == DOCUMENT_VERSION_V1_MIGRATED

    @property
    def capabilities(self) -> List[str]:
        """
        Backward-compatible alias for ``requires.methods``. Older code
        paths and downstream tools that read ``.capabilities`` continue
        to work; new code prefers ``requires.methods``.
        """
        return list(self.requires.methods)

    def manifest_issuer_public_key_b64url_raw(self) -> str:
        """Return :attr:`manifest_issuer_public_key` in the spec-
        canonical base64url-of-raw-bytes form.

        The dataclass stores keys in whatever format the source
        file used (typically PEM today). Spec-aligned consumers —
        cross-implementation verifiers expecting the format from
        AGTP-IDENTIFIERS — call this helper to get the 32 raw
        bytes encoded with URL-safe base64 (no padding), per
        RFC 8032 §5.1.2.

        Idempotent: input already in b64url-raw returns unchanged.
        Empty string when the document is unsigned (no manifest
        attestation). Raises
        :class:`core.key_encoding.KeyEncodingError` when the
        stored key isn't a valid Ed25519 key in either format.
        """
        from core.key_encoding import detect_format, pem_to_b64url_raw
        if not self.manifest_issuer_public_key:
            return ""
        if detect_format(self.manifest_issuer_public_key) == "pem":
            return pem_to_b64url_raw(self.manifest_issuer_public_key)
        return self.manifest_issuer_public_key.strip()

    def manifest_issuer_public_key_fingerprint(self) -> str:
        """Spec-canonical fingerprint of the manifest-issuer key:
        64-char lowercase hex of ``sha256(raw_ed25519_public_key_bytes)``.

        Empty string when the document is unsigned. Used by
        relying parties to identify which registrar key signed
        the AgentDocument without trusting an untrusted in-band
        lookup.
        """
        from core.key_encoding import fingerprint_b64url_raw
        if not self.manifest_issuer_public_key:
            return ""
        return fingerprint_b64url_raw(self.manifest_issuer_public_key)

    def accepts_method(self, method_name: str) -> bool:
        """
        True when this agent will dispatch ``method_name`` inbound.

        Wildcard agents accept anything. Strict agents accept only
        methods listed in ``requires.methods``.
        """
        if self.requires.wildcards:
            return True
        return method_name.upper() in {m.upper() for m in self.requires.methods}

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict in canonical field order. Always emits v2.

        Optional empty-string fields (``trust_warning``, ``owner_id``)
        are elided to keep the serialized shape clean. Tier 1 agents
        don't need a trust_warning and agents without a Genesis
        don't have an owner_id; emitting empty strings would clutter
        every agent.json file.
        """
        raw = asdict(self)
        # asdict converts the nested dataclass; rewrite using
        # RequiresDeclaration.to_dict for stable key order.
        raw["requires"] = self.requires.to_dict()
        out: Dict[str, Any] = {}
        for key in FIELD_ORDER:
            if key == "document_version":
                # Migrated documents are emitted as clean v2.
                out[key] = (
                    DOCUMENT_VERSION_V2
                    if self.is_migrated
                    else self.document_version
                )
                continue
            value = raw[key]
            # Elide empty-string optional fields.
            if key in (
                "trust_warning",
                "trust_score_computed_at",
                "owner_id",
                "manifest_issuer",
                "manifest_issuer_public_key",
                "manifest_signature",
            ) and not value:
                continue
            # Elide trust_score when not set (None).
            if key == "trust_score" and value is None:
                continue
            # Elide default role to keep agent.json files clean —
            # only merchant role is interesting at the document level.
            if key == "role" and value == DEFAULT_ROLE:
                continue
            # Elide empty per-agent policies dict — most agents have
            # no per-agent overrides and the field shouldn't bloat
            # the on-disk shape.
            if key == "policies" and not value:
                continue
            out[key] = value
        return out

    def to_canonical_json(self, *, exclude_signature: bool = False) -> str:
        """Return RFC 8785-style canonical JSON: sorted keys, no
        whitespace. The form Merchant-Manifest-Fingerprint hashes
        over (Phase 7); also a stable byte form for any other
        cryptographic binding that wants a content hash.

        When ``exclude_signature`` is true, the
        ``manifest_signature`` field is removed before serializing —
        the form that the manifest signature itself covers. The
        ``manifest_issuer`` and ``manifest_issuer_public_key`` fields
        STAY in the signed form so a verifier can identify which key
        to use without trusting an untrusted in-band lookup.
        """
        d = self.to_dict()
        if exclude_signature:
            d.pop("manifest_signature", None)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def sign_manifest(self, issuer_private_key) -> None:
        """Sign this document in place with the supplied Ed25519
        registrar key. Sets ``manifest_signature`` (base64url).
        Caller is responsible for populating ``manifest_issuer`` and
        ``manifest_issuer_public_key`` before signing (otherwise
        verifiers can't tell which key to use).
        """
        if not self.manifest_issuer_public_key:
            raise ValueError(
                "manifest_issuer_public_key must be set before signing"
            )
        import base64 as _b64
        canonical = self.to_canonical_json(exclude_signature=True)
        sig = issuer_private_key.sign(canonical.encode("utf-8"))
        self.manifest_signature = (
            _b64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
        )

    def verify_manifest_signature(self) -> None:
        """Verify the embedded manifest signature against
        ``manifest_issuer_public_key``.

        Raises ``ValueError`` on any failure (missing signature,
        malformed key, bad signature). Returns ``None`` on success.

        Callers MUST additionally verify the issuer's identity
        (i.e., that ``manifest_issuer_public_key`` belongs to a
        trusted registrar) — this method only checks the signature
        is intact, not that the signer is trusted.
        """
        if not self.manifest_signature:
            raise ValueError("AgentDocument has no manifest_signature")
        if not self.manifest_issuer_public_key:
            raise ValueError(
                "AgentDocument has no manifest_issuer_public_key — "
                "can't verify"
            )
        import base64 as _b64
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        try:
            issuer = serialization.load_pem_public_key(
                self.manifest_issuer_public_key.encode("utf-8"),
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"manifest_issuer_public_key is not a valid PEM key: {exc}"
            ) from exc
        if not isinstance(issuer, Ed25519PublicKey):
            raise ValueError(
                f"manifest_issuer_public_key is not Ed25519: "
                f"{type(issuer).__name__}"
            )
        padded = self.manifest_signature + "=" * (
            -len(self.manifest_signature) % 4
        )
        try:
            sig_bytes = _b64.urlsafe_b64decode(padded)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"manifest_signature is not valid base64url: {exc}"
            ) from exc
        canonical = self.to_canonical_json(exclude_signature=True)
        try:
            issuer.verify(sig_bytes, canonical.encode("utf-8"))
        except InvalidSignature as exc:
            raise ValueError(
                "manifest_signature does not verify against "
                "manifest_issuer_public_key"
            ) from exc

    def manifest_fingerprint(self) -> str:
        """Compute ``sha256(canonical AgentDocument JSON)`` hex-encoded.

        Used by Phase-7 ``mod_merchant`` to verify the inbound
        ``Merchant-Manifest-Fingerprint`` header: the buyer fetched
        the AgentDocument, hashed it, and sent the hash with their
        PURCHASE. The merchant verifies by recomputing locally and
        comparing — proves the manifest didn't change between fetch
        and purchase. The same helper drives DISCOVER /agents listing
        entries so clients can pin a manifest by its fingerprint.
        """
        import hashlib as _hashlib
        return _hashlib.sha256(
            self.to_canonical_json().encode("utf-8"),
        ).hexdigest()

    def to_json(self, *, pretty: bool = True) -> str:
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def to_yaml(self) -> str:
        """
        Compact YAML emitter (avoids the PyYAML dependency). Handles
        the nested ``requires`` mapping.
        """
        d = self.to_dict()
        lines: List[str] = []
        for key in FIELD_ORDER:
            # to_dict elides optional empty-string fields; skip them
            # in the YAML rendering too.
            if key not in d:
                continue
            value = d[key]
            if key == "requires":
                lines.append("requires:")
                lines.append(f"  methods: {_yaml_inline_list(value['methods'])}")
                lines.append(f"  scopes:  {_yaml_inline_list(value['scopes'])}")
                lines.append(f"  wildcards: {str(bool(value['wildcards'])).lower()}")
                continue
            if isinstance(value, list):
                if not value:
                    lines.append(f"{key}: []")
                else:
                    lines.append(f"{key}:")
                    for item in value:
                        lines.append(f"  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{key}: {_yaml_scalar(value)}")
        return "\n".join(lines) + "\n"


def _yaml_inline_list(items: List[Any]) -> str:
    if not items:
        return "[]"
    rendered = ", ".join(_yaml_scalar(i) for i in items)
    return f"[{rendered}]"


def _yaml_scalar(value: Any) -> str:
    """Emit a YAML scalar with appropriate quoting."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return '""'
    needs_quoting = (
        ":" in text
        or "," in text
        or text != text.strip()
        or text[0] in "!&*[{|>%@`"
        or text.lower() in ("yes", "no", "true", "false", "null")
    )
    if needs_quoting:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


_V2_REQUIRED_KEYS = {
    "agtp_version", "agent_id", "name", "principal", "principal_id",
    "description", "status", "skills", "requires", "scopes_accepted",
    "issued_at", "issuer",
}

_V1_SHAPE_KEYS = {"capabilities"}


def is_v1_document(data: Dict[str, Any]) -> bool:
    """Heuristic: v1 documents have ``capabilities`` and lack v2 fields."""
    if "capabilities" not in data:
        return False
    if "skills" in data and "requires" in data:
        return False
    return True


def _seed_skills_from_description(description: str) -> List[str]:
    """v1 didn't carry a skills array, so we synthesize a single entry."""
    description = (description or "").strip()
    if not description:
        return []
    return [description]


def from_dict(data: Dict[str, Any]) -> AgentDocument:
    """
    Construct an AgentDocument from parsed JSON.

    Detects v1 vs v2 by shape and dispatches. v2 documents are loaded
    as-is. v1 documents go through ``from_dict_v1_compat`` and emerge
    as in-memory v2 with ``document_version="v1-migrated"``.
    """
    if is_v1_document(data):
        return from_dict_v1_compat(data)

    missing = sorted(_V2_REQUIRED_KEYS - set(data.keys()))
    if missing:
        raise ValueError(
            f"Agent Document missing required fields: {', '.join(missing)}"
        )

    requires_block = data["requires"]
    if not isinstance(requires_block, dict):
        raise ValueError("'requires' must be a mapping with methods/scopes/wildcards")

    requires = RequiresDeclaration(
        methods=list(requires_block.get("methods", [])),
        scopes=list(requires_block.get("scopes", [])),
        wildcards=bool(requires_block.get("wildcards", False)),
    )

    return AgentDocument(
        agtp_version=str(data["agtp_version"]),
        document_version=str(data.get("document_version", DOCUMENT_VERSION_V2)),
        agent_id=str(data["agent_id"]),
        name=str(data["name"]),
        principal=str(data["principal"]),
        principal_id=str(data["principal_id"]),
        description=str(data["description"]),
        status=str(data["status"]),
        skills=list(data["skills"]),
        requires=requires,
        scopes_accepted=list(data["scopes_accepted"]),
        trust_tier=int(data.get("trust_tier", DEFAULT_TRUST_TIER)),
        verification_path=str(
            data.get("verification_path", DEFAULT_VERIFICATION_PATH)
        ),
        trust_warning=str(data.get("trust_warning") or ""),
        trust_score=(
            float(data["trust_score"])
            if data.get("trust_score") is not None
            else None
        ),
        trust_score_computed_at=str(
            data.get("trust_score_computed_at") or ""
        ),
        owner_id=str(data.get("owner_id") or ""),
        role=str(data.get("role") or DEFAULT_ROLE),
        policies=dict(data.get("policies") or {}),
        manifest_issuer=str(data.get("manifest_issuer") or ""),
        manifest_issuer_public_key=str(
            data.get("manifest_issuer_public_key") or ""
        ),
        manifest_signature=str(data.get("manifest_signature") or ""),
        issued_at=str(data["issued_at"]),
        issuer=str(data["issuer"]),
    )


def from_dict_v1_compat(data: Dict[str, Any]) -> AgentDocument:
    """
    Convert a v1 Agent Document dict into the v2 in-memory shape.

    Mapping:
      capabilities -> requires.methods
      <none>       -> skills (seeded from description)
      <none>       -> requires.scopes (empty)
      <none>       -> requires.wildcards (false)

    The resulting document carries ``document_version="v1-migrated"``
    so callers can warn the operator that the source file is older.
    """
    legacy_required = {
        "agtp_version", "agent_id", "name", "principal", "principal_id",
        "description", "status", "capabilities", "scopes_accepted",
        "issued_at", "issuer",
    }
    missing = sorted(legacy_required - set(data.keys()))
    if missing:
        raise ValueError(
            f"v1 Agent Document missing required fields: {', '.join(missing)}"
        )

    requires = RequiresDeclaration(
        methods=list(data.get("capabilities", [])),
        scopes=[],
        wildcards=False,
    )
    skills = _seed_skills_from_description(data.get("description", ""))

    return AgentDocument(
        agtp_version=str(data["agtp_version"]),
        document_version=DOCUMENT_VERSION_V1_MIGRATED,
        agent_id=str(data["agent_id"]),
        name=str(data["name"]),
        principal=str(data["principal"]),
        principal_id=str(data["principal_id"]),
        description=str(data["description"]),
        status=str(data["status"]),
        skills=skills,
        requires=requires,
        scopes_accepted=list(data["scopes_accepted"]),
        issued_at=str(data["issued_at"]),
        issuer=str(data["issuer"]),
    )


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CONTENT_TYPE_JSON",
    "CONTENT_TYPE_YAML",
    "CONTENT_TYPE_HTML",
    "CONTENT_TYPE_MANIFEST_JSON",
    "DEFAULT_ROLE",
    "DEFAULT_TRUST_TIER",
    "DEFAULT_VERIFICATION_PATH",
    "DOC_TYPE_AGENT_DOCUMENT",
    "DOC_TYPE_SERVER_IDENTITY",
    "DOC_TYPE_SERVER_MANIFEST",
    "DOCUMENT_VERSION_V1_MIGRATED",
    "DOCUMENT_VERSION_V2",
    "FIELD_ORDER",
    "HEADER_APPLICATION",
    "HEADER_APPLICATION_VERSION",
    "HEADER_DOCUMENT_TYPE",
    "TIER_2_TRUST_WARNING",
    "VALID_ROLES",
    "VALID_TRUST_TIERS",
    "VALID_VERIFICATION_PATHS",
    "AgentDocument",
    "RequiresDeclaration",
    "from_dict",
    "from_dict_v1_compat",
    "is_v1_document",
    "utc_now_iso",
]

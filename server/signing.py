"""
Ed25519 signing service for ``agtpd``.

The single source of cryptographic identity for the daemon. When
signing is enabled, the daemon loads an Ed25519 private key at boot
and signs:

  * **Attribution-Record headers** on every response — emitted as
    JWS Compact Serialization (RFC 7515 §3.1) so any JWS-aware
    verifier (jose libraries, Python ``jwt``) can validate them.
    The companion ``Audit-ID`` header carries
    ``sha256(jws_compact)``, anchoring the per-agent hash chain.
  * **Audit log receipts** emitted by ``mod_audit`` (when audit
    signing is enabled). Today these reuse the canonical-JSON
    signing helper; the SCITT/COSE_Sign1 wrapper for AGTP-LOG
    integration lands in a later phase.
  * **Server Manifest** at DISCOVER time (future revision)
  * **AGTP-LOG entries** for SCITT-style transparency log integration
    (future revision)

The signing service is also the surface the gateway protocol's v2
``sign_request`` capability consumes: modules ask the daemon to sign
opaque bytes and the daemon returns the Ed25519 signature without
exposing the private key. The ``sign()`` and ``sign_canonical()``
methods are the API shape the gateway frame handler calls into.

## Key layout

Private and public keys live in PEM-encoded files. Defaults:

    /etc/agtpd/signing.key   chmod 0600, owned by the daemon user
    /etc/agtpd/signing.pub   chmod 0644, distributable

Operators generate the pair with ``tools/generate_signing_key.py``;
the daemon refuses to start with a malformed or unreadable key file
when signing is enabled.

## Why Ed25519

Per the AGTP-LOG draft and the Agent-Cert draft, Ed25519 is the
mandatory-to-implement signature algorithm. It's fast, deterministic
(no nonce reuse hazard), the keys are 32 bytes, and signatures are
64 bytes — small enough to ride in a header without ceremony.

The signing service is algorithm-agnostic at the API surface: when
RFC-9942-and-later additions arrive, this module gains a new
backend class without callers needing to change.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class SigningError(Exception):
    """Raised when signing or verification fails."""


class KeyLoadError(SigningError):
    """Raised when the configured key file cannot be loaded."""


class AttributionRecordError(SigningError):
    """Raised when an Attribution-Record cannot be parsed or verified."""


def _b64url(data: bytes) -> str:
    """RFC 4648 §5 base64url encoding without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Inverse of :func:`_b64url`. Pads as needed."""
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def _canonical(payload: Dict[str, Any]) -> str:
    """RFC 8785-style canonical JSON: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class AttributionRecord:
    """A built (signed or unsigned) Attribution-Record.

    ``jws`` is the compact-serialization string the daemon stamps as
    the ``Attribution-Record`` response header.

    ``audit_id`` is ``sha256(jws)`` hex-encoded, stamped on the
    response as the ``Audit-ID`` header and used by the next record's
    ``previous_audit_id`` field to chain.

    ``payload`` is the decoded payload dict, surfaced for tests and
    in-daemon consumers (e.g., mod_audit writing JSONL); it is not
    part of the wire shape.
    """

    jws: str
    audit_id: str
    payload: Dict[str, Any] = field(default_factory=dict)


def parse_attribution_record(jws: str) -> tuple[Dict[str, Any], Dict[str, Any], bytes]:
    """Parse a compact-form Attribution-Record without verifying it.

    Returns ``(header, payload, signature_bytes)``. ``signature_bytes``
    is empty for ``alg: none`` (unsecured) records. Raises
    :class:`AttributionRecordError` for structural failures.

    Callers that need cryptographic verification combine this with
    :meth:`SigningService.verify_attribution_record` or
    :func:`verify_attribution_record` (when verifying against a key
    other than the local daemon's).
    """
    parts = jws.split(".")
    if len(parts) != 3:
        raise AttributionRecordError(
            f"Attribution-Record must have three dot-separated segments, "
            f"got {len(parts)}"
        )
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AttributionRecordError(
            f"Attribution-Record segments are not valid JSON: {exc}"
        ) from exc
    signature = _b64url_decode(parts[2]) if parts[2] else b""
    return header, payload, signature


def audit_id_for(jws: str) -> str:
    """Return the canonical ``audit_id`` for a JWS-compact Attribution-Record.

    Exported so external consumers (inspector, mod_audit reader) can
    compute audit_ids without re-implementing the rule. The rule is
    ``sha256(jws_ascii_bytes).hexdigest()``.
    """
    return hashlib.sha256(jws.encode("ascii")).hexdigest()


def verify_attribution_record(
    jws: str, public_key: Ed25519PublicKey,
) -> Dict[str, Any]:
    """Verify a JWS Compact Attribution-Record with the given public key.

    Returns the decoded payload on success. Raises
    :class:`AttributionRecordError` when the JWS is malformed, when
    the header announces an algorithm other than EdDSA / none, or
    when the signature does not validate.

    For ``alg: none`` (unsecured) records the signature segment must
    be empty; the function returns the payload but the caller MUST
    treat the result as advisory only.
    """
    header, payload, signature = parse_attribution_record(jws)
    alg = header.get("alg")
    if alg == "none":
        if signature:
            raise AttributionRecordError(
                "alg: none record carries a non-empty signature segment"
            )
        return payload
    if alg != "EdDSA":
        raise AttributionRecordError(
            f"unsupported Attribution-Record alg: {alg!r}"
        )
    parts = jws.split(".")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    try:
        public_key.verify(signature, signing_input)
    except InvalidSignature as exc:
        raise AttributionRecordError(
            "Attribution-Record signature did not verify"
        ) from exc
    return payload


class SigningService:
    """Holds the daemon's Ed25519 private key and exposes signing.

    Thread-safe: signature operations are stateless (the
    cryptography library's Ed25519PrivateKey doesn't maintain
    state), but the service guards key loading and key-id derivation
    behind a lock so concurrent boot paths are safe.
    """

    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey,
        key_id: str = "",
    ) -> None:
        self._key = private_key
        self._public = private_key.public_key()
        self._key_id = key_id or self._derive_key_id(self._public)
        self._lock = threading.Lock()

    # ----- Construction -----

    @classmethod
    def from_key_path(
        cls,
        key_path: str,
        *,
        key_id: str = "",
        password: Optional[bytes] = None,
    ) -> "SigningService":
        """Load an Ed25519 private key from ``key_path``.

        The file must be PEM-encoded. Pass ``password`` for
        password-encrypted keys; ``None`` is correct for the common
        unencrypted-file case (file permissions are the protection
        mechanism).
        """
        if not key_path:
            raise KeyLoadError("signing key path is empty")
        path = Path(key_path).expanduser()
        if not path.exists():
            raise KeyLoadError(f"signing key not found: {path}")
        try:
            pem_bytes = path.read_bytes()
        except OSError as exc:
            raise KeyLoadError(f"cannot read signing key {path}: {exc}") from exc
        try:
            key = serialization.load_pem_private_key(pem_bytes, password=password)
        except Exception as exc:  # noqa: BLE001  (cryptography raises many types)
            raise KeyLoadError(
                f"signing key {path} could not be parsed as PEM: {exc}"
            ) from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise KeyLoadError(
                f"signing key {path} is not Ed25519 "
                f"(got {type(key).__name__})"
            )
        # On POSIX, warn loudly when the key file is world-readable.
        # Windows file modes don't translate cleanly, so the check
        # is best-effort. Doesn't block boot — operators may have
        # access-control via other means (ACLs, container secrets).
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077 != 0 and os.name != "nt":
                import sys as _sys
                print(
                    f"[server] WARNING: signing key {path} has mode "
                    f"{oct(mode)}; consider chmod 0600",
                    file=_sys.stderr,
                )
        except OSError:
            pass
        return cls(private_key=key, key_id=key_id)

    # ----- Introspection -----

    @property
    def key_id(self) -> str:
        """Stable identifier for this key pair.

        Computed from the public key's raw bytes when not explicitly
        configured. Carried in signed attestations so verifiers can
        select the right public key at verification time.
        """
        return self._key_id

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._public

    def public_key_pem(self) -> bytes:
        """The public key as PEM (for distribution / agent-doc embedding)."""
        return self._public.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def public_key_raw(self) -> bytes:
        """The public key as 32 raw bytes (Ed25519 canonical form)."""
        return self._public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    # ----- Signing -----

    def sign(self, data: bytes) -> bytes:
        """Sign ``data`` with the configured private key. Returns 64
        raw bytes (Ed25519 signature)."""
        if not isinstance(data, (bytes, bytearray)):
            raise SigningError(
                f"sign() requires bytes; got {type(data).__name__}"
            )
        return self._key.sign(bytes(data))

    def sign_canonical(self, payload: Dict[str, Any]) -> bytes:
        """Canonicalize ``payload`` (sorted keys, compact JSON) and
        sign the result.

        Use when callers need a stable signature over a structured
        payload (e.g., Attribution-Record). The canonical form is
        RFC-8785-shaped JSON: sorted keys, no whitespace.
        """
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return self.sign(canonical.encode("utf-8"))

    def verify(self, data: bytes, signature: bytes) -> bool:
        """Verify ``signature`` over ``data`` with this service's
        public key. Returns True on valid, False on invalid (never
        raises for normal failures).

        Used internally by tests and by the future replay-verification
        tool; agents verifying attestations from other servers should
        use those servers' published public keys, not this service.
        """
        try:
            self._public.verify(bytes(signature), bytes(data))
        except InvalidSignature:
            return False
        return True

    # ----- Attribution-Record construction (JWS Compact) -----

    def build_attribution_record(
        self,
        *,
        agent_id: str = "",
        owner_id: str = "",
        principal_id: str = "",
        server_id: str,
        session_id: str = "",
        task_id: str = "",
        request_id: str = "",
        response_id: str = "",
        issued_at: str,
        status: int,
        previous_audit_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> "AttributionRecord":
        """Produce a signed Attribution-Record in JWS Compact Serialization
        (RFC 7515 §3.1, RFC 7519 JWT-style).

        The compact form is three base64url-encoded segments separated
        by dots::

            base64url(protected_header) "." base64url(payload) "." base64url(signature)

        Header (JOSE):
            {"alg": "EdDSA", "typ": "JWT", "kid": "<key id>"}

        Payload — the AGTP identifier chain. All daemon-known fields:

            * ``agent_id``           the agent that performed the action
            * ``owner_id``           the legal owner of the agent (when
                                     known; sourced from the Agent
                                     Document / Agent Genesis)
            * ``principal_id``       the human/service the agent acts
                                     on behalf of for this request
            * ``server_id``          the daemon that produced the record
            * ``session_id``         operational grouping (echo of
                                     Session-ID header)
            * ``task_id``            task correlation (echo of Task-ID)
            * ``request_id``         request correlation
            * ``response_id``        daemon-synthesized response id
            * ``issued_at``          ISO 8601 UTC timestamp
            * ``status``             HTTP/AGTP status code of the response
            * ``previous_audit_id``  prior record's audit_id for this
                                     agent (per-agent hash chain)

        Empty-string-valued fields are omitted from the payload so
        verifiers see only what was actually known. ``extra`` rides
        unchanged under a top-level ``extra`` key when supplied;
        handlers populate it via :attr:`EndpointResponse.attribution_extra`.

        Returns an :class:`AttributionRecord` carrying:

          * ``jws``       the compact serialization, ready to stamp
                          as the ``Attribution-Record`` header value
          * ``audit_id``  ``sha256(jws_bytes).hexdigest()``, stamped
                          on the response as the ``Audit-ID`` header
                          and used by the next record's
                          ``previous_audit_id``
          * ``payload``   the decoded payload dict (test/inspection aid;
                          not part of the wire shape)
        """
        payload: Dict[str, Any] = {
            "server_id": server_id,
            "issued_at": issued_at,
            "status": status,
        }
        # Optional identifier-chain fields. Empty strings are dropped
        # so the JWS payload only carries values the daemon actually
        # observed; verifiers can rely on "field present" = "value
        # known" without checking for empty strings.
        for key, value in [
            ("agent_id", agent_id),
            ("owner_id", owner_id),
            ("principal_id", principal_id),
            ("session_id", session_id),
            ("task_id", task_id),
            ("request_id", request_id),
            ("response_id", response_id),
            ("previous_audit_id", previous_audit_id),
        ]:
            if value:
                payload[key] = value
        if extra:
            payload["extra"] = extra

        header = {
            "alg": "EdDSA",
            "typ": "JWT",
            "kid": self._key_id,
        }
        protected_b64 = _b64url(_canonical(header).encode("utf-8"))
        payload_b64 = _b64url(_canonical(payload).encode("utf-8"))
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        signature = self.sign(signing_input)
        signature_b64 = _b64url(signature)
        jws = f"{protected_b64}.{payload_b64}.{signature_b64}"
        audit_id = hashlib.sha256(jws.encode("ascii")).hexdigest()
        return AttributionRecord(
            jws=jws,
            audit_id=audit_id,
            payload=payload,
        )

    def build_unsigned_attribution_record(
        self,
        *,
        agent_id: str = "",
        owner_id: str = "",
        principal_id: str = "",
        server_id: str,
        session_id: str = "",
        task_id: str = "",
        request_id: str = "",
        response_id: str = "",
        issued_at: str,
        status: int,
        previous_audit_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> "AttributionRecord":
        """Build an unsigned Attribution-Record (``alg: none``, RFC 7515 §6).

        The wire shape stays identical to the signed form (three
        base64url-encoded segments separated by dots) so consumers can
        parse either kind with the same code. The signature segment is
        empty, signaling an unsecured JWS per RFC 7515 Appendix A.5.

        Used as a fallback when the operator has enabled
        ``attribution_records_enabled`` but has not loaded a signing
        key. Verifiers MUST treat ``alg: none`` records as advisory
        only — no cryptographic guarantee of origin.
        """
        payload: Dict[str, Any] = {
            "server_id": server_id,
            "issued_at": issued_at,
            "status": status,
        }
        for key, value in [
            ("agent_id", agent_id),
            ("owner_id", owner_id),
            ("principal_id", principal_id),
            ("session_id", session_id),
            ("task_id", task_id),
            ("request_id", request_id),
            ("response_id", response_id),
            ("previous_audit_id", previous_audit_id),
        ]:
            if value:
                payload[key] = value
        if extra:
            payload["extra"] = extra
        header = {"alg": "none", "typ": "JWT"}
        protected_b64 = _b64url(_canonical(header).encode("utf-8"))
        payload_b64 = _b64url(_canonical(payload).encode("utf-8"))
        jws = f"{protected_b64}.{payload_b64}."
        audit_id = hashlib.sha256(jws.encode("ascii")).hexdigest()
        return AttributionRecord(
            jws=jws,
            audit_id=audit_id,
            payload=payload,
        )

    # ----- Internals -----

    @staticmethod
    def _derive_key_id(public_key: Ed25519PublicKey) -> str:
        """Stable short identifier from the public key's raw bytes.

        Format: ``ed25519-`` + first 16 hex chars of SHA-256(raw
        public bytes). Matches the agent-id pattern used elsewhere
        in the protocol, abbreviated.
        """
        raw = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return "ed25519-" + hashlib.sha256(raw).hexdigest()[:16]


__all__ = [
    "AttributionRecord",
    "AttributionRecordError",
    "KeyLoadError",
    "SigningError",
    "SigningService",
    "audit_id_for",
    "parse_attribution_record",
    "verify_attribution_record",
]

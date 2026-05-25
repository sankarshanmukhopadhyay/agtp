"""
Agent Certificate verification for mTLS-secured AGTP connections.

This module implements the full set of AGTP Agent Certificate
extensions defined in ``draft-hood-agtp-agent-cert-01``:
``subject-agent-id``, ``principal-id``,
``authority-scope-commitment``, ``governance-zone``, ``trust-tier``,
``archetype``, ``activation-certificate-id``, and ``agtp-ctl-sct``.
The extension OIDs, value formats, and encode/decode helpers live in
:mod:`server.agent_cert_ext`. This module is the verifier — what runs
on every TLS handshake to surface a :class:`VerifiedCert`.

## Agent-ID binding

The transport-layer Agent-ID derivation is::

    sha256(public_key_raw_bytes).hexdigest()

where ``public_key_raw_bytes`` is the 32-byte Ed25519 public key.
This produces a 64-hex-char identifier matching the format used
elsewhere in the protocol.

When the cert carries a ``subject-agent-id`` extension, that value
is the canonical Agent-ID (sha256 of the canonical Agent Genesis
JSON, per draft-hood-agtp-agent-cert-01 §3.2.1). It is treated as
authoritative and is decoupled from the cert's public-key hash —
this is by design so the same Agent-ID can ride on multiple certs
across key rotations. The Genesis-binding cross-check
(``sha256(Genesis) == subject_agent_id`` plus Genesis signature
verification against a trusted issuer key) is layered at the
application layer in :func:`core.genesis.verify_cert_genesis_binding`;
verifiers run it once on connection establishment. When the
``subject-agent-id`` extension is absent — a vanilla TLS cert
without Agent-Cert extensions — the key-derived form is the
fallback and the daemon treats this as "transport-only" identity.

## Scope-Enforcement at the daemon

The full :class:`AgentCertExtensions` block is surfaced on every
:class:`VerifiedCert` and is what ``mod_agent_cert`` reads in its
``before_dispatch`` hook to enforce Authority-Scope and governance
zone constraints at O(1) per request, without parsing the body.
"""

from __future__ import annotations

import hashlib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Union

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from server.agent_cert_ext import (
    AgentCertExtensions,
    CertExtensionError,
    parse_extensions,
)


class MtlsError(Exception):
    """Base class for mTLS verification failures."""


class CertVerificationError(MtlsError):
    """Raised when a client certificate cannot be verified.

    The stable ``detail`` field carries a structured tag callers can
    branch on without parsing text:

      * ``not-presented``      no client cert in the TLS session
      * ``not-x509``           presented bytes aren't a valid X.509 cert
      * ``not-ed25519``        the cert's public key isn't Ed25519
      * ``expired``            current time is outside not_before/not_after
      * ``agent-id-mismatch``  header Agent-ID doesn't match cert-derived
      * ``principal-id-mismatch``  header Principal-ID doesn't match the
                                   cert's principal-id extension
                                   (AGTP-CERT §5.1)
      * ``chain-untrusted``    chain doesn't validate against the CA bundle
      * ``malformed-extension``  one of the AGTP-specific X.509 v3
                                 extensions (draft-hood-agtp-agent-cert)
                                 carries a malformed value
    """

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class VerifiedCert:
    """Result of verifying a client certificate during the TLS handshake.

    Populated when mTLS is enabled and the client presented a cert
    that validated against the configured CA bundle. Carried on the
    connection's state and surfaced into the gateway request frame
    as the ``trust`` block; in-daemon dispatch reads
    ``ctx.agent_verified`` and ``ctx.agent_cert_fingerprint``.

    The ``extensions`` block carries the parsed AGTP X.509 v3
    extensions (``draft-hood-agtp-agent-cert``). A vanilla TLS cert
    without those extensions yields an ``AgentCertExtensions`` with
    every field ``None`` — the daemon and operational modules
    (``mod_agent_cert``) treat that as "transport-only" identity and
    skip extension-driven enforcement. Full Agent Certs populate the
    extension fields and unlock Scope-Enforcement-Point checks.
    """

    agent_id: str
    """Canonical Agent-ID for this cert.

    When the cert carries a ``subject-agent-id`` extension, that
    value is the canonical Agent-ID (per AGTP-LOG §2: the hash of
    the agent's Agent Genesis document). When the extension is
    absent — a vanilla TLS cert without Agent-Cert extensions —
    the daemon falls back to the SHA-256 of the Ed25519 public
    key as a transport-only identity.

    Phase 4 deliberately does NOT require ``subject-agent-id`` to
    equal the key-derived hash. Renewable Agent Certs share an
    Agent Genesis (and therefore an Agent-ID) across cert
    rotations, but each rotation uses a fresh keypair — equating
    extension-with-key-hash would tie cert lifetime to Genesis
    lifetime, defeating the AGTP-CERT 90-day renewal model.
    Verification that ``subject-agent-id`` matches a presented
    Genesis is handled at the application / inspector layer via
    :func:`core.genesis.verify_cert_genesis_binding`."""

    fingerprint: str
    """Hex-encoded SHA-256 of the certificate DER bytes (64 chars)."""

    not_before: datetime
    """Validity window start."""

    not_after: datetime
    """Validity window end."""

    subject_common_name: str
    """The CN from the cert's subject; informational only."""

    extensions: AgentCertExtensions = field(default_factory=AgentCertExtensions)
    """Parsed AGTP X.509 v3 extensions. Default is an all-``None``
    instance for transport-only certs."""


def derive_agent_id_from_public_key(public_key: Ed25519PublicKey) -> str:
    """Canonical mapping from an Ed25519 public key to an AGTP Agent-ID.

    Returns a 64-character hex string (SHA-256 of the 32 raw public
    key bytes). The same function is used by the keygen CLI so the
    operator can predict the Agent-ID a fresh cert will produce.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


class CertVerifier:
    """Validates client certificates against an operator-configured CA bundle.

    The TLS library does the heavy lifting of chain validation when
    ``ssl.SSLContext.verify_mode = CERT_REQUIRED`` is set on the
    server socket. By the time a cert reaches ``verify_peer_cert``,
    the chain is already known good. This class extracts the
    fields the daemon cares about (Ed25519 public key, Agent-ID
    derivation, fingerprint, validity window) and surfaces them as
    a :class:`VerifiedCert`.
    """

    def __init__(self, *, ca_bundle_path: Optional[str] = None) -> None:
        """``ca_bundle_path`` is the PEM file the TLS context loads via
        ``load_verify_locations``. Stored here purely so it can be
        introspected by tests and tooling; this class doesn't re-do
        chain validation."""
        self.ca_bundle_path = ca_bundle_path

    def verify_peer_cert(self, der_bytes: bytes) -> VerifiedCert:
        """Extract the verified fields from a chain-validated cert.

        Raises :class:`CertVerificationError` when the cert can't be
        parsed, doesn't carry an Ed25519 public key, or is outside
        its validity window. Chain validity is assumed (the TLS
        library refused the handshake otherwise).
        """
        if not der_bytes:
            raise CertVerificationError(
                "no client certificate presented",
                detail="not-presented",
            )
        try:
            cert = x509.load_der_x509_certificate(der_bytes)
        except Exception as exc:  # noqa: BLE001
            raise CertVerificationError(
                f"could not parse client cert as X.509 DER: {exc}",
                detail="not-x509",
            ) from exc

        pub = cert.public_key()
        if not isinstance(pub, Ed25519PublicKey):
            raise CertVerificationError(
                f"client cert public key is not Ed25519 "
                f"(got {type(pub).__name__})",
                detail="not-ed25519",
            )

        now = datetime.now(tz=timezone.utc)
        # cryptography returns aware datetimes since 42.x; older
        # versions returned naive UTC. Normalize either case.
        not_before = self._aware(cert.not_valid_before_utc)
        not_after = self._aware(cert.not_valid_after_utc)
        if now < not_before or now > not_after:
            raise CertVerificationError(
                f"client cert outside validity window "
                f"({not_before.isoformat()} → {not_after.isoformat()})",
                detail="expired",
            )

        key_derived_agent_id = derive_agent_id_from_public_key(pub)
        fingerprint = hashlib.sha256(der_bytes).hexdigest()
        cn = self._extract_cn(cert)

        # Parse the AGTP X.509 v3 extensions. Missing extensions are
        # fine (transport-only cert); malformed extension values are a
        # protocol violation and refuse the connection.
        try:
            extensions = parse_extensions(cert)
        except CertExtensionError as exc:
            raise CertVerificationError(
                f"AGTP certificate extension malformed: {exc}",
                detail="malformed-extension",
            ) from exc

        # Authoritative Agent-ID per AGTP-LOG §2: the
        # ``subject-agent-id`` extension when present (= hash of the
        # agent's Agent Genesis document), the key-derived form
        # otherwise (transport-only identity, no Genesis backing).
        #
        # Phase 4 explicitly does NOT require the extension to equal
        # the key-derived hash. Substitution defense moves to the
        # Genesis-binding check at the application layer (chain
        # inspector, registrar verifier, ``mod_agent_cert``-aware
        # SEPs): a presented Genesis must hash to the same value as
        # ``subject-agent-id``, and the Genesis must verify against a
        # trusted issuer key. See
        # :func:`core.genesis.verify_cert_genesis_binding`.
        agent_id = extensions.subject_agent_id or key_derived_agent_id

        return VerifiedCert(
            agent_id=agent_id,
            fingerprint=fingerprint,
            not_before=not_before,
            not_after=not_after,
            subject_common_name=cn,
            extensions=extensions,
        )

    @staticmethod
    def cross_check_agent_id_header(
        verified: VerifiedCert, header_agent_id: str,
    ) -> None:
        """Refuse when an inbound ``Agent-ID`` header disagrees with the
        cert-derived identity.

        Called by the dispatcher after the cert is verified. When
        the header is empty, this is a no-op — the verified
        agent_id becomes authoritative and the daemon writes it
        back into the request's Agent-ID slot.
        """
        if not header_agent_id:
            return
        if header_agent_id.lower() != verified.agent_id.lower():
            raise CertVerificationError(
                f"Agent-ID header {header_agent_id!r} does not match "
                f"cert-derived identity {verified.agent_id!r}; the "
                f"agent presented a cert for a different identity",
                detail="agent-id-mismatch",
            )

    @staticmethod
    def cross_check_principal_id_header(
        verified: VerifiedCert, header_principal_id: str,
    ) -> None:
        """Refuse when an inbound ``Principal-ID`` header disagrees with
        the cert's ``principal-id`` extension value.

        AGTP-CERT §5.1 step 2: on the first request after TLS
        establishment, the server MUST verify that the
        ``principal-id`` extension carried on the verified cert
        matches whatever the request advertises as ``Principal-ID``.
        Mismatch MUST be refused with 401 to prevent an agent
        signing in for one principal and asserting another in the
        application layer.

        Called by the dispatcher after :meth:`cross_check_agent_id_header`.
        When either the header or the cert extension is empty this
        is a no-op — the daemon accepts the populated side as
        authoritative (the cert-supplied principal-id is the more
        trustworthy of the two when both are present anyway).
        """
        if not header_principal_id:
            return
        cert_principal_id = ""
        if verified.extensions is not None:
            cert_principal_id = verified.extensions.principal_id or ""
        if not cert_principal_id:
            return
        if header_principal_id.strip() != cert_principal_id.strip():
            raise CertVerificationError(
                f"Principal-ID header {header_principal_id!r} does not "
                f"match the cert's principal-id extension "
                f"{cert_principal_id!r}; refuse per AGTP-CERT §5.1",
                detail="principal-id-mismatch",
            )

    # ----- Internals -----

    @staticmethod
    def _aware(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _extract_cn(cert: x509.Certificate) -> str:
        try:
            attrs = cert.subject.get_attributes_for_oid(
                x509.NameOID.COMMON_NAME,
            )
            if attrs:
                value = attrs[0].value
                return value if isinstance(value, str) else value.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        return ""


def build_server_ssl_context(
    *,
    certfile: str,
    keyfile: str,
    ca_bundle_path: Optional[str] = None,
    require_client_cert: bool = False,
) -> ssl.SSLContext:
    """Construct the ``ssl.SSLContext`` for the AGTP wire listener.

    Wraps the existing TLS-server setup so the mTLS bits live with
    the verification logic instead of in ``server.main``. Callers
    pass the loaded context to ``ssl.SSLContext.wrap_socket`` on the
    server side.

    ``require_client_cert`` corresponds to ``[mtls].mode = "required"``;
    ``ca_bundle_path`` non-empty with ``require_client_cert=False``
    corresponds to ``[mtls].mode = "optional"`` (validate when
    presented, accept when absent).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    if ca_bundle_path:
        if not Path(ca_bundle_path).exists():
            raise MtlsError(
                f"mTLS CA bundle not found at {ca_bundle_path}"
            )
        ctx.load_verify_locations(cafile=ca_bundle_path)
        ctx.verify_mode = (
            ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_OPTIONAL
        )
    return ctx


__all__ = [
    "CertVerificationError",
    "CertVerifier",
    "MtlsError",
    "VerifiedCert",
    "build_server_ssl_context",
    "derive_agent_id_from_public_key",
]

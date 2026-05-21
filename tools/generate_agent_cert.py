"""
Generate an Agent Certificate (X.509 + Ed25519) for AGTP mTLS.

Two modes:

  * **Self-signed** (default). Generates a fresh Ed25519 keypair,
    writes a self-signed X.509 cert. Suitable for test deployments
    where each agent's cert IS its own trust anchor. The operator
    configures ``[mtls].ca_bundle_path`` to point at a bundle of
    the trusted self-signed certs.

  * **CA-signed.** Pass ``--ca-cert`` and ``--ca-key`` to sign the
    agent cert with an operator-managed CA. The agent cert is then
    valid against any deployment that trusts the same CA.

The CLI also writes the AGTP X.509 v3 extensions defined in
``draft-hood-agtp-agent-cert-00`` when the matching flags are
supplied. Without them the cert is a vanilla TLS cert (Phase 2
shape) and the daemon treats it as transport-only identity. With
them the cert becomes a full Agent Certificate and unlocks
``mod_agent_cert``'s O(1) Scope-Enforcement at the daemon edge.

Output:

  * ``<output>.key`` — private key (PEM, 0o600)
  * ``<output>.crt`` — certificate (PEM, 0o644)

Prints the derived ``agent_id`` so the operator can copy it into the
agent's identity document (or use it as the Agent-ID header value
in a sanity check; the daemon will derive the same value on
verification).

Usage::

    # Transport-only Agent Cert (Phase 2 shape):
    python -m tools.generate_agent_cert agents/lauren

    # CA-signed Agent Cert with full extensions (Phase 3):
    python -m tools.generate_agent_cert agents/lauren \\
        --ca-cert ca.crt --ca-key ca.key \\
        --principal-id chris@nomotic.ai \\
        --authority-scope bookings:write --authority-scope ledger:read \\
        --governance-zone zone:finance \\
        --trust-tier 1 \\
        --archetype analyst \\
        --activation-certificate-id <agent-genesis-hash>
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID

# Reuse the daemon's Agent-ID derivation so the CLI's printed value
# matches what the verifier computes at runtime. Sharing the function
# is what keeps the off-line and on-line views in lockstep.
from server.agent_cert_ext import (
    CertExtensionError,
    add_activation_certificate_id,
    add_archetype,
    add_authority_scope_commitment,
    add_governance_zone,
    add_principal_id,
    add_subject_agent_id,
    add_trust_tier,
    VALID_ARCHETYPES,
    VALID_TRUST_TIERS,
)
from server.mtls import derive_agent_id_from_public_key


def _load_ca(
    cert_path: str, key_path: str,
) -> tuple[x509.Certificate, Ed25519PrivateKey]:
    cert_pem = Path(cert_path).read_bytes()
    ca_cert = x509.load_pem_x509_certificate(cert_pem)
    key_pem = Path(key_path).read_bytes()
    ca_key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(ca_key, Ed25519PrivateKey):
        raise SystemExit(
            f"CA key at {key_path} is not Ed25519 "
            f"(got {type(ca_key).__name__})"
        )
    return ca_cert, ca_key


def _build_cert(
    *,
    subject_key: Ed25519PublicKey,
    issuer_name: x509.Name,
    issuer_key: Ed25519PrivateKey,
    common_name: str,
    valid_days: int,
    is_ca: bool = False,
    organization: Optional[str] = None,
    organizational_unit: Optional[str] = None,
    email: Optional[str] = None,
    subject_agent_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    authority_scopes: Optional[list[str]] = None,
    governance_zone: Optional[str] = None,
    trust_tier: Optional[int] = None,
    archetype: Optional[str] = None,
    activation_certificate_id: Optional[str] = None,
) -> x509.Certificate:
    name_attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    if organization:
        name_attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    if organizational_unit:
        name_attrs.append(
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit),
        )
    if email:
        name_attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    subject_name = x509.Name(name_attrs)

    now = datetime.now(tz=timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(issuer_name)
        .public_key(subject_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.BasicConstraints(ca=is_ca, path_length=None),
            critical=True,
        )
    )
    if not is_ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )

    # AGTP Agent Certificate extensions. Each is opt-in via its CLI
    # flag; absent flags leave the cert as a vanilla TLS cert that
    # the daemon treats as transport-only identity.
    if subject_agent_id is not None:
        builder = add_subject_agent_id(builder, subject_agent_id)
    if principal_id is not None:
        builder = add_principal_id(builder, principal_id)
    if authority_scopes is not None:
        builder = add_authority_scope_commitment(builder, authority_scopes)
    if governance_zone is not None:
        builder = add_governance_zone(builder, governance_zone)
    if trust_tier is not None:
        builder = add_trust_tier(builder, trust_tier)
    if archetype is not None:
        builder = add_archetype(builder, archetype)
    if activation_certificate_id is not None:
        builder = add_activation_certificate_id(builder, activation_certificate_id)

    return builder.sign(private_key=issuer_key, algorithm=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_agent_cert",
        description="Generate an Agent Certificate (X.509 + Ed25519) for AGTP mTLS.",
    )
    parser.add_argument(
        "output",
        help=(
            "Base path for the output files. Writes <output>.key and "
            "<output>.crt."
        ),
    )
    parser.add_argument(
        "--common-name",
        default="",
        help=(
            "Cert Common Name (CN). Defaults to the output basename. "
            "Informational only; the Agent-ID derives from the public key."
        ),
    )
    parser.add_argument(
        "--valid-days",
        type=int,
        default=365,
        help="Certificate validity in days. Defaults to 365.",
    )
    parser.add_argument("--ca-cert", help="PEM-encoded CA cert to sign with.")
    parser.add_argument("--ca-key", help="PEM-encoded CA private key.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output files.",
    )

    # Standard X.509 subject fields per AGTP-CERT §4.1.1.
    parser.add_argument(
        "--organization",
        help="Subject O field (operator/principal organization).",
    )
    parser.add_argument(
        "--organizational-unit",
        help="Subject OU field (governance zone label).",
    )
    parser.add_argument(
        "--email",
        help="Subject emailAddress (contact for the responsible principal).",
    )

    # AGTP X.509 v3 extensions per AGTP-CERT §4.1.2. All optional;
    # omitting a flag drops the extension. The cert remains valid TLS
    # but the daemon treats it as transport-only.
    parser.add_argument(
        "--subject-agent-id",
        help=(
            "subject-agent-id extension value (64 hex). When omitted, the "
            "extension is also omitted; the daemon falls back to the "
            "key-derived Agent-ID. When supplied, MUST equal the "
            "key-derived value (the daemon refuses mismatched certs)."
        ),
    )
    parser.add_argument(
        "--principal-id",
        help="principal-id extension value (UTF-8 string ≤256 bytes).",
    )
    parser.add_argument(
        "--authority-scope",
        action="append",
        default=[],
        help=(
            "Authority-Scope token to commit to. Repeatable. The "
            "extension is built from the lexicographically sorted, "
            "deduplicated set of all supplied values."
        ),
    )
    parser.add_argument(
        "--governance-zone",
        help="governance-zone extension value (e.g., 'zone:finance').",
    )
    parser.add_argument(
        "--trust-tier",
        type=int,
        choices=list(VALID_TRUST_TIERS),
        help="trust-tier extension value (1, 2, or 3).",
    )
    parser.add_argument(
        "--archetype",
        choices=sorted(VALID_ARCHETYPES),
        help="archetype extension value.",
    )
    parser.add_argument(
        "--activation-certificate-id",
        help=(
            "activation-certificate-id extension value (64 hex). "
            "Cross-layer reference to the Agent Genesis hash."
        ),
    )

    args = parser.parse_args(argv)

    base = Path(args.output)
    key_path = base.with_suffix(".key")
    crt_path = base.with_suffix(".crt")

    if not args.force:
        for p in (key_path, crt_path):
            if p.exists():
                print(
                    f"refusing to overwrite existing file: {p} (use --force)",
                    file=sys.stderr,
                )
                return 2

    if base.parent and not base.parent.exists():
        base.parent.mkdir(parents=True, exist_ok=True)

    # Agent keypair.
    agent_key = Ed25519PrivateKey.generate()
    agent_pub = agent_key.public_key()
    common_name = args.common_name or base.name

    # Issuer = self when no CA provided.
    if args.ca_cert and args.ca_key:
        ca_cert, ca_key = _load_ca(args.ca_cert, args.ca_key)
        issuer_name = ca_cert.subject
        issuer_key = ca_key
        signed_by = "CA-signed"
    elif args.ca_cert or args.ca_key:
        print(
            "Both --ca-cert and --ca-key are required to sign with a CA.",
            file=sys.stderr,
        )
        return 2
    else:
        issuer_name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        issuer_key = agent_key
        signed_by = "self-signed"

    agent_id = derive_agent_id_from_public_key(agent_pub)

    # When the caller explicitly supplies --subject-agent-id, it MUST
    # equal the key-derived Agent-ID. The daemon enforces the same
    # invariant at verification time; refusing here gives a clearer
    # error message than a 401 on the wire.
    if args.subject_agent_id and args.subject_agent_id.lower() != agent_id:
        print(
            f"--subject-agent-id {args.subject_agent_id!r} does not match "
            f"the key-derived Agent-ID {agent_id!r}.",
            file=sys.stderr,
        )
        return 2

    # Default: when any AGTP extension flag is present, also write
    # subject-agent-id so the cert is recognizably a full Agent Cert
    # at the wire layer. A bare TLS cert (no AGTP flags) omits the
    # extension and lets the daemon fall back to key-derived ID.
    extension_flags_present = any(
        v not in (None, [], "")
        for v in (
            args.principal_id,
            args.authority_scope,
            args.governance_zone,
            args.trust_tier,
            args.archetype,
            args.activation_certificate_id,
            args.subject_agent_id,
        )
    )
    effective_subject_agent_id = args.subject_agent_id or (
        agent_id if extension_flags_present else None
    )

    try:
        cert = _build_cert(
            subject_key=agent_pub,
            issuer_name=issuer_name,
            issuer_key=issuer_key,
            common_name=common_name,
            valid_days=args.valid_days,
            organization=args.organization,
            organizational_unit=args.organizational_unit,
            email=args.email,
            subject_agent_id=effective_subject_agent_id,
            principal_id=args.principal_id,
            authority_scopes=args.authority_scope or None,
            governance_zone=args.governance_zone,
            trust_tier=args.trust_tier,
            archetype=args.archetype,
            activation_certificate_id=args.activation_certificate_id,
        )
    except CertExtensionError as exc:
        print(f"invalid Agent Cert extension: {exc}", file=sys.stderr)
        return 2

    # Write key (PKCS8 PEM).
    key_pem = agent_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.write_bytes(key_pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass

    # Write cert (PEM).
    crt_pem = cert.public_bytes(encoding=serialization.Encoding.PEM)
    crt_path.write_bytes(crt_pem)
    try:
        os.chmod(crt_path, 0o644)
    except OSError:
        pass

    print(f"private key:  {key_path}")
    print(f"certificate:  {crt_path}  ({signed_by})")
    print(f"common name:  {common_name}")
    print(f"agent_id:     {agent_id}")
    if extension_flags_present:
        print()
        print("AGTP Agent Cert extensions written:")
        if effective_subject_agent_id:
            print(f"  subject-agent-id:          {effective_subject_agent_id}")
        if args.principal_id:
            print(f"  principal-id:              {args.principal_id}")
        if args.authority_scope:
            print(f"  authority-scope-commitment: {sorted(set(args.authority_scope))}")
        if args.governance_zone:
            print(f"  governance-zone:           {args.governance_zone}")
        if args.trust_tier is not None:
            print(f"  trust-tier:                {args.trust_tier}")
        if args.archetype:
            print(f"  archetype:                 {args.archetype}")
        if args.activation_certificate_id:
            print(f"  activation-certificate-id: {args.activation_certificate_id}")
    print()
    print("This Agent-ID is derived from the cert's Ed25519 public key.")
    print("When the daemon verifies a connection presenting this cert, it")
    print("will compute the same agent_id and treat the connection as that")
    print("agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

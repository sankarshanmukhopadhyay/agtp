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

Output:

  * ``<output>.key`` — private key (PEM, 0o600)
  * ``<output>.crt`` — certificate (PEM, 0o644)

Prints the derived ``agent_id`` so the operator can copy it into the
agent's identity document (or use it as the Agent-ID header value
in a sanity check; the daemon will derive the same value on
verification).

Usage::

    python -m tools.generate_agent_cert agents/lauren
    python -m tools.generate_agent_cert agents/lauren \\
        --ca-cert ca.crt --ca-key ca.key
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
) -> x509.Certificate:
    subject_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
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

    cert = _build_cert(
        subject_key=agent_pub,
        issuer_name=issuer_name,
        issuer_key=issuer_key,
        common_name=common_name,
        valid_days=args.valid_days,
    )

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

    agent_id = derive_agent_id_from_public_key(agent_pub)
    print(f"private key:  {key_path}")
    print(f"certificate:  {crt_path}  ({signed_by})")
    print(f"common name:  {common_name}")
    print(f"agent_id:     {agent_id}")
    print()
    print("This Agent-ID is derived from the cert's Ed25519 public key.")
    print("When the daemon verifies a connection presenting this cert, it")
    print("will compute the same agent_id and treat the connection as that")
    print("agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

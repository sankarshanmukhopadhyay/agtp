"""
Generate an Ed25519 key pair for ``agtpd`` signing.

Writes the private key as PEM with ``0o600`` mode and the public key
as PEM with ``0o644`` mode. Print the key-id (``ed25519-<sha256-prefix>``)
on stdout so operators can copy it into their config or distribute
it to verifiers.

Usage::

    python -m tools.generate_signing_key /etc/agtpd/signing
    # → writes /etc/agtpd/signing.key and /etc/agtpd/signing.pub

    python -m tools.generate_signing_key --output ./mykey
    # → writes ./mykey.key and ./mykey.pub
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _derive_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "ed25519-" + hashlib.sha256(raw).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_signing_key",
        description="Generate an Ed25519 signing key pair for agtpd.",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default="signing",
        help=(
            "Base path for the key files. The private key is written "
            "to <output>.key and the public key to <output>.pub. "
            "Defaults to ./signing."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files. Without this, the tool refuses "
             "to write over existing keys.",
    )
    args = parser.parse_args(argv)

    base = Path(args.output)
    priv_path = base.with_suffix(".key")
    pub_path = base.with_suffix(".pub")

    if not args.force:
        for p in (priv_path, pub_path):
            if p.exists():
                print(
                    f"refusing to overwrite existing file: {p} (use --force)",
                    file=sys.stderr,
                )
                return 2

    if base.parent and not base.parent.exists():
        base.parent.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)

    # Set restrictive permissions on the private key. Best-effort on
    # Windows where chmod is partial.
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass
    try:
        os.chmod(pub_path, 0o644)
    except OSError:
        pass

    key_id = _derive_key_id(public_key)
    print(f"private key: {priv_path}")
    print(f"public key:  {pub_path}")
    print(f"key id:      {key_id}")
    print()
    print("Add the following to your agtp-server.toml [signing] block:")
    print()
    print("    [signing]")
    print("    enabled  = true")
    print(f'    key_path = "{priv_path}"')
    print(f'    key_id   = "{key_id}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())

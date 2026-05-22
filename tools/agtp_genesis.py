"""
agtp-genesis — manage AGTP Agent Genesis documents.

Subcommands::

    agtp-genesis create --name NAME --owner OWNER --principal PRINCIPAL \\
        [--issuer self|registrar.example.com] \\
        [--issuer-key PATH] [--issuer-pub PATH] \\
        [--archetype ARCHETYPE] [--zone ZONE] [--tier {1,2,3}] \\
        [--verification-path {dns-anchored,log-anchored,hybrid,self-signed}] \\
        [--out PATH] [--key-out PATH] [--pub-out PATH]

    agtp-genesis verify   PATH
    agtp-genesis hash     PATH
    agtp-genesis show     PATH

The ``create`` subcommand has two modes:

  * **self-signed (default).** Generates a fresh Ed25519 keypair for
    the agent and signs the Genesis with the same key. The
    ``issuer`` field is set to ``"self"`` and ``issuer_public_key``
    equals ``agent_public_key``. Equivalent to a self-signed SSL
    cert — fine for development; Trust Tier 1 requires registrar
    issuance via ``tools.registrar``.

  * **registrar-signed.** Pass ``--issuer registrar.example.com
    --issuer-key registrar.key --issuer-pub registrar.pub`` to sign
    the Genesis with a separate issuer key. The agent's own
    public key is still generated fresh; only the signature comes
    from the issuer.

This is the local-development companion to the reference registrar
(``tools/registrar/``). The registrar itself uses the same
:mod:`core.genesis` module to issue Geneses over HTTP.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from core.genesis import (
    AgentGenesis,
    GENESIS_VERSION,
    GenesisFormatError,
    GenesisSignatureError,
    VALID_ARCHETYPES,
    VALID_TRUST_TIERS,
    VALID_VERIFICATION_PATHS,
    load_genesis_json,
    public_key_pem,
    utc_now_iso,
)


def _generate_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _write_private_key(key: Ed25519PrivateKey, path: Path) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_public_key(key: Ed25519PrivateKey, path: Path) -> None:
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.write_bytes(pem)


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    pem = path.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(
            f"key at {path} is not Ed25519 (got {type(key).__name__})"
        )
    return key


def _load_public_key_pem(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _cmd_create(args: argparse.Namespace) -> int:
    out_path = Path(args.out) if args.out else Path(f"{args.name}.genesis.json")
    key_out = Path(args.key_out) if args.key_out else Path(f"{args.name}.key")
    pub_out = Path(args.pub_out) if args.pub_out else Path(f"{args.name}.pub")

    if not args.force:
        for p in (out_path, key_out, pub_out):
            if p.exists():
                print(
                    f"refusing to overwrite existing file: {p} (use --force)",
                    file=sys.stderr,
                )
                return 2

    # Agent keypair.
    agent_key = _generate_keypair()
    agent_pub_pem = public_key_pem(agent_key.public_key())

    # Issuer mode.
    if args.issuer in (None, "", "self"):
        issuer_name = "self"
        issuer_key = agent_key
        issuer_pub_pem = agent_pub_pem
    else:
        if not args.issuer_key:
            print(
                "--issuer-key is required when --issuer is not 'self'",
                file=sys.stderr,
            )
            return 2
        issuer_name = args.issuer
        issuer_key = _load_private_key(Path(args.issuer_key))
        if args.issuer_pub:
            issuer_pub_pem = _load_public_key_pem(Path(args.issuer_pub))
        else:
            issuer_pub_pem = public_key_pem(issuer_key.public_key())

    genesis = AgentGenesis(
        name=args.name,
        owner_id=args.owner,
        principal_id=args.principal or args.owner,
        agent_public_key=agent_pub_pem,
        archetype=args.archetype,
        governance_zone=args.zone,
        trust_tier=args.tier,
        verification_path=args.verification_path,
        issued_at=utc_now_iso(),
        issuer=issuer_name,
        issuer_public_key=issuer_pub_pem,
    )

    try:
        genesis.sign(issuer_key)
    except GenesisFormatError as exc:
        print(f"could not sign Genesis: {exc}", file=sys.stderr)
        return 2

    agent_id = genesis.canonical_agent_id()
    out_path.write_text(genesis.to_pretty_json() + "\n", encoding="utf-8")
    _write_private_key(agent_key, key_out)
    _write_public_key(agent_key, pub_out)

    print(f"genesis:     {out_path}")
    print(f"private key: {key_out}")
    print(f"public key:  {pub_out}")
    print(f"name:        {args.name}")
    print(f"owner:       {args.owner}")
    print(f"issuer:      {issuer_name}")
    print(f"trust_tier:  {args.tier}")
    print(f"verification:{args.verification_path}")
    print(f"agent_id:    {agent_id}")
    print()
    print("This Agent-ID = sha256(canonical Genesis JSON sans signature).")
    print("It rides on every AGTP request as the Agent-ID header.")
    if issuer_name == "self":
        print()
        print("Note: self-signed Genesis. Trust Tier 1 requires registrar")
        print("issuance — run a registrar via `python -m tools.registrar`.")
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    try:
        genesis = load_genesis_json(path.read_text(encoding="utf-8"))
    except GenesisFormatError as exc:
        print(f"malformed Genesis: {exc}", file=sys.stderr)
        return 1
    try:
        genesis.verify()
    except GenesisSignatureError as exc:
        print(f"signature did not verify: {exc}", file=sys.stderr)
        return 1
    print(f"OK  signature verifies; agent_id = {genesis.canonical_agent_id()}")
    return 0


# ---------------------------------------------------------------------------
# hash
# ---------------------------------------------------------------------------


def _cmd_hash(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    try:
        genesis = load_genesis_json(path.read_text(encoding="utf-8"))
    except GenesisFormatError as exc:
        print(f"malformed Genesis: {exc}", file=sys.stderr)
        return 1
    print(genesis.canonical_agent_id())
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    try:
        genesis = load_genesis_json(path.read_text(encoding="utf-8"))
    except GenesisFormatError as exc:
        print(f"malformed Genesis: {exc}", file=sys.stderr)
        return 1
    print(f"agent_id:          {genesis.canonical_agent_id()}")
    print(f"name:              {genesis.name}")
    print(f"owner_id:          {genesis.owner_id}")
    print(f"principal_id:      {genesis.principal_id}")
    print(f"archetype:         {genesis.archetype or '-'}")
    print(f"governance_zone:   {genesis.governance_zone or '-'}")
    print(f"trust_tier:        {genesis.trust_tier}")
    print(f"verification_path: {genesis.verification_path}")
    print(f"issued_at:         {genesis.issued_at}")
    print(f"issuer:            {genesis.issuer}")
    print(f"agtp_genesis_ver:  {genesis.agtp_genesis_version}")
    print(f"signed:            {'yes' if genesis.signature else 'no'}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agtp-genesis",
        description="Create, verify, hash, and inspect AGTP Agent Genesis documents.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ----- create -----
    p_create = sub.add_parser(
        "create", help="Issue a new Agent Genesis (signed JSON document)."
    )
    p_create.add_argument("--name", required=True, help="Agent name (label).")
    p_create.add_argument(
        "--owner", required=True,
        help="Owner ID — the legal entity that owns this agent (e.g., 'nomotic.inc').",
    )
    p_create.add_argument(
        "--principal",
        help=(
            "Principal ID — the human/service the agent acts on behalf of "
            "(defaults to --owner)."
        ),
    )
    p_create.add_argument(
        "--issuer", default="self",
        help="Issuer identifier. 'self' (default) self-signs; otherwise pass a registrar id.",
    )
    p_create.add_argument(
        "--issuer-key",
        help="Path to issuer's Ed25519 private key (required when --issuer != 'self').",
    )
    p_create.add_argument(
        "--issuer-pub",
        help=(
            "Path to issuer's Ed25519 public key (PEM). Optional; derived "
            "from --issuer-key when omitted."
        ),
    )
    p_create.add_argument(
        "--archetype", choices=sorted(VALID_ARCHETYPES),
        help="Behavioral archetype.",
    )
    p_create.add_argument(
        "--zone", help="Governance zone identifier (e.g., 'zone:finance').",
    )
    p_create.add_argument(
        "--tier", type=int, choices=list(VALID_TRUST_TIERS), default=2,
        help="Trust tier (1/2/3). Default: 2 (Org-Asserted).",
    )
    p_create.add_argument(
        "--verification-path", default="self-signed",
        choices=sorted(VALID_VERIFICATION_PATHS),
        help="Verification path. Default: self-signed.",
    )
    p_create.add_argument(
        "--out", help="Output path for the Genesis JSON. Default: <name>.genesis.json.",
    )
    p_create.add_argument(
        "--key-out", help="Output path for the agent's Ed25519 private key.",
    )
    p_create.add_argument(
        "--pub-out", help="Output path for the agent's Ed25519 public key.",
    )
    p_create.add_argument(
        "--force", action="store_true",
        help="Overwrite existing output files.",
    )
    p_create.set_defaults(func=_cmd_create)

    # ----- verify -----
    p_verify = sub.add_parser(
        "verify", help="Verify a Genesis document's signature.",
    )
    p_verify.add_argument("path", help="Path to <name>.genesis.json.")
    p_verify.set_defaults(func=_cmd_verify)

    # ----- hash -----
    p_hash = sub.add_parser(
        "hash", help="Print the Canonical Agent-ID derived from a Genesis.",
    )
    p_hash.add_argument("path", help="Path to <name>.genesis.json.")
    p_hash.set_defaults(func=_cmd_hash)

    # ----- show -----
    p_show = sub.add_parser(
        "show", help="Print a Genesis document's fields.",
    )
    p_show.add_argument("path", help="Path to <name>.genesis.json.")
    p_show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

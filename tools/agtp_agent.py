"""
agtp_agent — unified CLI for the agent-lifecycle workflow.

The three things every AGTP agent needs to come online are:

  1. **Agent Genesis** — the immutable identity document, signed by
     either the agent itself (self-signed dev path) or a registrar
     (org-signed / Tier 2 production path). Hashes to the canonical
     Agent-ID.
  2. **AgentDocument** — the mutable capability declaration: name,
     description, role, methods/scopes the agent answers, trust
     posture. References the Genesis by Agent-ID.
  3. **Agent Certificate** — optional X.509 v3 cert for mTLS, with
     ``subject-agent-id`` bound to the Genesis hash. Required when
     the daemon enforces mTLS; optional for plaintext-trust deployments.

Today three separate CLIs cover (1) and (3) with no CLI for (2);
operators hand-write ``{name}.agent.json`` from a template, then run
the three CLIs by hand and drop the files into the daemon's
``agents/`` directory. This module replaces that workflow with one
CLI that covers all three plus the placement step.

Subcommand surface
~~~~~~~~~~~~~~~~~~

::

  agtp-agent new       --name X --owner Y [...]
                       Mints the agent's Ed25519 keypair and a
                       signed Genesis. Defaults to self-signed;
                       --registrar https://... POSTs to a registrar
                       over HTTPS for an org-signed Genesis.

  agtp-agent cert      --genesis G.json --agent-key K.key [...]
                       Generates the X.509 v3 cert from an existing
                       Genesis. Thin wrapper over
                       ``tools.generate_agent_cert`` for callers that
                       want cert generation as a separate step.

  agtp-agent install   --genesis G.json --agents-dir DIR [...]
                       Generates the AgentDocument JSON (the missing
                       piece) AND drops the Genesis + AgentDocument
                       (and cert if supplied) into the daemon's
                       agents directory. Most operators call this
                       directly when they have the Genesis already.

  agtp-agent register  --name X --owner Y --agents-dir DIR [...]
                       End-to-end: runs ``new`` + ``install``
                       (and ``cert`` when --with-cert is supplied)
                       in one shot. The command operators reach for
                       when bringing a fresh agent online.

The lower-level CLIs (``tools.agtp_genesis``,
``tools.generate_agent_cert``, ``tools.registrar``) stay available as
escape hatches for operators who want to script one piece at a time
or hold the keys outside the daemon host.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.genesis import (
    AgentGenesis, VALID_ARCHETYPES, VALID_TRUST_TIERS,
    VALID_VERIFICATION_PATHS, parse_genesis, public_key_pem,
)
from core.identity import (
    AgentDocument, DEFAULT_ROLE, RequiresDeclaration, VALID_ROLES,
)


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_outdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"{path} already exists (use --force to overwrite)"
        )
    path.write_text(content, encoding="utf-8")


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _build_policies(args: argparse.Namespace) -> Dict[str, Any]:
    """Translate ``--oauth-*`` CLI flags into the AgentDocument's
    ``policies`` dict.

    Returns ``{}`` when no OAuth flags are set so the resulting
    document elides the ``policies`` key entirely (Pattern 1
    documents emit no OAuth machinery on the wire). When
    ``--oauth-validator`` is set, builds the canonical
    ``policies.oauth`` block consumed by
    :func:`server.methods._resolve_oauth_config`.
    """
    validator = getattr(args, "oauth_validator", None)
    if not validator:
        return {}
    oauth_block: Dict[str, Any] = {
        "enabled": True,
        "validator": str(validator).strip(),
    }
    required = _split_csv(getattr(args, "oauth_required_on", ""))
    if required:
        oauth_block["required_on_methods"] = [
            m.upper() for m in required
        ]
    claim = (getattr(args, "oauth_principal_id_claim", "") or "").strip()
    if claim:
        oauth_block["principal_id_claim"] = claim
    raw_cfg = getattr(args, "oauth_config", None)
    if raw_cfg:
        try:
            parsed = json.loads(raw_cfg)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"--oauth-config is not valid JSON: {exc}"
            )
        if not isinstance(parsed, dict):
            raise SystemExit(
                f"--oauth-config must be a JSON object (got {type(parsed).__name__})"
            )
        oauth_block["validator_config"] = parsed
    return {"oauth": oauth_block}


def _install_trust_anchor(
    src_path: str, agents_dir: Path, *, force: bool,
) -> Optional[Path]:
    """Copy a Pattern-3 trust-anchors file into the agents dir.

    Returns the destination path (or ``None`` when the source is
    not configured). The daemon's startup scan looks for
    ``trust-anchors.json`` in the agents dir; that name is
    intentionally hardcoded so the operator never has to wire
    config + file paths in two places.
    """
    if not src_path:
        return None
    src = Path(src_path).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"trust-anchor file not found: {src}")
    # Parse to fail-fast on malformed JSON. The daemon's loader
    # tolerates this, but the operator running `register` deserves
    # an immediate error rather than silent ignore at boot.
    try:
        json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"trust-anchor file is not valid JSON: {src} ({exc})"
        )
    dst = agents_dir / "trust-anchors.json"
    _write_text(dst, src.read_text(encoding="utf-8"), force=force)
    return dst


# ---------------------------------------------------------------------------
# `new` — keypair + Genesis.
# ---------------------------------------------------------------------------


def _cmd_new(args: argparse.Namespace) -> int:
    """Mint the agent's keypair and a signed Genesis.

    Two paths: self-signed (default — dev / Tier 3 deployments) or
    registrar-signed (--registrar URL — production / Tier 2). The
    registrar path POSTs the generated public key to the registrar's
    /issue endpoint and writes the returned signed Genesis to disk.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    out_dir = Path(args.out_dir).expanduser().resolve()
    _ensure_outdir(out_dir)
    name = args.name

    # Generate keypair.
    key = Ed25519PrivateKey.generate()
    pub_pem = public_key_pem(key.public_key())
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    key_path = out_dir / f"{name}.key"
    pub_path = out_dir / f"{name}.pub"
    _write_text(key_path, key_pem, force=args.force)
    _write_text(pub_path, pub_pem, force=args.force)
    # POSIX-permissive: tighten the private key. No-op on Windows.
    try:
        key_path.chmod(0o600)
    except OSError:
        pass

    # Mint Genesis.
    if args.registrar:
        genesis = _issue_via_registrar(args, pub_pem=pub_pem)
    else:
        genesis = _issue_self_signed(args, key=key, pub_pem=pub_pem)

    genesis_path = out_dir / f"{name}.genesis.json"
    _write_text(
        genesis_path, genesis.to_pretty_json() + "\n", force=args.force,
    )

    print(f"agent keypair:  {key_path} (PEM, 0600)")
    print(f"agent pubkey:   {pub_path}")
    print(f"genesis:        {genesis_path}")
    print(f"agent_id:       {genesis.canonical_agent_id()}")
    print(f"issuer:         {genesis.issuer}")
    print(f"trust_tier:     {genesis.trust_tier}")
    print(f"verification:   {genesis.verification_path}")
    return 0


def _issue_self_signed(
    args: argparse.Namespace, *, key, pub_pem: str,
) -> AgentGenesis:
    genesis = AgentGenesis(
        name=args.name,
        owner_id=args.owner,
        principal_id=args.principal or args.owner,
        agent_public_key=pub_pem,
        issued_at=_utc_now_iso(),
        issuer="self",
        issuer_public_key=pub_pem,
        archetype=args.archetype,
        governance_zone=args.zone,
        trust_tier=args.tier,
        verification_path=args.verification_path,
    )
    genesis.sign(key)
    return genesis


def _issue_via_registrar(
    args: argparse.Namespace, *, pub_pem: str,
) -> AgentGenesis:
    """POST the agent's public key + metadata to a registrar's
    /issue endpoint over HTTPS and parse the returned Genesis."""
    import urllib.request
    import urllib.error

    payload: Dict[str, Any] = {
        "name": args.name,
        "owner_id": args.owner,
        "principal_id": args.principal or args.owner,
        "agent_public_key": pub_pem,
        "trust_tier": args.tier,
        "verification_path": args.verification_path,
    }
    if args.archetype:
        payload["archetype"] = args.archetype
    if args.zone:
        payload["governance_zone"] = args.zone

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        args.registrar,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"registrar refused issuance: HTTP {exc.code}\n{body}"
        )
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"could not reach registrar at {args.registrar}: {exc.reason}"
        )

    try:
        return parse_genesis(data)
    except Exception as exc:
        raise SystemExit(
            f"registrar response is not a valid Genesis: {exc}"
        )


# ---------------------------------------------------------------------------
# `cert` — generate the X.509 cert.
# ---------------------------------------------------------------------------


def _cmd_cert(args: argparse.Namespace) -> int:
    """Thin wrapper over ``tools.generate_agent_cert``. Operators who
    want cert generation as a discrete step (e.g., to use a HSM-held
    CA key) call this; the ``register`` end-to-end command invokes
    the same code path internally.
    """
    # Defer to the existing tool's main(), assembling its argv from
    # ours so we don't duplicate cert-generation logic.
    from tools import generate_agent_cert as _gen

    argv: List[str] = [str(Path(args.out).expanduser().resolve())]
    argv += ["--genesis", str(Path(args.genesis).expanduser().resolve())]
    if args.principal_id:
        argv += ["--principal-id", args.principal_id]
    if args.authority_scope:
        argv += ["--authority-scope", args.authority_scope]
    if args.ca_cert:
        argv += ["--ca-cert", str(Path(args.ca_cert).expanduser().resolve())]
    if args.ca_key:
        argv += ["--ca-key", str(Path(args.ca_key).expanduser().resolve())]
    if args.valid_days is not None:
        argv += ["--valid-days", str(args.valid_days)]
    if args.force:
        argv += ["--force"]

    rc = _gen.main(argv)
    return int(rc or 0)


# ---------------------------------------------------------------------------
# `install` — generate AgentDocument + drop into agents/ dir.
# ---------------------------------------------------------------------------


def _cmd_install(args: argparse.Namespace) -> int:
    """Generate the AgentDocument JSON from the supplied Genesis + flags,
    then place the Genesis + AgentDocument (and cert if --cert is
    supplied) in the daemon's agents directory.

    This is the closest thing to "register an agent with a daemon"
    today — the daemon's startup scan picks up any
    ``{name}.agent.json`` (+ optional ``{name}.genesis.json`` and
    ``{name}.cert`` / ``.key``) it finds in the directory.
    """
    genesis_path = Path(args.genesis).expanduser().resolve()
    if not genesis_path.exists():
        raise SystemExit(f"genesis file not found: {genesis_path}")
    genesis_json = json.loads(genesis_path.read_text(encoding="utf-8"))
    genesis = parse_genesis(genesis_json)
    # Verify before installing — operator should be told if the
    # Genesis on disk doesn't survive a signature check.
    try:
        genesis.verify()
    except Exception as exc:
        raise SystemExit(
            f"genesis signature failed to verify: {exc}\n"
            f"Refusing to install an unverifiable identity document."
        )

    agents_dir = Path(args.agents_dir).expanduser().resolve()
    _ensure_outdir(agents_dir)
    name = args.name or genesis.name

    # Build the AgentDocument from Genesis-derived fields + operator
    # flags. The Genesis supplies cryptographic identity; the
    # operator supplies mutable capability.
    requires = RequiresDeclaration(
        methods=_split_csv(args.methods),
        scopes=_split_csv(args.scopes),
        wildcards=args.wildcards,
    )
    doc = AgentDocument(
        agtp_version="1.0",
        agent_id=genesis.canonical_agent_id(),
        name=name,
        principal=args.principal_display or genesis.principal_id,
        principal_id=genesis.principal_id,
        description=args.description or f"AGTP agent {name!r}.",
        status="active",
        skills=_split_csv(args.skills),
        requires=requires,
        scopes_accepted=_split_csv(args.scopes_accepted),
        issued_at=_utc_now_iso(),
        issuer=genesis.issuer,
        trust_tier=genesis.trust_tier,
        verification_path=genesis.verification_path,
        owner_id=genesis.owner_id,
        role=args.role,
        policies=_build_policies(args),
    )

    # Write all three artifacts.
    doc_path = agents_dir / f"{name}.agent.json"
    genesis_target = agents_dir / f"{name}.genesis.json"
    _write_text(
        doc_path, json.dumps(doc.to_dict(), indent=2) + "\n",
        force=args.force,
    )
    _write_text(
        genesis_target, genesis.to_pretty_json() + "\n",
        force=args.force,
    )
    print(f"agent document: {doc_path}")
    print(f"genesis:        {genesis_target}")
    print(f"agent_id:       {doc.agent_id}")

    anchor_target = _install_trust_anchor(
        getattr(args, "trust_anchor", "") or "",
        agents_dir,
        force=args.force,
    )
    if anchor_target is not None:
        print(f"trust anchors:  {anchor_target}")

    if args.cert:
        cert_path = Path(args.cert).expanduser().resolve()
        if not cert_path.exists():
            raise SystemExit(f"cert file not found: {cert_path}")
        cert_target = agents_dir / f"{name}.cert"
        _write_text(
            cert_target, cert_path.read_text(encoding="utf-8"),
            force=args.force,
        )
        print(f"cert:           {cert_target}")
    if args.cert_key:
        key_path = Path(args.cert_key).expanduser().resolve()
        if not key_path.exists():
            raise SystemExit(f"cert key file not found: {key_path}")
        key_target = agents_dir / f"{name}.key"
        _write_text(
            key_target, key_path.read_text(encoding="utf-8"),
            force=args.force,
        )
        try:
            key_target.chmod(0o600)
        except OSError:
            pass
        print(f"cert key:       {key_target} (0600)")

    print()
    print(
        f"Installed {name} -> {agents_dir}. Restart agtpd (or call "
        f"its reload signal) to register the agent."
    )
    return 0


# ---------------------------------------------------------------------------
# `register` — end-to-end wrapper.
# ---------------------------------------------------------------------------


def _cmd_register(args: argparse.Namespace) -> int:
    """End-to-end agent registration.

    Runs ``new`` (mint keypair + Genesis), optionally ``cert``
    (generate Agent Cert), and ``install`` (generate AgentDocument
    + drop everything in the daemon's agents/ dir) in one shot.
    The output_dir flow goes to a temporary work area; the
    install step copies the final artifacts to ``--agents-dir``.
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix=f"agtp-agent-{args.name}-"))
    try:
        # Step 1: new — keypair + Genesis in work/
        new_args = argparse.Namespace(
            name=args.name,
            owner=args.owner,
            principal=args.principal,
            archetype=args.archetype,
            zone=args.zone,
            tier=args.tier,
            verification_path=args.verification_path,
            registrar=args.registrar,
            out_dir=str(work),
            force=True,  # work dir is fresh
        )
        print("=== step 1/3 — mint keypair + Genesis ===")
        _cmd_new(new_args)

        # Step 2 (optional): cert
        cert_path: Optional[Path] = None
        cert_key_path: Optional[Path] = None
        if args.with_cert:
            print()
            print("=== step 2/3 — generate Agent Cert ===")
            cert_path = work / f"{args.name}.cert"
            cert_key_path = work / f"{args.name}.key"
            cert_args = argparse.Namespace(
                out=str(cert_path),
                genesis=str(work / f"{args.name}.genesis.json"),
                principal_id=args.principal or args.owner,
                authority_scope=args.scopes,
                ca_cert=args.ca_cert,
                ca_key=args.ca_key,
                valid_days=args.cert_valid_days,
                force=True,
            )
            _cmd_cert(cert_args)

        # Step 3: install — AgentDocument + place in agents dir
        print()
        print("=== step 3/3 — install in agents directory ===")
        install_args = argparse.Namespace(
            name=args.name,
            genesis=str(work / f"{args.name}.genesis.json"),
            agents_dir=args.agents_dir,
            methods=args.methods,
            scopes=args.scopes,
            scopes_accepted=args.scopes_accepted,
            skills=args.skills,
            description=args.description,
            role=args.role,
            principal_display=args.principal_display,
            wildcards=args.wildcards,
            cert=str(cert_path) if cert_path else "",
            cert_key=str(cert_key_path) if cert_key_path else "",
            # OAuth composition flags pass through unchanged.
            oauth_validator=getattr(args, "oauth_validator", None),
            oauth_required_on=getattr(args, "oauth_required_on", ""),
            oauth_principal_id_claim=getattr(
                args, "oauth_principal_id_claim", "",
            ),
            oauth_config=getattr(args, "oauth_config", None),
            # Trust anchor (Pattern 3) passes through unchanged.
            trust_anchor=getattr(args, "trust_anchor", "") or "",
            force=args.force,
        )
        _cmd_install(install_args)

        return 0
    finally:
        # Clean up work directory but keep the agent's private key
        # IF the operator didn't already copy it to the agents/ dir
        # (cert flow). Self-signed Geneses sign with the agent key,
        # which we copy to the agents/ dir during install only if
        # --with-cert. For the no-cert path we move the keypair to
        # the agents/ dir defensively so operators don't lose it.
        if not args.with_cert:
            agents_dir = Path(args.agents_dir).expanduser().resolve()
            for fname in (f"{args.name}.key", f"{args.name}.pub"):
                src = work / fname
                dst = agents_dir / fname
                if src.exists() and not dst.exists():
                    dst.write_text(src.read_text(encoding="utf-8"))
                    if fname.endswith(".key"):
                        try:
                            dst.chmod(0o600)
                        except OSError:
                            pass
                    print(f"keypair:        {dst}")
        import shutil
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# argparse wiring.
# ---------------------------------------------------------------------------


def _add_genesis_flags(p: argparse.ArgumentParser) -> None:
    """Flags shared by `new` and `register` that affect Genesis
    issuance."""
    p.add_argument("--name", required=True, help="Agent name (label).")
    p.add_argument(
        "--owner", required=True,
        help="Owner-ID: the legal entity owning this agent.",
    )
    p.add_argument(
        "--principal",
        help="Principal-ID: the human/service the agent acts for "
             "(defaults to --owner).",
    )
    p.add_argument(
        "--archetype", choices=sorted(VALID_ARCHETYPES),
        help="Behavioral archetype.",
    )
    p.add_argument(
        "--zone",
        help="Governance zone identifier (e.g. 'zone:finance').",
    )
    p.add_argument(
        "--tier", type=int, default=2,
        choices=list(VALID_TRUST_TIERS),
        help="Trust tier. Default 2 (Org-Asserted).",
    )
    p.add_argument(
        "--verification-path", default="self-signed",
        choices=sorted(VALID_VERIFICATION_PATHS),
        help="Verification path. Default 'self-signed' (dev / local).",
    )
    p.add_argument(
        "--registrar",
        help="Registrar /issue URL (HTTPS). When supplied, the public "
             "key is POSTed for an org-signed Genesis; default is to "
             "self-sign locally.",
    )


def _add_document_flags(p: argparse.ArgumentParser) -> None:
    """Flags shared by `install` and `register` that populate the
    AgentDocument (the mutable capability layer)."""
    p.add_argument(
        "--methods", default="",
        help="Comma-separated AGTP method names the agent handles "
             "inbound (e.g. 'DISCOVER,DESCRIBE,QUERY').",
    )
    p.add_argument(
        "--scopes", default="",
        help="Comma-separated scope tokens the agent declares "
             "(e.g. 'bookings:write,calendar:read').",
    )
    p.add_argument(
        "--scopes-accepted", default="",
        help="Comma-separated scopes the agent accepts on inbound "
             "requests (Authority-Scope header tokens).",
    )
    p.add_argument(
        "--skills", default="",
        help="Comma-separated human-readable skill labels "
             "(e.g. 'coffee,scheduling').",
    )
    p.add_argument(
        "--wildcards", action="store_true",
        help="Agent accepts any inbound method (orchestrator pattern). "
             "Sets requires.wildcards = true.",
    )
    p.add_argument(
        "--description", default="",
        help="Free-text agent description (one line).",
    )
    p.add_argument(
        "--role", default=DEFAULT_ROLE, choices=sorted(VALID_ROLES),
        help="Identity role. Default 'agent'; 'merchant' triggers "
             "mod_merchant's PURCHASE gate.",
    )
    p.add_argument(
        "--principal-display",
        help="Human-readable principal label (defaults to "
             "principal_id from the Genesis).",
    )
    # OAuth composition (Pattern 2). When supplied, the generated
    # AgentDocument carries a policies.oauth block that overrides
    # the server-wide [policies.oauth] config for this agent.
    p.add_argument(
        "--oauth-validator",
        help="OAuth validator name (e.g. 'noop', 'jwt'). Sets "
             "policies.oauth.enabled=true on the AgentDocument and "
             "pins the validator. Without this flag, the agent "
             "inherits whatever the server's [policies.oauth] block "
             "says (default: no OAuth — Pattern 1).",
    )
    p.add_argument(
        "--oauth-required-on", default="",
        help="Comma-separated AGTP methods that REQUIRE a valid "
             "OAuth bearer token (e.g. 'PURCHASE,WRITE'). Methods "
             "outside this list still validate any token presented, "
             "but missing tokens are tolerated. Ignored unless "
             "--oauth-validator is also set.",
    )
    p.add_argument(
        "--oauth-principal-id-claim", default="",
        help="Token claim lifted onto request.acting_principal_id "
             "after successful validation. Defaults to 'sub'. "
             "Ignored unless --oauth-validator is also set.",
    )
    p.add_argument(
        "--oauth-config",
        help="JSON object passed verbatim into the validator's "
             "config (e.g. '{\"public_key\": \"-----BEGIN ...\"}' "
             "for JWTValidator). Ignored unless --oauth-validator "
             "is also set.",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agtp-agent",
        description=(
            "Unified CLI for the agent-lifecycle workflow. Combines "
            "keypair generation, Genesis issuance, AgentDocument "
            "creation, optional cert generation, and placement in "
            "the daemon's agents directory."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # new
    p_new = sub.add_parser(
        "new",
        help="Mint the agent's keypair and a signed Genesis "
             "(self-signed or via a registrar).",
    )
    _add_genesis_flags(p_new)
    p_new.add_argument(
        "--out-dir", default=".",
        help="Directory for the keypair + Genesis files. "
             "Default: current directory.",
    )
    p_new.add_argument(
        "--force", action="store_true",
        help="Overwrite existing output files.",
    )
    p_new.set_defaults(func=_cmd_new)

    # cert
    p_cert = sub.add_parser(
        "cert", help="Generate an Agent Cert from an existing Genesis.",
    )
    p_cert.add_argument(
        "--genesis", required=True,
        help="Path to the agent's Genesis JSON.",
    )
    p_cert.add_argument(
        "--out", required=True,
        help="Output basename (the tool writes .cert and .key).",
    )
    p_cert.add_argument("--principal-id")
    p_cert.add_argument(
        "--authority-scope",
        help="Comma-separated authority-scope token list embedded "
             "in the cert's authority-scope-commitment extension.",
    )
    p_cert.add_argument("--ca-cert")
    p_cert.add_argument("--ca-key")
    p_cert.add_argument(
        "--valid-days", type=int,
        help="Cert validity in days. Default per AGTP-CERT: 90.",
    )
    p_cert.add_argument(
        "--force", action="store_true",
        help="Overwrite existing output files.",
    )
    p_cert.set_defaults(func=_cmd_cert)

    # install
    p_install = sub.add_parser(
        "install",
        help="Generate AgentDocument from a Genesis + flags and drop "
             "everything in the daemon's agents directory.",
    )
    p_install.add_argument(
        "--genesis", required=True,
        help="Path to the agent's Genesis JSON.",
    )
    p_install.add_argument(
        "--agents-dir", required=True,
        help="Path to the daemon's agents directory.",
    )
    p_install.add_argument(
        "--name",
        help="Override the AgentDocument name (defaults to "
             "Genesis.name).",
    )
    _add_document_flags(p_install)
    p_install.add_argument(
        "--cert", help="Path to a generated cert to install alongside.",
    )
    p_install.add_argument(
        "--cert-key", help="Path to the cert's private key.",
    )
    p_install.add_argument(
        "--trust-anchor",
        help="Path to a JSON trust-anchors file (Pattern 3 — "
             "Genesis-issuer federation). Copied into the agents "
             "directory as trust-anchors.json for the daemon to "
             "load on boot. Existing file is overwritten only when "
             "--force is set.",
    )
    p_install.add_argument(
        "--force", action="store_true",
        help="Overwrite existing AgentDocument / Genesis / cert "
             "files in the agents directory.",
    )
    p_install.set_defaults(func=_cmd_install)

    # register — the end-to-end one
    p_reg = sub.add_parser(
        "register",
        help="End-to-end: mint keypair + Genesis, optionally generate "
             "cert, build AgentDocument, install everything in the "
             "agents directory.",
    )
    _add_genesis_flags(p_reg)
    p_reg.add_argument(
        "--agents-dir", required=True,
        help="Path to the daemon's agents directory.",
    )
    _add_document_flags(p_reg)
    p_reg.add_argument(
        "--with-cert", action="store_true",
        help="Also generate an Agent Cert and install it.",
    )
    p_reg.add_argument("--ca-cert")
    p_reg.add_argument("--ca-key")
    p_reg.add_argument(
        "--cert-valid-days", type=int,
        help="Cert validity in days when --with-cert is set.",
    )
    p_reg.add_argument(
        "--trust-anchor",
        help="Path to a JSON trust-anchors file (Pattern 3 — "
             "Genesis-issuer federation). Copied into the agents "
             "directory as trust-anchors.json.",
    )
    p_reg.add_argument(
        "--force", action="store_true",
        help="Overwrite existing files in the agents directory.",
    )
    p_reg.set_defaults(func=_cmd_register)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

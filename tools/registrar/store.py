"""
RegistrarStore — filesystem-backed storage for an AGTP registrar.

Layout:

    {data_dir}/
        registrar.key            # Ed25519 private key (PEM, 0600)
        registrar.pub            # Ed25519 public key (PEM, 0644)
        issued/
            {agent_id}.json      # one signed Genesis per agent
        issued.jsonl             # append-only audit log of issuances

The data directory is created lazily on first write. Default
location is platform-aware (``~/.agtp/registrar/`` on POSIX,
``%APPDATA%\\agtp\\registrar\\`` on Windows). Operators with multiple
registrars on one host MUST set ``--data-dir`` explicitly to
prevent collisions.

The issuer key is generated on first use and never rotated by the
store. Operators wanting key rotation regenerate the file manually
and reissue all outstanding Geneses (whose signatures depend on the
old key).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core.genesis import (
    AgentGenesis,
    load_genesis_json,
    public_key_pem,
    utc_now_iso,
)


_LOCK = threading.Lock()


def default_data_dir() -> Path:
    """Platform-appropriate default storage directory."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "agtp" / "registrar"
    return Path.home() / ".agtp" / "registrar"


class RegistrarStore:
    """Filesystem-backed registrar store.

    Construction loads (or generates) the issuer keypair. All write
    paths are synchronized through a module-level lock so concurrent
    HTTP handlers can't corrupt the audit log.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        issuer_id: str = "registrar.local",
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.issuer_id = issuer_id
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._issued_dir = self.data_dir / "issued"
        self._issued_dir.mkdir(exist_ok=True)
        self._audit_log = self.data_dir / "issued.jsonl"
        self._key_path = self.data_dir / "registrar.key"
        self._pub_path = self.data_dir / "registrar.pub"
        self._private_key = self._load_or_generate_key()
        self._public_key_pem = public_key_pem(self._private_key.public_key())

    # ----- Issuer key -----

    def _load_or_generate_key(self) -> Ed25519PrivateKey:
        if self._key_path.exists():
            pem = self._key_path.read_bytes()
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise RuntimeError(
                    f"registrar key at {self._key_path} is not Ed25519"
                )
            return key
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._key_path.write_bytes(pem)
        try:
            os.chmod(self._key_path, 0o600)
        except OSError:
            pass
        # Mirror the public half for convenient distribution.
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._pub_path.write_bytes(pub_pem)
        return key

    @property
    def issuer_public_key_pem(self) -> str:
        return self._public_key_pem

    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    # ----- Issuance -----

    def issue(
        self,
        *,
        name: str,
        owner_id: str,
        principal_id: str,
        agent_public_key_pem: str,
        archetype: Optional[str] = None,
        governance_zone: Optional[str] = None,
        trust_tier: int = 2,
        verification_path: str = "self-signed",
    ) -> AgentGenesis:
        """Mint and sign a new Agent Genesis. Persists to disk.

        Returns the signed :class:`AgentGenesis`. Caller hands it to
        the requester (downloads it, attaches it to a cert
        generation flow, etc.).
        """
        genesis = AgentGenesis(
            name=name,
            owner_id=owner_id,
            principal_id=principal_id or owner_id,
            agent_public_key=agent_public_key_pem,
            archetype=archetype,
            governance_zone=governance_zone,
            trust_tier=trust_tier,
            verification_path=verification_path,
            issued_at=utc_now_iso(),
            issuer=self.issuer_id,
            issuer_public_key=self._public_key_pem,
        )
        genesis.sign(self._private_key)
        agent_id = genesis.canonical_agent_id()
        out_path = self._issued_dir / f"{agent_id}.json"
        with _LOCK:
            out_path.write_text(
                genesis.to_pretty_json() + "\n", encoding="utf-8",
            )
            self._append_audit({
                "agent_id": agent_id,
                "name": name,
                "owner_id": owner_id,
                "issued_at": genesis.issued_at,
                "issuer": self.issuer_id,
                "trust_tier": trust_tier,
                "verification_path": verification_path,
            })
        return genesis

    def fetch(self, agent_id: str) -> Optional[AgentGenesis]:
        """Read a previously-issued Genesis from disk. ``None`` when
        the agent_id is unknown to this registrar."""
        # Defensive: refuse path separators in agent_id so a caller
        # can't escape the issued/ directory.
        safe = agent_id.strip().replace("/", "").replace("\\", "")
        path = self._issued_dir / f"{safe}.json"
        if not path.exists():
            return None
        return load_genesis_json(path.read_text(encoding="utf-8"))

    def list_issued(self) -> List[str]:
        """Return the agent_ids of every Genesis issued by this
        registrar."""
        return sorted(
            p.stem for p in self._issued_dir.glob("*.json")
        )

    # ----- Internals -----

    def _append_audit(self, entry: dict) -> None:
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with self._audit_log.open("a", encoding="utf-8") as f:
            f.write(line)

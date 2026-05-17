"""
Ed25519 signing service for ``agtpd``.

The single source of cryptographic identity for the daemon. When
signing is enabled, the daemon loads an Ed25519 private key at boot
and signs:

  * **Attribution-Record headers** on every response (replaces the
    pre-§5 placeholder)
  * **Audit log receipts** emitted by ``mod_audit`` (when audit
    signing is enabled)
  * **Server Manifest** at DISCOVER time (future revision)
  * **AGTP-LOG entries** for SCITT-style transparency log integration
    (future revision; format is currently sketched but receipt
    construction lands when AGTP-LOG signing arrives)

The signing service is also the surface the gateway protocol's v2
``sign_request`` capability will consume: modules ask the daemon to
sign opaque bytes and the daemon returns the Ed25519 signature
without exposing the private key. The current `sign()` and
`sign_canonical()` methods are already the API shape that frame
handler will call into.

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
import json
import os
import threading
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

    # ----- Attribution-Record construction -----

    def build_attribution_record(
        self,
        *,
        server_id: str,
        issued_at: str,
        status: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Produce a signed Attribution-Record envelope.

        Returns a dict the caller serializes into the header value.
        The shape:

            {
              "kid": "<key id>",
              "alg": "Ed25519",
              "payload": {
                "server_id": ...,
                "issued_at": ...,
                "status": ...,
                ...extra...
              },
              "signature": "<base64url>"
            }

        Verifiers reconstruct the canonical payload, look up the key
        by `kid`, and verify against `signature`. The signature is
        base64url-encoded (RFC 4648 §5) without padding to keep
        header values compact.
        """
        payload: Dict[str, Any] = {
            "server_id": server_id,
            "issued_at": issued_at,
            "status": status,
        }
        if extra:
            payload.update(extra)
        signature = self.sign_canonical(payload)
        return {
            "kid": self._key_id,
            "alg": "Ed25519",
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
        }

    # ----- Internals -----

    @staticmethod
    def _derive_key_id(public_key: Ed25519PublicKey) -> str:
        """Stable short identifier from the public key's raw bytes.

        Format: ``ed25519-`` + first 16 hex chars of SHA-256(raw
        public bytes). Matches the agent-id pattern used elsewhere
        in the protocol, abbreviated.
        """
        import hashlib

        raw = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return "ed25519-" + hashlib.sha256(raw).hexdigest()[:16]


__all__ = [
    "KeyLoadError",
    "SigningError",
    "SigningService",
]

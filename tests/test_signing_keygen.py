"""Tests for the tools.generate_signing_key CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.signing import SigningService
from tools.generate_signing_key import main


def test_generates_key_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "signing"
    rc = main([str(out)])
    assert rc == 0

    priv = tmp_path / "signing.key"
    pub = tmp_path / "signing.pub"
    assert priv.exists()
    assert pub.exists()

    # Private key parses through the same loader the daemon uses.
    service = SigningService.from_key_path(str(priv))
    assert service.key_id.startswith("ed25519-")

    # Public key is valid PEM.
    assert pub.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")

    # The printed output mentions the key id and config snippet.
    captured = capsys.readouterr()
    assert service.key_id in captured.out
    assert "[signing]" in captured.out
    assert 'enabled  = true' in captured.out


def test_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    out = tmp_path / "signing"
    assert main([str(out)]) == 0
    # Second run should refuse.
    assert main([str(out)]) == 2


def test_force_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "signing"
    assert main([str(out)]) == 0
    capsys.readouterr()  # drain
    first_kid = (tmp_path / "signing.pub").read_bytes()

    # --force lets us generate again over the same path.
    assert main([str(out), "--force"]) == 0
    second_kid = (tmp_path / "signing.pub").read_bytes()
    # The new key is different.
    assert first_kid != second_kid


def test_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "more" / "signing"
    assert main([str(out)]) == 0
    assert (tmp_path / "nested" / "more" / "signing.key").exists()

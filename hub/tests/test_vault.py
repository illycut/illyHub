from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from illyhub_hub.auth.vault import Vault, VaultError, load_or_create_key, open_vault


def test_key_is_generated_once_with_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "vault.key"
    key = load_or_create_key(None, key_path)
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert load_or_create_key(None, key_path) == key  # stable across restarts
    env = Fernet.generate_key().decode()
    assert load_or_create_key(env, key_path) == env.encode()  # env wins


def test_env_key_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_or_create_key("not-a-key", tmp_path / "k")


def test_put_get_delete_round_trip_is_encrypted_on_disk(tmp_path: Path) -> None:
    v = open_vault(None, tmp_path / "vault.key", tmp_path / "vault.json")
    assert v.get("tidal") is None
    v.put("tidal", {"access_token": "SECRET-TOKEN", "refresh_token": "R"})
    raw = (tmp_path / "vault.json").read_bytes()
    assert b"SECRET-TOKEN" not in raw and b"access_token" not in raw
    assert stat.S_IMODE((tmp_path / "vault.json").stat().st_mode) == 0o600
    again = open_vault(None, tmp_path / "vault.key", tmp_path / "vault.json")
    assert again.get("tidal") == {"access_token": "SECRET-TOKEN", "refresh_token": "R"}
    assert again.services() == ["tidal"]
    assert again.delete("tidal") is True and again.delete("tidal") is False
    assert open_vault(None, tmp_path / "vault.key", tmp_path / "vault.json").get("tidal") is None


def test_wrong_key_is_a_clear_error(tmp_path: Path) -> None:
    v = open_vault(None, tmp_path / "vault.key", tmp_path / "vault.json")
    v.put("tidal", {"access_token": "x"})
    with pytest.raises(VaultError, match="cannot be decrypted"):
        Vault(tmp_path / "vault.json", Fernet.generate_key())


def test_get_returns_copies(tmp_path: Path) -> None:
    v = open_vault(None, tmp_path / "vault.key", tmp_path / "vault.json")
    v.put("tidal", {"a": 1})
    got = v.get("tidal")
    assert got is not None
    got["a"] = 2
    assert v.get("tidal") == {"a": 1}

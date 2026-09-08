"""Encrypted-at-rest JSON vault for service credentials (PRD §4.3 "Auth vault").

One Fernet-encrypted file under ``HUB_DATA_DIR`` holds a JSON object keyed by service. The key
comes from ``HUB_VAULT_KEY``; when unset the hub generates one on first run and writes it to
``HUB_DATA_DIR/vault.key`` with mode 0600. **That file (and the env var) must never leave the hub
Mac**: whoever holds it can read every stored token. Losing it only means relinking accounts.

Values are never logged. File I/O is small and synchronous; callers run it on the loop thread
during startup or inside link/unlink handlers, which is fine at this volume.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..logsetup import get_logger

log = get_logger("vault")


class VaultError(Exception):
    pass


def load_or_create_key(env_key: str | None, key_path: Path) -> bytes:
    """``HUB_VAULT_KEY`` wins; otherwise read ``key_path`` or generate it (0600)."""
    if env_key:
        key = env_key.strip().encode()
        Fernet(key)  # validates the format early
        return key
    if key_path.exists():
        key = key_path.read_bytes().strip()
        Fernet(key)
        mode = stat.S_IMODE(key_path.stat().st_mode)
        if mode & 0o077:
            log.warning(
                "vault.key is readable by other users; chmod 600 it",
                extra={"extra": {"path": str(key_path), "mode": oct(mode)}},
            )
        return key
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    log.warning(
        "generated a new vault key; back it up only on this machine",
        extra={"extra": {"path": str(key_path)}},
    )
    return key


class Vault:
    """``path=None`` is an in-memory vault: used when the on-disk vault cannot be opened so
    the hub still runs (accounts simply show as not linked and ``last_error`` explains)."""

    def __init__(self, path: Path | None, key: bytes) -> None:
        self.path = path
        self._fernet = Fernet(key)
        self._data: dict[str, Any] = self._read()

    @classmethod
    def in_memory(cls) -> Vault:
        return cls(None, Fernet.generate_key())

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def _read(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            raw = self._fernet.decrypt(self.path.read_bytes())
        except InvalidToken as exc:
            raise VaultError(
                f"{self.path} cannot be decrypted with the current key; relink accounts "
                "or restore the original vault.key"
            ) from exc
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = self._fernet.encrypt(json.dumps(self._data).encode())
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def get(self, service: str) -> dict[str, Any] | None:
        value = self._data.get(service)
        return dict(value) if isinstance(value, dict) else None

    def put(self, service: str, value: dict[str, Any]) -> None:
        self._data[service] = dict(value)
        self._write()

    def delete(self, service: str) -> bool:
        existed = self._data.pop(service, None) is not None
        if existed:
            self._write()
        return existed

    def services(self) -> list[str]:
        return sorted(self._data)


def open_vault(env_key: str | None, key_path: Path, vault_path: Path) -> Vault:
    return Vault(vault_path, load_or_create_key(env_key, key_path))

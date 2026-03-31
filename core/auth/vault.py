"""Encrypted secret vault using Fernet symmetric encryption."""
from __future__ import annotations

import json
from pathlib import Path

from cryptography.fernet import Fernet

from core.config import settings


def _get_key() -> bytes:
    key_path = Path(settings.db_path).parent / ".vault.key"
    if key_path.exists():
        return key_path.read_bytes()
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    try:
        key_path.chmod(0o600)
    except Exception:
        pass
    return key


class SecretVault:
    def __init__(self) -> None:
        self._vault_path = Path(settings.db_path).parent / ".vault.json"
        self._fernet = Fernet(_get_key())

    def _load(self) -> dict[str, str]:
        if not self._vault_path.exists():
            return {}
        encrypted = self._vault_path.read_bytes()
        decrypted = self._fernet.decrypt(encrypted)
        return json.loads(decrypted)

    def _save(self, data: dict[str, str]) -> None:
        plaintext = json.dumps(data).encode()
        encrypted = self._fernet.encrypt(plaintext)
        self._vault_path.write_bytes(encrypted)
        try:
            self._vault_path.chmod(0o600)
        except Exception:
            pass

    def set(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        self._save(data)

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def delete(self, key: str) -> bool:
        data = self._load()
        if key not in data:
            return False
        del data[key]
        self._save(data)
        return True

    def list_keys(self) -> list[str]:
        return list(self._load().keys())


_vault: SecretVault | None = None

def get_vault() -> SecretVault:
    global _vault
    if _vault is None:
        _vault = SecretVault()
    return _vault

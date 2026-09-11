"""Session cookies in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service)."""
from __future__ import annotations

import os
import re

import keyring
from keyring.errors import KeyringError

SERVICE = "wikitcg-completer"
# Windows Credential Manager rejects values above ~1270 characters.
CHUNK = 1000
_ENV = {"session": "WIKITCG_SESSION", "extra": "WIKITCG_EXTRA_COOKIES"}


class VaultError(RuntimeError):
    pass


def env_name(account_id: str, kind: str) -> str:
    return f"{_ENV[kind]}_{re.sub(r'[^A-Z0-9]', '_', account_id.upper())}"


def available() -> bool:
    try:
        return keyring.get_keyring().priority > 0
    except Exception:
        return False


def backend_name() -> str:
    try:
        return type(keyring.get_keyring()).__name__
    except Exception:
        return "none"


def _key(account_id: str, kind: str) -> str:
    if kind not in _ENV:
        raise ValueError(f"Unknown secret kind: {kind}")
    return f"{account_id}:{kind}"


def _chunk_count(key: str) -> int:
    raw = keyring.get_password(SERVICE, key)
    return int(raw) if raw and raw.isdigit() else 0


def _clear(key: str) -> None:
    for name in [f"{key}:{i}" for i in range(_chunk_count(key))] + [key]:
        try:
            keyring.delete_password(SERVICE, name)
        except KeyringError:
            pass


def get(account_id: str, kind: str = "session") -> str:
    key = _key(account_id, kind)
    env = os.environ.get(env_name(account_id, kind))
    if env:
        return env
    try:
        parts = [keyring.get_password(SERVICE, f"{key}:{i}") for i in range(_chunk_count(key))]
    except KeyringError:
        return ""
    return "" if None in parts else "".join(parts)


def store(account_id: str, kind: str, value: str) -> None:
    key = _key(account_id, kind)
    try:
        _clear(key)
        if not value:
            return
        chunks = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
        for i, part in enumerate(chunks):
            keyring.set_password(SERVICE, f"{key}:{i}", part)
        keyring.set_password(SERVICE, key, str(len(chunks)))
    except KeyringError as exc:
        raise VaultError(f"No secure OS keyring available: set the {env_name(account_id, kind)} "
                         "environment variable instead.") from exc


def delete(account_id: str) -> None:
    for kind in _ENV:
        try:
            _clear(_key(account_id, kind))
        except KeyringError:
            pass

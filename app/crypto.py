"""Encryption for Plaid access tokens at rest.

ENCRYPTION_KEY_SOURCE controls where the Fernet key comes from:
  - macos_keychain (default, recommended) — generated once and stored in the
    system Keychain via `keyring`. Never touches disk in plaintext, never
    enters git, and survives app restarts without living in .env.
  - passphrase — derived from ENCRYPTION_PASSPHRASE via PBKDF2. A fallback
    for non-macOS or when Keychain access isn't available.

Either way, only the ENCRYPTED token ever reaches SQLite (db.py never sees
a plaintext access_token) — this module is the sole place plaintext exists,
and only in memory.
"""

from __future__ import annotations

import base64
import os

import keyring
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_KEYCHAIN_ACCOUNT = "plaid-token-encryption-key"


def _keychain_key(service: str) -> bytes:
    existing = keyring.get_password(service, _KEYCHAIN_ACCOUNT)
    if existing:
        return existing.encode()
    # First run: generate once, store once. Every later run (and every other
    # process on this Mac) fetches the same key from the Keychain — it's
    # never written anywhere else, so there's no plaintext copy to leak.
    key = Fernet.generate_key()
    keyring.set_password(service, _KEYCHAIN_ACCOUNT, key.decode())
    return key


def _passphrase_key(passphrase: str) -> bytes:
    # A static salt is fine here: the secret is the passphrase, not the salt,
    # and this key only protects Plaid tokens that never leave this machine.
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                      salt=b"cc-benefits-tracker-v1", iterations=390_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    source = os.getenv("ENCRYPTION_KEY_SOURCE", "macos_keychain")
    if source == "passphrase":
        passphrase = os.getenv("ENCRYPTION_PASSPHRASE")
        if not passphrase:
            raise RuntimeError(
                "ENCRYPTION_KEY_SOURCE=passphrase requires ENCRYPTION_PASSPHRASE to be set")
        key = _passphrase_key(passphrase)
    elif source == "macos_keychain":
        service = os.getenv("KEYCHAIN_SERVICE_NAME", "cc-benefits-tracker")
        key = _keychain_key(service)
    else:
        raise RuntimeError(f"Unknown ENCRYPTION_KEY_SOURCE: {source!r}")
    _fernet = Fernet(key)
    return _fernet


def reset_cached_key() -> None:
    """Test-only: force the next encrypt/decrypt to re-derive the key."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()

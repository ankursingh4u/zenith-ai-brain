"""Encryption for secrets at rest (Google OAuth tokens).

We never store Google tokens in plain text. They are Fernet-encrypted with
ENCRYPTION_KEY before touching the database, and decrypted only in memory
when needed to call a Google API.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

import config

_fernet = Fernet(config.ENCRYPTION_KEY)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()

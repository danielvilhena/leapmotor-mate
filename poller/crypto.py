"""Symmetric at-rest encryption for secret settings (Fernet / cryptography).

Identical copy in poller/ and web/ (the two dirs are separate import roots, like
session_share.py). Key resolution order:
  1. MATE_SECRET_KEY env var (user override) — any passphrase, derived to a key.
  2. <data_dir>/secret.key file — auto-generated on first use (mode 0600).

decrypt() is total: empty -> empty, legacy plaintext -> unchanged, ciphertext ->
plaintext. This lets existing installs migrate transparently (read still works
before the one-time re-encrypt migration has run). Never logs keys or secrets.
"""
import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _key_path() -> Path:
    db_path = os.environ.get("DB_PATH", "leapmotor_mate.db")
    return Path(db_path).resolve().parent / "secret.key"


def _derive_key(passphrase: str) -> bytes:
    """Turn any user passphrase into a valid 32-byte url-safe Fernet key."""
    return base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())


def load_or_create_key() -> bytes:
    """Resolve the Fernet key. Env override wins; otherwise a persisted key file
    in /data, generated atomically on first run so the poller and web processes
    booting together can't clobber each other (the loser re-reads the winner's)."""
    env = os.environ.get("MATE_SECRET_KEY")
    if env:
        return _derive_key(env)
    p = _key_path()
    if p.exists():
        return p.read_bytes().strip()
    key = Fernet.generate_key()
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        log.info("Generated new secret key at %s", p)
        return key
    except FileExistsError:
        return p.read_bytes().strip()   # another process created it first


_fernet = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(load_or_create_key())
    return _fernet


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(value: str) -> str:
    """Encrypt a plaintext string. Empty stays empty; already-encrypted is a no-op."""
    if not value or is_encrypted(value):
        return value
    return _PREFIX + _f().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Return plaintext. Total over empty / legacy-plaintext / ciphertext. On a
    decrypt failure (lost or changed key) returns the raw value rather than raising."""
    if not value or not is_encrypted(value):
        return value
    try:
        return _f().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        log.warning("A stored secret could not be decrypted (wrong/lost key?)")
        return value

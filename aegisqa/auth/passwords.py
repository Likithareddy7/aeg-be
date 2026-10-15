"""scrypt password hashes in the original backend's "salt:hex" format (Node's
scryptSync defaults: N=16384, r=8, p=1, 64-byte key), so existing users can
still sign in."""

from __future__ import annotations

import hashlib
import hmac
import secrets

_KEY_LENGTH = 64
_N, _R, _P = 16384, 8, 1


def _derive(password: str, salt: str) -> bytes:
  return hashlib.scrypt(
    password.encode("utf-8"), salt=salt.encode("utf-8"), n=_N, r=_R, p=_P, dklen=_KEY_LENGTH, maxmem=64 * 1024 * 1024
  )


def hash_password(password: str) -> str:
  salt = secrets.token_hex(16)
  return f"{salt}:{_derive(password, salt).hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
  salt, _, derived_hex = stored_hash.partition(":")
  if not salt or not derived_hex:
    return False
  try:
    stored = bytes.fromhex(derived_hex)
  except ValueError:
    return False
  return hmac.compare_digest(_derive(password, salt), stored)

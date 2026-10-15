"""AES-256-GCM for credentials at rest (environment secrets, integration
tokens). The stored format -- JSON {iv, authTag, ciphertext}, hex-encoded,
12-byte IV -- is the one the original backend wrote, so existing rows still
decrypt."""

from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

_TAG_BYTES = 16


def _key() -> bytes:
  if not settings.credential_encryption_key:
    raise ValueError(
      "CREDENTIAL_ENCRYPTION_KEY is not set -- required to store credentials. "
      'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
    )
  try:
    key = bytes.fromhex(settings.credential_encryption_key)
  except ValueError as err:
    raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be 64 hex characters.") from err
  if len(key) != 32:
    raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be a 32-byte value encoded as 64 hex characters.")
  return key


def encrypt_secret(plaintext: str) -> str:
  iv = os.urandom(12)
  sealed = AESGCM(_key()).encrypt(iv, plaintext.encode("utf-8"), None)
  ciphertext, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
  return json.dumps({"iv": iv.hex(), "authTag": tag.hex(), "ciphertext": ciphertext.hex()}, separators=(",", ":"))


def decrypt_secret(serialized: str) -> str:
  payload = json.loads(serialized)
  sealed = bytes.fromhex(payload["ciphertext"]) + bytes.fromhex(payload["authTag"])
  return AESGCM(_key()).decrypt(bytes.fromhex(payload["iv"]), sealed, None).decode("utf-8")

"""
Field-level encryption for connector configs (GV-2).

Fernet (AES-128-CBC + HMAC) keyed from ``CONNECTOR_CONFIG_ENCRYPTION_KEY``.
When the key is unset every function is a pass-through, so dev/demo deployments
keep plaintext JSON and production opts in via env.

Encrypted payload shape: ``{"_enc": "<fernet token>"}`` — self-describing, so
mixed plaintext/ciphertext rows migrate lazily on next save.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)
_MARKER = "_enc"


def _fernet():
    key = getattr(settings, "CONNECTOR_CONFIG_ENCRYPTION_KEY", "")
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode())


def encrypt_config(config: dict) -> dict:
    """Encrypt a config dict (no-op when no key is configured or already encrypted)."""
    f = _fernet()
    if f is None or not isinstance(config, dict) or _MARKER in config:
        return config
    return {_MARKER: f.encrypt(json.dumps(config).encode()).decode()}


def decrypt_config(config: dict) -> dict:
    """Decrypt a stored config dict; plaintext rows pass through unchanged."""
    if not isinstance(config, dict) or _MARKER not in config:
        return config or {}
    f = _fernet()
    if f is None:
        logger.error("Encrypted connector config found but CONNECTOR_CONFIG_ENCRYPTION_KEY is unset.")
        return {}
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(f.decrypt(config[_MARKER].encode()).decode())
    except (InvalidToken, ValueError) as exc:
        logger.error("Connector config decryption failed: %s", exc)
        return {}

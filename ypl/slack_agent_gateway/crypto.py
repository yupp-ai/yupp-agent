"""Cryptographic helpers for Slack Agent Gateway.

Provides Fernet-based encryption/decryption for OAuth tokens and secrets.
Uses a dedicated encryption key stored in Secret Manager.

Migration note: Previously, the encryption key was derived from SLACK_BOT_FATHER_SIGNING_SECRET
via SHA256. For one-time migration of existing encrypted data, pass use_legacy_key=True to
decrypt functions. New data should always use the default (new) key.
"""

import base64
import hashlib
import json
from datetime import datetime

from cryptography.fernet import Fernet

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


def _get_fernet(use_legacy_key: bool = False) -> Fernet:
    """Get a Fernet instance for encryption/decryption.

    Args:
        use_legacy_key: If True, use the legacy key derived from SLACK_BOT_FATHER_SIGNING_SECRET.
                        If False (default), use the dedicated SLACK_AGENT_GW_ENCRYPTION_KEY.

    Returns:
        Fernet instance for encryption/decryption.

    Raises:
        ValueError: If the required key is not configured.
    """
    if use_legacy_key:
        signing_secret = settings.SLACK_BOT_FATHER_SIGNING_SECRET
        if not signing_secret:
            raise ValueError("SLACK_BOT_FATHER_SIGNING_SECRET not configured for legacy decryption")
        key_bytes = hashlib.sha256(signing_secret.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        return Fernet(fernet_key)

    encryption_key = settings.SLACK_AGENT_GW_ENCRYPTION_KEY
    if not encryption_key:
        raise ValueError("SLACK_AGENT_GW_ENCRYPTION_KEY not configured for token encryption")
    return Fernet(encryption_key.encode())


def encrypt_token_data(token: str, expires_at: datetime) -> str:
    """Encrypt token and expiry for Redis storage.

    Args:
        token: The access token to encrypt.
        expires_at: Token expiration timestamp.

    Returns:
        Encrypted string suitable for Redis storage.
    """
    fernet = _get_fernet()
    data = json.dumps({"token": token, "expires_at": expires_at.isoformat()})
    return fernet.encrypt(data.encode()).decode()


def decrypt_token_data(
    encrypted: str,
    use_legacy_key: bool = False,
) -> tuple[str, datetime] | None:
    """Decrypt token data from Redis.

    Args:
        encrypted: Encrypted string from Redis.
        use_legacy_key: If True, use the legacy key (for migrating old data).

    Returns:
        Tuple of (token, expires_at) or None if decryption fails.
    """
    try:
        fernet = _get_fernet(use_legacy_key=use_legacy_key)
        data = json.loads(fernet.decrypt(encrypted.encode()).decode())
        return data["token"], datetime.fromisoformat(data["expires_at"])
    except Exception as e:
        logger.warning("Failed to decrypt cached token", error=str(e), use_legacy_key=use_legacy_key)
        return None


def encrypt_secret(secret: str) -> str:
    """Encrypt a secret string for storage.

    Args:
        secret: The secret to encrypt.

    Returns:
        Encrypted string suitable for storage.
    """
    fernet = _get_fernet()
    return fernet.encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str, use_legacy_key: bool = False) -> str | None:
    """Decrypt a secret string.

    Args:
        encrypted: Encrypted string.
        use_legacy_key: If True, use the legacy key (for migrating old data).

    Returns:
        Decrypted secret or None if decryption fails.
    """
    try:
        fernet = _get_fernet(use_legacy_key=use_legacy_key)
        return fernet.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.warning("Failed to decrypt secret", error=str(e), use_legacy_key=use_legacy_key)
        return None

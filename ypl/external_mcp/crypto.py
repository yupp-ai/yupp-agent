"""Fernet encryption helpers for external-MCP grants and server secrets.

Mirrors :mod:`ypl.slack_agent_gateway.crypto` but with its own dedicated key
so a rotation of one doesn't ripple into the other.  Operators set
``MCP_USER_GRANT_ENCRYPTION_KEY`` in ``.env`` (the Mac installer generates
it on first run).
"""

from cryptography.fernet import Fernet

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


def _get_fernet() -> Fernet:
    """Return a Fernet instance bound to ``MCP_USER_GRANT_ENCRYPTION_KEY``.

    Raises:
        ValueError: when the key is not configured — callers must treat
            this as a setup error rather than a runtime fallback.
    """
    key = settings.MCP_USER_GRANT_ENCRYPTION_KEY
    if not key:
        raise ValueError(
            "MCP_USER_GRANT_ENCRYPTION_KEY is not configured. "
            "Generate a Fernet key (cryptography.fernet.Fernet.generate_key) "
            "and set it in .env before using the external-MCP feature."
        )
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    """Encrypt a UTF-8 string with the grant key.  Returns urlsafe-base64 text."""
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(encrypted: str) -> str | None:
    """Decrypt a Fernet ciphertext.  Returns ``None`` on failure rather than
    raising so callers can degrade gracefully (a corrupted grant should be
    re-authorized, not crash the resolver)."""
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.warning("Failed to decrypt MCP grant value", error=str(e))
        return None

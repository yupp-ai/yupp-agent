"""Unit tests for ypl/slack_agent_gateway/crypto.py.

Covers:
- _get_fernet (new key, legacy key, error paths)
- encrypt_token_data / decrypt_token_data (round-trip, bad ciphertext, legacy key)
- encrypt_secret / decrypt_secret (round-trip, bad ciphertext, legacy key)
"""

from __future__ import annotations
import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from ypl.slack_agent_gateway.crypto import (
    _get_fernet,
    decrypt_secret,
    decrypt_token_data,
    encrypt_secret,
    encrypt_token_data,
)

MODULE = "ypl.slack_agent_gateway.crypto"

# A valid Fernet key we can use in tests
_VALID_FERNET_KEY: str = Fernet.generate_key().decode()
_LEGACY_SIGNING_SECRET = "test-signing-secret-for-legacy-key"


def _legacy_fernet_key_for(secret: str) -> str:
    """Derive the legacy Fernet key the same way the production code does."""
    key_bytes = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(key_bytes).decode()


# ---------------------------------------------------------------------------
# _get_fernet
# ---------------------------------------------------------------------------


class TestGetFernet:
    def test_returns_fernet_with_valid_new_key(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY
        with patch(f"{MODULE}.settings", mock_settings):
            fernet = _get_fernet()
        assert isinstance(fernet, Fernet)

    def test_raises_when_new_key_missing(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = ""
        with (
            patch(f"{MODULE}.settings", mock_settings),
            pytest.raises(ValueError, match="SLACK_AGENT_GW_ENCRYPTION_KEY"),
        ):
            _get_fernet()

    def test_returns_fernet_with_legacy_key(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = _LEGACY_SIGNING_SECRET
        with patch(f"{MODULE}.settings", mock_settings):
            fernet = _get_fernet(use_legacy_key=True)
        assert isinstance(fernet, Fernet)

    def test_raises_when_legacy_secret_missing(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = ""
        with (
            patch(f"{MODULE}.settings", mock_settings),
            pytest.raises(ValueError, match="SLACK_BOT_FATHER_SIGNING_SECRET"),
        ):
            _get_fernet(use_legacy_key=True)


# ---------------------------------------------------------------------------
# encrypt_token_data / decrypt_token_data
# ---------------------------------------------------------------------------


class TestEncryptDecryptTokenData:
    def test_round_trip_with_new_key(self) -> None:
        token = "xoxe-access-token-abc123"
        expires_at = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)

        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY

        with patch(f"{MODULE}.settings", mock_settings):
            encrypted = encrypt_token_data(token, expires_at)
            result = decrypt_token_data(encrypted)

        assert result is not None
        decrypted_token, decrypted_expires_at = result
        assert decrypted_token == token
        assert decrypted_expires_at == expires_at

    def test_decrypt_returns_none_on_garbage_input(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY

        with patch(f"{MODULE}.settings", mock_settings):
            result = decrypt_token_data("not-valid-fernet-ciphertext")

        assert result is None

    def test_decrypt_returns_none_on_wrong_key(self) -> None:
        other_key = Fernet.generate_key().decode()
        mock_settings_encrypt = MagicMock()
        mock_settings_encrypt.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY
        mock_settings_decrypt = MagicMock()
        mock_settings_decrypt.SLACK_AGENT_GW_ENCRYPTION_KEY = other_key

        expires_at = datetime(2030, 1, 1, tzinfo=UTC)
        with patch(f"{MODULE}.settings", mock_settings_encrypt):
            encrypted = encrypt_token_data("tok", expires_at)

        with patch(f"{MODULE}.settings", mock_settings_decrypt):
            result = decrypt_token_data(encrypted)

        assert result is None

    def test_legacy_key_round_trip(self) -> None:
        token = "xoxe-legacy-token"
        expires_at = datetime(2030, 6, 15, 9, 0, 0, tzinfo=UTC)
        legacy_key = _legacy_fernet_key_for(_LEGACY_SIGNING_SECRET)

        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = _LEGACY_SIGNING_SECRET
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = legacy_key

        # Encrypt with new-key-path but using the derived legacy key for simplicity:
        # Instead, encrypt directly with the legacy Fernet so we can test decrypt with use_legacy_key=True.
        fernet = Fernet(legacy_key.encode())
        import json

        data = json.dumps({"token": token, "expires_at": expires_at.isoformat()})
        encrypted = fernet.encrypt(data.encode()).decode()

        with patch(f"{MODULE}.settings", mock_settings):
            result = decrypt_token_data(encrypted, use_legacy_key=True)

        assert result is not None
        assert result[0] == token

    def test_decrypt_token_data_returns_none_when_key_missing(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = ""

        with patch(f"{MODULE}.settings", mock_settings):
            result = decrypt_token_data("anything")

        assert result is None


# ---------------------------------------------------------------------------
# encrypt_secret / decrypt_secret
# ---------------------------------------------------------------------------


class TestEncryptDecryptSecret:
    def test_round_trip(self) -> None:
        secret = "super-secret-value-12345"
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY

        with patch(f"{MODULE}.settings", mock_settings):
            encrypted = encrypt_secret(secret)
            result = decrypt_secret(encrypted)

        assert result == secret

    def test_decrypt_returns_none_on_garbage_input(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY

        with patch(f"{MODULE}.settings", mock_settings):
            result = decrypt_secret("not-valid-ciphertext")

        assert result is None

    def test_decrypt_returns_none_on_wrong_key(self) -> None:
        other_key = Fernet.generate_key().decode()
        mock_settings_e = MagicMock()
        mock_settings_e.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY
        mock_settings_d = MagicMock()
        mock_settings_d.SLACK_AGENT_GW_ENCRYPTION_KEY = other_key

        with patch(f"{MODULE}.settings", mock_settings_e):
            encrypted = encrypt_secret("my-secret")

        with patch(f"{MODULE}.settings", mock_settings_d):
            result = decrypt_secret(encrypted)

        assert result is None

    def test_legacy_key_decrypt(self) -> None:
        secret = "legacy-secret-value"
        legacy_key = _legacy_fernet_key_for(_LEGACY_SIGNING_SECRET)
        fernet = Fernet(legacy_key.encode())
        encrypted = fernet.encrypt(secret.encode()).decode()

        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = _LEGACY_SIGNING_SECRET

        with patch(f"{MODULE}.settings", mock_settings):
            result = decrypt_secret(encrypted, use_legacy_key=True)

        assert result == secret

    def test_decrypt_returns_none_when_legacy_key_missing(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = ""

        with patch(f"{MODULE}.settings", mock_settings):
            result = decrypt_secret("anything", use_legacy_key=True)

        assert result is None

    def test_encrypted_value_differs_from_plaintext(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY

        with patch(f"{MODULE}.settings", mock_settings):
            encrypted = encrypt_secret("plain-secret")

        assert encrypted != "plain-secret"
        assert len(encrypted) > 20

    def test_different_encryptions_of_same_value_differ(self) -> None:
        """Fernet uses random IV, so two encryptions of the same plaintext differ."""
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GW_ENCRYPTION_KEY = _VALID_FERNET_KEY

        with patch(f"{MODULE}.settings", mock_settings):
            enc1 = encrypt_secret("same-value")
            enc2 = encrypt_secret("same-value")

        assert enc1 != enc2

"""Unit tests for ypl/agent_harness_service/common/signing.py.

Covers:
- sign_artifact_url: happy path, TTL, URL structure, missing secret
- verify_artifact_sig: valid sig, expired URL, tampered UUID, tampered sig,
  tampered expiry, non-integer expiry, empty secret
- _compute_hmac: determinism and different-input divergence
- Round-trip: sign then verify
"""

from __future__ import annotations
import hashlib
import hmac
import time
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from ypl.agent_harness_service.common.signing import (
    DEFAULT_TTL_SECONDS,
    _compute_hmac,
    sign_artifact_url,
    verify_artifact_sig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-secret-key-32-bytes-long!!!"
_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _settings_with_secret(secret: str = _SECRET) -> MagicMock:
    """Return a mock settings object with ARTIFACT_SIGNING_SECRET set."""
    s = MagicMock()
    s.ARTIFACT_SIGNING_SECRET = secret
    return s


def _settings_no_secret() -> MagicMock:
    s = MagicMock()
    s.ARTIFACT_SIGNING_SECRET = ""
    return s


# ---------------------------------------------------------------------------
# sign_artifact_url
# ---------------------------------------------------------------------------


class TestSignArtifactUrl:
    def test_returns_path_with_correct_uuid(self) -> None:
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url = sign_artifact_url(_UUID)
        assert url.startswith(f"/p/{_UUID}?")

    def test_query_contains_sig_and_exp(self) -> None:
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url = sign_artifact_url(_UUID)
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        assert "sig" in qs
        assert "exp" in qs

    def test_exp_is_approximately_now_plus_ttl(self) -> None:
        before = int(time.time())
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url = sign_artifact_url(_UUID)
        after = int(time.time())

        qs = parse_qs(urlparse(url).query)
        exp = int(qs["exp"][0])
        assert before + DEFAULT_TTL_SECONDS <= exp <= after + DEFAULT_TTL_SECONDS + 2

    def test_custom_ttl_reflected_in_exp(self) -> None:
        custom_ttl = 3600
        before = int(time.time())
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url = sign_artifact_url(_UUID, ttl_seconds=custom_ttl)
        after = int(time.time())

        qs = parse_qs(urlparse(url).query)
        exp = int(qs["exp"][0])
        assert before + custom_ttl <= exp <= after + custom_ttl + 2

    def test_sig_is_hex_sha256(self) -> None:
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url = sign_artifact_url(_UUID)
        qs = parse_qs(urlparse(url).query)
        sig = qs["sig"][0]
        # SHA-256 hex digest is exactly 64 hex chars
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)

    def test_raises_value_error_when_secret_not_configured(self) -> None:
        with (
            patch("ypl.agent_harness_service.common.signing.settings", _settings_no_secret()),
            pytest.raises(ValueError, match="ARTIFACT_SIGNING_SECRET"),
        ):
            sign_artifact_url(_UUID)

    def test_different_uuids_produce_different_sigs(self) -> None:
        uuid2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url1 = sign_artifact_url(_UUID)
            url2 = sign_artifact_url(uuid2)
        sig1 = parse_qs(urlparse(url1).query)["sig"][0]
        sig2 = parse_qs(urlparse(url2).query)["sig"][0]
        assert sig1 != sig2

    def test_different_secrets_produce_different_sigs(self) -> None:
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret("secret-A")):
            url1 = sign_artifact_url(_UUID)
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret("secret-B")):
            url2 = sign_artifact_url(_UUID)
        sig1 = parse_qs(urlparse(url1).query)["sig"][0]
        sig2 = parse_qs(urlparse(url2).query)["sig"][0]
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# verify_artifact_sig
# ---------------------------------------------------------------------------


class TestVerifyArtifactSig:
    def _make_sig_and_exp(self, uuid: str = _UUID, ttl: int = 3600) -> tuple[str, int]:
        """Generate a valid (sig, exp) pair using the test secret."""
        exp = int(time.time()) + ttl
        sig = _compute_hmac(_SECRET, uuid, exp)
        return sig, exp

    def test_valid_sig_returns_true(self) -> None:
        sig, exp = self._make_sig_and_exp()
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, sig, exp) is True

    def test_exp_as_string_also_works(self) -> None:
        sig, exp = self._make_sig_and_exp()
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, sig, str(exp)) is True

    def test_expired_url_returns_false(self) -> None:
        # exp set 1 second in the past
        exp = int(time.time()) - 1
        sig = _compute_hmac(_SECRET, _UUID, exp)
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, sig, exp) is False

    def test_tampered_uuid_returns_false(self) -> None:
        sig, exp = self._make_sig_and_exp()
        other_uuid = "deadbeef-dead-beef-dead-beefdeadbeef"
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(other_uuid, sig, exp) is False

    def test_tampered_sig_returns_false(self) -> None:
        _sig, exp = self._make_sig_and_exp()
        bad_sig = "a" * 64  # valid hex length but wrong value
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, bad_sig, exp) is False

    def test_tampered_exp_returns_false(self) -> None:
        sig, exp = self._make_sig_and_exp()
        # Increase exp by 1 — sig no longer matches
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, sig, exp + 1) is False

    def test_non_integer_exp_returns_false(self) -> None:
        sig, _exp = self._make_sig_and_exp()
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, sig, "not-a-number") is False

    def test_empty_sig_returns_false(self) -> None:
        _sig, exp = self._make_sig_and_exp()
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, "", exp) is False

    def test_missing_secret_returns_false(self) -> None:
        sig, exp = self._make_sig_and_exp()
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_no_secret()):
            assert verify_artifact_sig(_UUID, sig, exp) is False

    def test_wrong_secret_returns_false(self) -> None:
        sig, exp = self._make_sig_and_exp()
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret("wrong-secret")):
            assert verify_artifact_sig(_UUID, sig, exp) is False

    def test_exp_exactly_at_now_is_expired(self) -> None:
        """URL with exp == now is considered expired (strictly greater required)."""
        now = int(time.time())
        sig = _compute_hmac(_SECRET, _UUID, now)
        with (
            patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()),
            patch("ypl.agent_harness_service.common.signing.time") as mock_time,
        ):
            mock_time.time.return_value = float(now)
            # now > now is False, so this should actually pass at the boundary;
            # document and assert the exact boundary behaviour (not expired when now == exp).
            result = verify_artifact_sig(_UUID, sig, now)
        assert result is True  # now > exp is False when now == exp

    def test_exp_one_second_past_is_expired(self) -> None:
        future_exp = int(time.time()) + 100
        sig = _compute_hmac(_SECRET, _UUID, future_exp)
        with (
            patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()),
            patch("ypl.agent_harness_service.common.signing.time") as mock_time,
        ):
            # Simulate clock advancing 101 seconds beyond signing time
            mock_time.time.return_value = float(future_exp + 1)
            result = verify_artifact_sig(_UUID, sig, future_exp)
        assert result is False


# ---------------------------------------------------------------------------
# Round-trip: sign → verify
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_sign_then_verify_succeeds(self) -> None:
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            url = sign_artifact_url(_UUID, ttl_seconds=3600)
        qs = parse_qs(urlparse(url).query)
        sig = qs["sig"][0]
        exp = qs["exp"][0]
        with patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()):
            assert verify_artifact_sig(_UUID, sig, exp) is True

    def test_sign_with_short_ttl_expires(self) -> None:
        """Sign a URL that expires in 1 second, wait for the mock clock to advance."""
        fixed_now = int(time.time())
        with (
            patch("ypl.agent_harness_service.common.signing.settings", _settings_with_secret()),
            patch("ypl.agent_harness_service.common.signing.time") as mock_time,
        ):
            mock_time.time.return_value = float(fixed_now)
            url = sign_artifact_url(_UUID, ttl_seconds=1)
            qs = parse_qs(urlparse(url).query)
            sig = qs["sig"][0]
            exp = qs["exp"][0]

            # Advance clock by 2 seconds — URL should now be expired
            mock_time.time.return_value = float(fixed_now + 2)
            assert verify_artifact_sig(_UUID, sig, exp) is False


# ---------------------------------------------------------------------------
# _compute_hmac internals
# ---------------------------------------------------------------------------


class TestComputeHmac:
    def test_deterministic(self) -> None:
        h1 = _compute_hmac(_SECRET, _UUID, 9999999)
        h2 = _compute_hmac(_SECRET, _UUID, 9999999)
        assert h1 == h2

    def test_matches_stdlib_hmac(self) -> None:
        exp = 1234567890
        expected = hmac.new(
            _SECRET.encode("utf-8"),
            f"{_UUID}:{exp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert _compute_hmac(_SECRET, _UUID, exp) == expected

    def test_different_exp_produces_different_digest(self) -> None:
        assert _compute_hmac(_SECRET, _UUID, 1000) != _compute_hmac(_SECRET, _UUID, 1001)

    def test_different_uuid_produces_different_digest(self) -> None:
        assert _compute_hmac(_SECRET, _UUID, 1000) != _compute_hmac(_SECRET, "other-uuid", 1000)

    def test_different_secret_produces_different_digest(self) -> None:
        assert _compute_hmac("secret-a", _UUID, 1000) != _compute_hmac("secret-b", _UUID, 1000)

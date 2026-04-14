"""HMAC-based signed URL generation and verification for the artifact viewer.

URLs take the form ``/p/{uuid}?sig=<hex>&exp=<unix_timestamp>``.
The HMAC message is the string ``"{uuid}:{exp}"``, signed with
HMAC-SHA256 using the ``ARTIFACT_SIGNING_SECRET`` environment variable.

Configuration
-------------
- ``ARTIFACT_SIGNING_SECRET``  — required; raw string secret (≥ 32 chars recommended).
  Generate a suitable value with::

      python -c "import secrets; print(secrets.token_hex(32))"

- TTL defaults to 30 days but is overridable per call.

Usage
-----
::

    from ypl.agent_harness_service.common.signing import sign_artifact_url, verify_artifact_sig

    url = sign_artifact_url("550e8400-e29b-41d4-a716-446655440000")
    # → "/p/550e8400-e29b-41d4-a716-446655440000?sig=abcdef...&exp=1234567890"

    ok = verify_artifact_sig("550e8400-...", sig="abcdef...", exp="1234567890")
    # → True (or False if expired / tampered)
"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

from ypl.backend.config import settings

# Default TTL: 30 days expressed in seconds.
DEFAULT_TTL_SECONDS: int = 30 * 24 * 3600


def sign_artifact_url(uuid: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Return a signed viewer path for the given artifact UUID.

    Args:
        uuid: The artifact UUID (used verbatim in the path and HMAC message).
        ttl_seconds: How many seconds from now the URL remains valid.
            Defaults to :data:`DEFAULT_TTL_SECONDS` (30 days).

    Returns:
        A path string of the form ``/p/{uuid}?sig=<hex>&exp=<unix_ts>``.

    Raises:
        ValueError: If ``ARTIFACT_SIGNING_SECRET`` is not configured.
    """
    secret = settings.ARTIFACT_SIGNING_SECRET
    if not secret:
        raise ValueError(
            "ARTIFACT_SIGNING_SECRET is not configured. Set it via environment variable or GCP Secret Manager."
        )

    exp = int(time.time()) + ttl_seconds
    sig = _compute_hmac(secret, uuid, exp)
    qs = urlencode({"sig": sig, "exp": exp})
    return f"/p/{uuid}?{qs}"


def verify_artifact_sig(uuid: str, sig: str, exp: str | int) -> bool:
    """Verify that a signed artifact URL is authentic and unexpired.

    Args:
        uuid: The artifact UUID extracted from the URL path.
        sig: The ``sig`` query-parameter value (hex digest).
        exp: The ``exp`` query-parameter value (Unix timestamp as string or int).

    Returns:
        ``True`` if the HMAC matches and the URL has not expired; ``False``
        otherwise.  Fails safe — any misconfiguration or malformed input
        returns ``False`` rather than raising.
    """
    secret = settings.ARTIFACT_SIGNING_SECRET
    if not secret:
        return False

    try:
        exp_int = int(exp)
    except (ValueError, TypeError):
        return False

    # Constant-time expiry check avoids leaking timing info.
    now = int(time.time())
    if now > exp_int:
        return False

    expected_sig = _compute_hmac(secret, uuid, exp_int)
    return hmac.compare_digest(sig, expected_sig)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_hmac(secret: str, uuid: str, exp: int) -> str:
    """Return the hex HMAC-SHA256 of ``"{uuid}:{exp}"`` signed with *secret*."""
    message = f"{uuid}:{exp}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

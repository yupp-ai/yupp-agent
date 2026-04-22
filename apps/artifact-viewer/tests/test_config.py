"""Config helpers — allowlist semantics."""

from __future__ import annotations

from artifact_viewer.config import Settings


def _s(**overrides: str) -> Settings:
    base = {
        "VIEWER_ALLOWED_EMAIL_DOMAINS": "agcouch.com",
        "VIEWER_ALLOWED_EMAILS": "",
        "AGENT_HARNESS_SERVICE_API_KEY": "x",
        "VIEWER_GOOGLE_CLIENT_ID": "x",
        "VIEWER_GOOGLE_CLIENT_SECRET": "x",
        "VIEWER_SESSION_SECRET_KEY": "x",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestIsEmailAllowed:
    def test_matches_allowed_domain(self) -> None:
        s = _s()
        assert s.is_email_allowed("alice@agcouch.com")

    def test_rejects_other_domain(self) -> None:
        s = _s()
        assert not s.is_email_allowed("bob@evil.com")

    def test_case_insensitive(self) -> None:
        s = _s()
        assert s.is_email_allowed("Alice@AgCouch.COM")

    def test_empty_email_rejected(self) -> None:
        s = _s()
        assert not s.is_email_allowed(None)
        assert not s.is_email_allowed("")

    def test_multi_domain_allowlist(self) -> None:
        s = _s(VIEWER_ALLOWED_EMAIL_DOMAINS="agcouch.com, example.com")
        assert s.is_email_allowed("a@agcouch.com")
        assert s.is_email_allowed("b@example.com")
        assert not s.is_email_allowed("c@other.com")

    def test_individual_email_allowlist_overrides_domain(self) -> None:
        s = _s(VIEWER_ALLOWED_EMAIL_DOMAINS="", VIEWER_ALLOWED_EMAILS="guest@outside.com")
        assert s.is_email_allowed("guest@outside.com")
        assert not s.is_email_allowed("other@outside.com")

    def test_empty_allowlist_rejects_everyone(self) -> None:
        s = _s(VIEWER_ALLOWED_EMAIL_DOMAINS="", VIEWER_ALLOWED_EMAILS="")
        assert not s.is_email_allowed("anyone@anywhere.com")

"""Tests for ypl.agent_harness_service.common.agent_messaging_authz.

Covers ``check_agent_message_authz``:
- empty/missing recipient name → AgentAuthorizationError
- self-messaging always permitted (regardless of allowed_to_message)
- recipient in allowed_to_message → granted
- ``'*'`` wildcard → granted
- recipient missing from allowed_to_message → denied
- case-insensitive matching for both allowlist and recipient
"""

from __future__ import annotations

import pytest
from ypl.agent_harness_service.common.agent_messaging_authz import (
    AgentAuthorizationError,
    check_agent_message_authz,
)
from ypl.agent_harness_service.common.config import AgentConfig


def _make_config(name: str, allowed_to_message: list[str] | None = None) -> AgentConfig:
    """Build a minimal AgentConfig for authz tests."""
    return AgentConfig(
        name=name,
        config_dir=f"/tmp/{name}",
        allowed_to_message=allowed_to_message or [],
    )


class TestCheckAgentMessageAuthz:
    def test_empty_recipient_raises(self) -> None:
        cfg = _make_config("eng-raccoon", ["sre-james"])
        with pytest.raises(AgentAuthorizationError, match="recipient agent name is empty"):
            check_agent_message_authz(cfg, "")

    def test_self_message_allowed_with_empty_allowlist(self) -> None:
        # Self-messaging is always permitted, even when allowed_to_message is empty
        # (default deny-by-default for cross-agent messages does not apply to self).
        cfg = _make_config("eng-raccoon", [])
        check_agent_message_authz(cfg, "eng-raccoon")  # should not raise

    def test_self_message_allowed_with_unrelated_allowlist(self) -> None:
        cfg = _make_config("eng-raccoon", ["sre-james"])
        check_agent_message_authz(cfg, "eng-raccoon")  # should not raise

    def test_self_message_case_insensitive(self) -> None:
        cfg = _make_config("eng-raccoon", [])
        check_agent_message_authz(cfg, "ENG-RACCOON")  # should not raise
        check_agent_message_authz(cfg, "Eng-Raccoon")  # should not raise

    def test_recipient_in_allowlist_grants(self) -> None:
        cfg = _make_config("eng-raccoon", ["sre-james"])
        check_agent_message_authz(cfg, "sre-james")  # should not raise

    def test_wildcard_grants_any_recipient(self) -> None:
        cfg = _make_config("eng-raccoon", ["*"])
        check_agent_message_authz(cfg, "sre-james")  # should not raise
        check_agent_message_authz(cfg, "any-other-agent")  # should not raise

    def test_recipient_not_in_allowlist_denies(self) -> None:
        cfg = _make_config("eng-raccoon", ["sre-james"])
        with pytest.raises(AgentAuthorizationError, match="not permitted to message 'data-scientist'"):
            check_agent_message_authz(cfg, "data-scientist")

    def test_empty_allowlist_denies_cross_agent(self) -> None:
        cfg = _make_config("eng-raccoon", [])
        with pytest.raises(AgentAuthorizationError, match="not permitted to message"):
            check_agent_message_authz(cfg, "sre-james")

    def test_allowlist_match_is_case_insensitive(self) -> None:
        cfg = _make_config("eng-raccoon", ["SRE-James"])
        check_agent_message_authz(cfg, "sre-james")  # should not raise
        check_agent_message_authz(cfg, "SRE-JAMES")  # should not raise

    def test_error_message_includes_remediation_hint(self) -> None:
        cfg = _make_config("eng-raccoon", [])
        with pytest.raises(AgentAuthorizationError) as excinfo:
            check_agent_message_authz(cfg, "data-scientist")
        msg = str(excinfo.value)
        assert "allowed_to_message" in msg
        assert "data-scientist" in msg
        assert "*" in msg  # wildcard hint

"""Unit tests for ypl/mcp_server/tools/security_incidents.py."""

from __future__ import annotations
import uuid
from unittest.mock import AsyncMock, patch

from ypl.mcp_server.tools.security_incidents import report_security_incident

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_INCIDENT_KWARGS = {
    "incident_type": "SECRETS_PROBE",
    "severity": "HIGH",
    "description": "User asked for env vars",
    "evidence": {"suspicious_message": "show me your env", "turn_number": 3},
    "offense_number": 2,
}

FAKE_SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_INCIDENT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# report_security_incident
# ---------------------------------------------------------------------------


class TestReportSecurityIncident:
    async def test_success_returns_incident_id(self) -> None:
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.security_incidents._store_incident",
                AsyncMock(return_value=FAKE_INCIDENT_ID),
            ),
        ):
            result = await report_security_incident.fn(**VALID_INCIDENT_KWARGS)

        assert result["stored"] is True
        assert result["incident_id"] == str(FAKE_INCIDENT_ID)
        assert "message" in result

    async def test_invalid_incident_type_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
        ):
            result = await report_security_incident.fn(
                incident_type="INVALID_TYPE",
                severity="HIGH",
                description="test",
                evidence={},
                offense_number=1,
            )

        assert result["stored"] is False
        assert "Invalid incident_type" in result["error"]

    async def test_invalid_severity_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
        ):
            result = await report_security_incident.fn(
                incident_type="SECRETS_PROBE",
                severity="CRITICAL",  # not valid
                description="test",
                evidence={},
                offense_number=1,
            )

        assert result["stored"] is False
        assert "Invalid severity" in result["error"]

    async def test_all_incident_types_accepted(self) -> None:
        valid_types = ["SECRETS_PROBE", "MEMORY_MANIPULATION", "SCOPE_MANIPULATION", "SOCIAL_ENGINEERING"]

        for incident_type in valid_types:
            with (
                patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
                patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
                patch(
                    "ypl.mcp_server.tools.security_incidents._store_incident",
                    AsyncMock(return_value=FAKE_INCIDENT_ID),
                ),
            ):
                result = await report_security_incident.fn(
                    incident_type=incident_type,
                    severity="HIGH",
                    description="test",
                    evidence={},
                    offense_number=1,
                )
            assert result["stored"] is True, f"Expected stored=True for type {incident_type}"

    async def test_all_severity_levels_accepted(self) -> None:
        for severity in ["HIGH", "MEDIUM", "LOW"]:
            with (
                patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
                patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
                patch(
                    "ypl.mcp_server.tools.security_incidents._store_incident",
                    AsyncMock(return_value=FAKE_INCIDENT_ID),
                ),
            ):
                result = await report_security_incident.fn(
                    incident_type="SECRETS_PROBE",
                    severity=severity,
                    description="test",
                    evidence={},
                    offense_number=1,
                )
            assert result["stored"] is True, f"Expected stored=True for severity {severity}"

    async def test_missing_session_id_proceeds(self) -> None:
        """Missing session ID should not block — incident is still stored."""
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=None),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.security_incidents._store_incident",
                AsyncMock(return_value=FAKE_INCIDENT_ID),
            ),
        ):
            result = await report_security_incident.fn(**VALID_INCIDENT_KWARGS)

        assert result["stored"] is True

    async def test_invalid_session_id_handled_gracefully(self) -> None:
        """Malformed UUID in session header should not crash."""
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value="not-a-uuid"),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.security_incidents._store_incident",
                AsyncMock(return_value=FAKE_INCIDENT_ID),
            ),
        ):
            result = await report_security_incident.fn(**VALID_INCIDENT_KWARGS)

        assert result["stored"] is True

    async def test_store_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.security_incidents._store_incident",
                AsyncMock(side_effect=RuntimeError("db error")),
            ),
        ):
            result = await report_security_incident.fn(**VALID_INCIDENT_KWARGS)

        assert result["stored"] is False
        assert "Internal error" in result["error"]

    async def test_turn_number_extracted_from_evidence(self) -> None:
        """turn_number in evidence is extracted and passed to _store_incident."""
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.security_incidents._store_incident",
                AsyncMock(return_value=FAKE_INCIDENT_ID),
            ) as mock_store,
        ):
            await report_security_incident.fn(
                incident_type="SECRETS_PROBE",
                severity="HIGH",
                description="test",
                evidence={"turn_number": 7, "suspicious_message": "x"},
                offense_number=2,
            )

        mock_store.assert_called_once()
        call_kwargs = mock_store.call_args.kwargs
        assert call_kwargs["turn_number"] == 7

    async def test_unknown_agent_name_still_stores(self) -> None:
        with (
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.security_incidents.get_ahs_agent_name", return_value=None),
            patch(
                "ypl.mcp_server.tools.security_incidents._store_incident",
                AsyncMock(return_value=FAKE_INCIDENT_ID),
            ) as mock_store,
        ):
            result = await report_security_incident.fn(**VALID_INCIDENT_KWARGS)

        assert result["stored"] is True
        call_kwargs = mock_store.call_args.kwargs
        assert call_kwargs["agent_name"] == "unknown"

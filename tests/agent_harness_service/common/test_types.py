from datetime import UTC, datetime
from decimal import Decimal

from ypl.agent_harness_service.common.types import MessageHistoryItem, SessionHistoryResponse


def test_session_history_cost_usd_serializes_as_float() -> None:
    response = SessionHistoryResponse(
        session_id="session-123",
        agent_id="test-agent",
        status="ACTIVE",
        messages=[
            MessageHistoryItem(
                message_id="message-123",
                turn_number=1,
                role="AGENT",
                content="done",
                cost_usd=Decimal("0.123456"),
                created_at=datetime(2026, 3, 20, tzinfo=UTC),
            )
        ],
    )

    payload = response.model_dump(mode="json")

    assert payload["messages"][0]["cost_usd"] == 0.123456
    assert isinstance(payload["messages"][0]["cost_usd"], float)

"""Abstract base for all AHS outgoing gateways.

Each gateway knows how to push replies, request feedback, and send
proactive messages through a specific transport (Slack, web/SSE, etc.).
Text is expected in the gateway's native format (e.g., Slack mrkdwn for Slack).
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class GatewaySendResult(BaseModel):
    """Result of a proactive send_message call."""

    success: bool
    message_id: str | None = None  # gateway-specific message ref
    destination: str | None = None
    error: str | None = None


class Gateway(ABC):
    """Abstract base for all AHS outgoing gateways."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this gateway (e.g., 'slack', 'web')."""
        ...

    @abstractmethod
    async def send_reply(
        self, session_id: str, text: str, reply_type: str | None = None, username: str | None = None
    ) -> bool:
        """Push a reply into an existing session.

        Text should be in the gateway's native format.
        *reply_type* is an optional content-type hint (e.g. 'thinking', 'tool_use')
        that the gateway can use to adjust rendering.
        *username* overrides the bot's display name for this message (requires
        ``chat:write.customize`` scope on Slack).
        """
        ...

    @abstractmethod
    async def append_reply(
        self, session_id: str, text: str, reply_type: str | None = None, username: str | None = None
    ) -> bool:
        """Append text to the last reply in an existing session (buffered).

        *reply_type* must match the type of the message being appended to.
        If it differs, the gateway should flush the buffer and start a new message.
        *username* overrides the bot's display name (passed through on new messages).
        """
        ...

    @abstractmethod
    async def request_feedback(self, session_id: str) -> bool:
        """Request user feedback for a session."""
        ...

    @abstractmethod
    async def send_message(
        self,
        agent_name: str,
        destination: str,
        text: str,
        thread_id: str | None = None,
        ahs_session_id: str | None = None,
        username: str | None = None,
    ) -> GatewaySendResult:
        """Proactive message (no existing session).

        Destination is gateway-specific (Slack channel, email, web user_id, etc.).
        *ahs_session_id* allows the gateway to register the new thread/conversation
        so that human replies route to the existing agent session.
        *username* overrides the bot's display name for this message.
        """
        ...

    async def send_status_update(self, session_id: str, text: str) -> bool:
        """Push a live status hint (e.g., tool-use progress) to the session.

        Optional — the default no-op implementation returns ``False`` so callers
        can fall back to ``send_reply`` for gateways that do not support inline
        status blocks.  Only the Slack gateway currently overrides this.

        Args:
            session_id: The gateway session identifier.
            text: Short status line, e.g. "🔧 5 tools used: Bash, Grep, Read".

        Returns:
            True if the update was accepted, False if unsupported or failed.
        """
        return False

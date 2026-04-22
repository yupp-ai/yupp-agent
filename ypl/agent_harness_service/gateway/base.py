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
        unfurl_links: bool = True,
        unfurl_media: bool = True,
    ) -> GatewaySendResult:
        """Proactive message (no existing session).

        Destination is gateway-specific (Slack channel, email, web user_id, etc.).
        *ahs_session_id* allows the gateway to register the new thread/conversation
        so that human replies route to the existing agent session.
        *username* overrides the bot's display name for this message.
        *unfurl_links* / *unfurl_media* control gateway-native link preview
        behaviour (Slack only, today) — both default True to match Slack's
        default for user messages.
        """
        ...

    async def send_status_update(self, session_id: str, text: str) -> bool:
        """Push a plain-text status hint to the session (no-op by default).

        No gateway currently overrides this — tool-use progress is delivered
        via ``send_tool_event`` instead.  Callers fall back to ``send_reply``
        when this returns ``False`` (e.g. for stop notices).

        Args:
            session_id: The gateway session identifier.
            text: Short status line to display.

        Returns:
            True if the update was accepted, False if unsupported or failed.
        """
        return False

    async def send_tool_event(
        self,
        session_id: str,
        kind: str,
        tool_use_id: str,
        *,
        name: str | None = None,
        command: str | None = None,
        result_status: str | None = None,
        error_msg: str | None = None,
        result_content: str | None = None,
    ) -> bool:
        """Push a structured tool-start or tool-result event to the session.

        Optional — the default no-op implementation returns ``False`` for
        gateways that do not support the structured tool-cluster display.
        Only the Slack gateway currently overrides this.

        Args:
            session_id: The gateway session identifier.
            kind: ``"start"`` (tool call begins) or ``"result"`` (result arrives).
            tool_use_id: Unique identifier that correlates start ↔ result events.
            name: Tool name (required for ``"start"`` events, e.g. "Bash").
            command: Formatted command string shown in the cluster (START only).
            result_status: ``"done"`` | ``"empty"`` | ``"failed"`` (RESULT only).
            error_msg: Short error text when result_status is ``"failed"``.
            result_content: First line of tool output (DONE only, max 150 chars).

        Returns:
            True if the event was accepted, False if unsupported or failed.
        """
        return False

    async def send_questionnaire(
        self,
        session_id: str,
        question_id: str,
        text: str,
        choices: list[dict[str, str]],
        allow_free_text: bool = True,
    ) -> bool:
        """Post a multiple-choice questionnaire to the session.

        Optional — the default no-op implementation returns ``False`` for
        gateways that do not support interactive buttons.  Only the Slack
        gateway currently overrides this.

        Args:
            session_id: The gateway session identifier.
            question_id: Caller-assigned ID for this question (alphanumeric + underscores).
            text: Question text to display to the user.
            choices: List of ``{"label": str, "value": str}`` dicts (max 5 for Slack).
            allow_free_text: If True, a hint is shown that the user can type a free answer.

        Returns:
            True if the questionnaire was posted, False if unsupported or failed.
        """
        return False

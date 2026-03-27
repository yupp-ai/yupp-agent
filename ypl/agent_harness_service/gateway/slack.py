"""Slack gateway — pushes replies and messages through the Slack Agent Gateway HTTP API.

Extracted from the original ``gateway_client.py``.  Text is passed through
as-is; the agent is responsible for producing the appropriate output format.
"""

import httpx

from ypl.agent_harness_service.gateway.base import Gateway, GatewaySendResult
from ypl.agent_harness_service.gateway.registry import GatewayConfig
from ypl.structured_logger import get_logger

logger = get_logger()

# Timeout for gateway calls (should be fast — just posting to Slack)
_GATEWAY_TIMEOUT = 10.0


class SlackGateway(Gateway):
    """Concrete gateway that routes through the Slack Agent Gateway HTTP API."""

    def __init__(self, config: GatewayConfig) -> None:
        self._name = config.name
        self._base_url: str = config.settings["base_url"]
        self._api_key: str | None = config.settings.get("api_key")
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return self._name

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_GATEWAY_TIMEOUT)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    # ------------------------------------------------------------------
    # send_reply
    # ------------------------------------------------------------------

    async def send_reply(
        self, session_id: str, text: str, reply_type: str | None = None, username: str | None = None
    ) -> bool:
        """Push a reply into an existing Slack thread."""
        url = f"{self._base_url}/slack-agent-gateway/sessions/reply"

        logger.info(
            "Calling gateway /sessions/reply",
            session_id=session_id,
            url=url,
            text_length=len(text),
            reply_type=reply_type,
        )

        payload: dict[str, str | None] = {"session_id": session_id, "text": text}
        if reply_type is not None:
            payload["reply_type"] = reply_type
        if username is not None:
            payload["username"] = username

        try:
            resp = await self._get_client().post(
                url,
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                logger.error("Gateway rejected reply", session_id=session_id, error=data.get("error"))
                return False
            logger.info("Gateway accepted reply", session_id=session_id, message_ts=data.get("message_ts"))
            return True
        except httpx.TimeoutException:
            logger.error("Timeout calling gateway /sessions/reply", session_id=session_id)
            return False
        except httpx.HTTPStatusError as e:
            logger.error(
                "HTTP error calling gateway /sessions/reply",
                session_id=session_id,
                status_code=e.response.status_code,
            )
            return False
        except Exception:
            logger.error("Error calling gateway /sessions/reply", session_id=session_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # append_reply
    # ------------------------------------------------------------------

    async def append_reply(
        self, session_id: str, text: str, reply_type: str | None = None, username: str | None = None
    ) -> bool:
        """Append text to the last reply in a Slack thread (buffered)."""
        url = f"{self._base_url}/slack-agent-gateway/sessions/reply/append"

        logger.info(
            "Calling gateway /sessions/reply/append",
            session_id=session_id,
            url=url,
            text_length=len(text),
            reply_type=reply_type,
        )

        payload: dict[str, str | None] = {"session_id": session_id, "text": text}
        if reply_type is not None:
            payload["reply_type"] = reply_type
        if username is not None:
            payload["username"] = username

        try:
            resp = await self._get_client().post(
                url,
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                logger.error("Gateway rejected append", session_id=session_id, error=data.get("error"))
                return False
            logger.info("Gateway accepted append", session_id=session_id, buffered=data.get("buffered"))
            return True
        except httpx.TimeoutException:
            logger.error("Timeout calling gateway /sessions/reply/append", session_id=session_id)
            return False
        except httpx.HTTPStatusError as e:
            logger.error(
                "HTTP error calling gateway /sessions/reply/append",
                session_id=session_id,
                status_code=e.response.status_code,
            )
            return False
        except Exception:
            logger.error("Error calling gateway /sessions/reply/append", session_id=session_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # request_feedback
    # ------------------------------------------------------------------

    async def request_feedback(self, session_id: str) -> bool:
        """Ask the gateway to post a feedback survey to the Slack thread."""
        url = f"{self._base_url}/slack-agent-gateway/sessions/request-feedback"

        logger.info("Calling gateway /sessions/request-feedback", session_id=session_id, url=url)

        try:
            resp = await self._get_client().post(
                url,
                json={"session_id": session_id},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                logger.error("Gateway rejected feedback request", session_id=session_id, error=data.get("error"))
                return False
            logger.info("Gateway accepted feedback request", session_id=session_id, message_ts=data.get("message_ts"))
            return True
        except httpx.TimeoutException:
            logger.error("Timeout calling gateway /sessions/request-feedback", session_id=session_id)
            return False
        except httpx.HTTPStatusError as e:
            logger.error(
                "HTTP error calling gateway /sessions/request-feedback",
                session_id=session_id,
                status_code=e.response.status_code,
            )
            return False
        except Exception:
            logger.error("Error calling gateway /sessions/request-feedback", session_id=session_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # send_message (proactive — no existing session)
    # ------------------------------------------------------------------

    async def send_message(
        self,
        agent_name: str,
        destination: str,
        text: str,
        thread_id: str | None = None,
        ahs_session_id: str | None = None,
        username: str | None = None,
    ) -> GatewaySendResult:
        """Send a proactive message to a Slack channel.

        *destination* is the Slack channel name or ID.
        *thread_id* maps to Slack's ``thread_ts``.
        *ahs_session_id* is the AHS session UUID — when provided for a top-level
        message, SAG registers the new Slack thread so human replies route to the
        existing agent session.
        """
        url = f"{self._base_url}/slack-agent-gateway/messages/send"

        payload: dict[str, str] = {
            "agent_name": agent_name,
            "channel": destination,
            "text": text,
        }
        if thread_id:
            payload["thread_ts"] = thread_id
        if ahs_session_id:
            payload["ahs_session_id"] = ahs_session_id
        if username:
            payload["username"] = username

        logger.info(
            "Calling gateway /messages/send",
            agent_name=agent_name,
            channel=destination,
            url=url,
            text_length=len(text),
            in_thread=thread_id is not None,
        )

        try:
            resp = await self._get_client().post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                logger.error(
                    "Gateway rejected send_message",
                    agent_name=agent_name,
                    channel=destination,
                    error=data.get("error"),
                )
                return GatewaySendResult(
                    success=False,
                    error=data.get("error", "Unknown error"),
                    destination=destination,
                )

            logger.info(
                "Gateway accepted send_message",
                agent_name=agent_name,
                channel=destination,
                message_ts=data.get("message_ts"),
            )
            return GatewaySendResult(
                success=True,
                message_id=data.get("message_ts"),
                destination=data.get("channel") or destination,
            )

        except httpx.TimeoutException:
            logger.error("Timeout calling gateway /messages/send", agent_name=agent_name, channel=destination)
            return GatewaySendResult(success=False, error="Timeout", destination=destination)
        except httpx.HTTPStatusError as e:
            logger.error(
                "HTTP error calling gateway /messages/send",
                agent_name=agent_name,
                channel=destination,
                status_code=e.response.status_code,
            )
            return GatewaySendResult(
                success=False,
                error=f"HTTP {e.response.status_code}",
                destination=destination,
            )
        except Exception:
            logger.error(
                "Error calling gateway /messages/send",
                agent_name=agent_name,
                channel=destination,
                exc_info=True,
            )
            return GatewaySendResult(success=False, error="Unexpected error", destination=destination)

    # ------------------------------------------------------------------
    # send_status_update (live tool-use hints — Slack-only)
    # ------------------------------------------------------------------

    async def send_status_update(self, session_id: str, text: str) -> bool:
        """Push a live status hint to SAG, which renders it as a muted context block."""
        url = f"{self._base_url}/slack-agent-gateway/sessions/status"

        logger.debug(
            "Calling gateway /sessions/status",
            session_id=session_id,
            url=url,
            text_length=len(text),
        )

        try:
            resp = await self._get_client().post(
                url,
                json={"session_id": session_id, "text": text},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                logger.warning(
                    "Gateway rejected status update",
                    session_id=session_id,
                    error=data.get("error"),
                )
                return False
            return True
        except httpx.TimeoutException:
            logger.warning("Timeout calling gateway /sessions/status", session_id=session_id)
            return False
        except httpx.HTTPStatusError as e:
            logger.warning(
                "HTTP error calling gateway /sessions/status",
                session_id=session_id,
                status_code=e.response.status_code,
            )
            return False
        except Exception:
            logger.warning("Error calling gateway /sessions/status", session_id=session_id, exc_info=True)
            return False

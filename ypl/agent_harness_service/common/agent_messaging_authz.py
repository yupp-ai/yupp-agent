"""Agent-to-agent (A2A) messaging authorization utilities.

This module provides authorization utilities for the deny-by-default A2A
messaging model.  Authorization is evaluated at send time: the sending
agent's ``allowed_to_message`` config field must explicitly list the
recipient agent's name (or contain ``'*'`` for unrestricted access).

Lives in ``common/`` (Layer 0) so it can be imported by both ``tools/``
and ``service/`` without introducing cross-layer dependencies.
"""

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.structured_logger import get_logger

logger = get_logger()


class AgentAuthorizationError(Exception):
    """Raised when an agent is not permitted to message another agent.

    This is a domain-specific exception (not ``PermissionError`` / ``OSError``)
    so it can be caught precisely at the API boundary and translated to HTTP 403
    without being accidentally swallowed by ``OSError`` handlers.
    """


def check_agent_message_authz(from_config: AgentConfig, to_agent_name: str) -> None:
    """Verify that the agent described by *from_config* is authorized to message *to_agent_name*.

    Authorization is deny-by-default for *cross-agent* messaging: the sending
    agent's ``allowed_to_message`` list must explicitly include the recipient's
    name, or contain ``'*'`` for unrestricted outbound messaging.  Comparison
    is case-insensitive to avoid silent bypass or unexpected denial from
    casing inconsistencies.

    **Self-messaging is always permitted, for every agent, with no config
    required.**  An agent dispatching a message to a fresh session of itself
    is a legitimate worker pattern, not an A2A escalation: the agent already
    has full authority over its own work.  Loop-prevention is the
    responsibility of higher-level dispatch logic (turn budgets, session
    limits), not authz.

    Accepts both filesystem-loaded and DB-loaded ``AgentConfig`` objects uniformly
    — the check does not require a DB session or an ``Agent`` ORM object.

    Args:
        from_config: The ``AgentConfig`` of the agent attempting to send a message.
        to_agent_name: The name of the intended recipient agent.

    Raises:
        AgentAuthorizationError: If the sending agent is not permitted to message
            the recipient according to its ``allowed_to_message`` config.

    Example::

        # Cross-agent: raises unless from_config.allowed_to_message lists "agent-b"
        # (or contains "*").
        check_agent_message_authz(agent_a_config, "agent-b")

        # Self-message: always allowed, regardless of allowed_to_message.
        check_agent_message_authz(agent_a_config, "agent-a")
    """
    if not to_agent_name:
        raise AgentAuthorizationError("Cannot authorize A2A message: recipient agent name is empty or None")

    to_lower = to_agent_name.lower()

    # Self-messaging is always permitted — see docstring.
    if from_config.name.lower() == to_lower:
        logger.info(
            "A2A authorization granted (self-message)",
            from_agent=from_config.name,
            to_agent=to_agent_name,
        )
        return

    allowed: list[str] = from_config.allowed_to_message
    allowed_lower = [a.lower() for a in allowed]

    if to_lower not in allowed_lower and "*" not in allowed_lower:
        logger.warning(
            "A2A authorization denied",
            from_agent=from_config.name,
            to_agent=to_agent_name,
            allowed_to_message=allowed,
        )
        raise AgentAuthorizationError(
            f"Agent '{from_config.name}' is not permitted to message '{to_agent_name}'. "
            f"Add '{to_agent_name}' to its allowed_to_message config, or use '*' for unrestricted access."
        )

    logger.info(
        "A2A authorization granted",
        from_agent=from_config.name,
        to_agent=to_agent_name,
    )

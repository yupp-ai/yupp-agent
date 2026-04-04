"""Agent-to-agent (A2A) messaging helpers.

This module provides authorization utilities for the deny-by-default A2A
messaging model.  Authorization is evaluated at send time: the sending
agent's ``allowed_to_message`` config field must explicitly list the
recipient agent's name (or contain ``'*'`` for unrestricted access).
"""

from ypl.db.agent_harness import Agent


def check_agent_message_authz(from_agent: Agent, to_agent: Agent) -> None:
    """Verify that *from_agent* is authorized to message *to_agent*.

    Authorization is deny-by-default.  The sending agent's config must
    include the recipient's name in its ``allowed_to_message`` list, or
    include ``'*'`` to grant unrestricted outbound messaging.

    Args:
        from_agent: The agent attempting to send a message.
        to_agent: The intended recipient agent.

    Raises:
        PermissionError: If *from_agent* is not permitted to message
            *to_agent* according to its ``allowed_to_message`` config.

    Example::

        # Raises PermissionError if eng-raccoon's config doesn't list sre-james
        check_agent_message_authz(eng_raccoon_agent, sre_james_agent)
    """
    config: dict = from_agent.config or {}
    allowed: list[str] = config.get("allowed_to_message", [])
    if to_agent.name not in allowed and "*" not in allowed:
        raise PermissionError(f"Agent '{from_agent.name}' is not permitted to message '{to_agent.name}'")

"""Agent-to-agent (A2A) messaging helpers.

This module provides authorization utilities for the deny-by-default A2A
messaging model.  Authorization is evaluated at send time: the sending
agent's ``allowed_to_message`` config field must explicitly list the
recipient agent's name (or contain ``'*'`` for unrestricted access).

.. note::
    The canonical implementations live in
    ``ypl.agent_harness_service.common.agent_messaging_authz`` (Layer 0).
    This module re-exports them for backward compatibility so existing
    ``service/`` and ``service/__init__`` callers continue to work unchanged.
"""

# Re-exported from common/ — new code should import from
# ypl.agent_harness_service.common.agent_messaging_authz directly.
from ypl.agent_harness_service.common.agent_messaging_authz import (
    AgentAuthorizationError,
    check_agent_message_authz,
)

__all__ = ["AgentAuthorizationError", "check_agent_message_authz"]

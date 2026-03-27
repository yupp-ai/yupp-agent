"""Helper functions for mapping Yuppster identities.

Provides utilities for mapping between different Yuppster identity systems
(GitHub, Linear, Slack, email, user_id).
"""

from ypl.backend.llm.constants import (
    GITHUB_TO_LINEAR_NAME,
    LINEAR_TO_SLACK_ID,
    SLACK_ID_TO_EMAIL,
)
from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email
from ypl.structured_logger import get_logger

logger = get_logger()


async def github_username_to_yupp_user_id(github_username: str) -> str | None:
    """Map GitHub username to Yupp user_id via mapping chain.

    Chain: GitHub username -> Linear name -> Slack ID -> email -> user_id

    Args:
        github_username: GitHub login/username (e.g., "AmaxGuan")

    Returns:
        Yupp user_id if mapping exists and user found, None otherwise.
    """
    linear_name = GITHUB_TO_LINEAR_NAME.get(github_username)
    if not linear_name:
        logger.debug("GitHub user not in GITHUB_TO_LINEAR_NAME", github_username=github_username)
        return None

    slack_id = LINEAR_TO_SLACK_ID.get(linear_name)
    if not slack_id:
        logger.debug("Linear name not in LINEAR_TO_SLACK_ID", linear_name=linear_name)
        return None

    email = SLACK_ID_TO_EMAIL.get(slack_id)
    if not email:
        logger.debug("Slack ID not in SLACK_ID_TO_EMAIL", slack_id=slack_id)
        return None

    user_id, error = await resolve_user_id_from_email(email)
    if error:
        logger.debug("Could not resolve email to user_id", email=email, error=error)
        return None

    return user_id


async def slack_id_to_yupp_user_id(slack_id: str) -> str | None:
    """Map Slack user ID to Yupp user_id via email.

    Chain: Slack ID -> email -> user_id

    Args:
        slack_id: Slack user ID (e.g., "U08PM4JMQHW")

    Returns:
        Yupp user_id if mapping exists and user found, None otherwise.
    """
    email = SLACK_ID_TO_EMAIL.get(slack_id)
    if not email:
        logger.debug("Slack ID not in SLACK_ID_TO_EMAIL", slack_id=slack_id)
        return None

    user_id, error = await resolve_user_id_from_email(email)
    if error:
        logger.debug("Could not resolve email to user_id", email=email, error=error)
        return None

    return user_id

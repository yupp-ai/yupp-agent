"""Stub email sender for yupp-agent.

Only the signature used by MCP tasks is provided; actual delivery (templates,
campaigns, provider wiring) is not wired up yet and calls are logged only.
"""

import uuid

from ypl.backend.email.email_types import EmailConfig
from ypl.structured_logger import get_logger

logger = get_logger()


async def send_email_async(email_config: EmailConfig, print_only: bool = False) -> uuid.UUID | None:
    """Stub — logs instead of sending."""
    logger.info(
        "send_email_async stub called",
        to=email_config.to_address,
        campaign=email_config.campaign,
    )
    return None

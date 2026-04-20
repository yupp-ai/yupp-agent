from dataclasses import dataclass, field
from typing import Any

from ypl.backend.config import settings


@dataclass
class EmailConfig:
    campaign: str
    to_address: str
    template_params: dict[str, Any]
    # Resolved lazily at instantiation so tests/self-hosted deployments can
    # override ``settings.TEAM_FROM_EMAIL_ADDRESS`` without importing a frozen
    # module-level constant.
    from_address: str = field(default_factory=lambda: settings.TEAM_FROM_EMAIL_ADDRESS)


@dataclass
class EmailContent:
    subject: str
    preview: str | None
    body_html: str

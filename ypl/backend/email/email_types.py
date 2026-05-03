from dataclasses import dataclass
from typing import Any


@dataclass
class EmailConfig:
    campaign: str
    to_address: str
    template_params: dict[str, Any]


@dataclass
class EmailContent:
    subject: str
    preview: str | None
    body_html: str

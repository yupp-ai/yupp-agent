from dataclasses import dataclass
from typing import Any, Literal

FromEmailAddressType = Literal[
    "Yupp Team <team@updates.yupp.ai>",
    "Support <support@updates.yupp.ai>",
    "Notice <notice@updates.yupp.ai>",
    "Onboarding <onboarding@updates.yupp.ai>",
    "Payments <payments@updates.yupp.ai>",
]

TEAM_FROM_EMAIL_ADDRESS: FromEmailAddressType = "Yupp Team <team@updates.yupp.ai>"
SUPPORT_FROM_EMAIL_ADDRESS: FromEmailAddressType = "Support <support@updates.yupp.ai>"
NOTICE_FROM_EMAIL_ADDRESS: FromEmailAddressType = "Notice <notice@updates.yupp.ai>"
ONBOARDING_FROM_EMAIL_ADDRESS: FromEmailAddressType = "Onboarding <onboarding@updates.yupp.ai>"
PAYMENTS_FROM_EMAIL_ADDRESS: FromEmailAddressType = "Payments <payments@updates.yupp.ai>"


@dataclass
class EmailConfig:
    campaign: str
    to_address: str
    template_params: dict[str, Any]
    from_address: FromEmailAddressType = TEAM_FROM_EMAIL_ADDRESS


@dataclass
class EmailContent:
    subject: str
    preview: str | None
    body_html: str

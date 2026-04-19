from enum import Enum
from typing import NamedTuple


# TODO(Tian): retire ChatProvider class and use something else that doesn't depend on hard-coded values.
class ChatProvider(Enum):
    OPENAI = 1
    ANTHROPIC = 2
    GOOGLE = 3
    MISTRAL = 4
    META = 5
    MICROSOFT = 6
    ZERO_ONE = 7
    DEEPSEEK = 8
    NVIDIA = 9
    QWEN = 10
    HERMES = 11
    TOGETHER = 12
    ANYSCALE = 13
    HUGGINGFACE = 14
    AI21 = 15
    MINIMAX = 16
    REVE = 17

    @classmethod
    def from_string(cls, provider: str) -> "ChatProvider":
        try:
            return cls[provider.upper()]
        except KeyError as e:
            raise ValueError(f"Unsupported provider string: {provider}") from e


# ------------------------ model filter constants ------------------------
MF_TEXT = "TEXT"
MF_IMAGE_GENERATION = "IMAGE_GENERATION"
MF_IMAGE = "IMAGE"
MF_PDF = "PDF"
MF_LIVE = "LIVE"
MF_SVG = "SVG"  # just for passing the code/SVG needs to categories
MF_HTML = "HTML"  # just for passing the code/HTML needs to categories
MF_AGENTIC_CODING = "AGENTIC_CODING"

ALL_MODEL_FILTERS = [
    MF_TEXT,
    MF_IMAGE_GENERATION,
    MF_IMAGE,
    MF_PDF,
    MF_LIVE,
    MF_SVG,
    MF_HTML,
    MF_AGENTIC_CODING,
]

# ------------------------ prompt categories ------------------------

# Attachment categories, sent by FE
IMAGE_CATEGORY = "image"
PDF_CATEGORY = "pdf"

# Online/offline categories, detected
ONLINE_CATEGORY = "online"
OFFLINE_CATEGORY = "offline"

# Image/SVG/Coding intent categories
IMAGE_GEN_CATEGORY = "Image Generation"
IMAGE_EDIT_CATEGORY = "Image Edit"
IMAGE_GEN_STOP_CATEGORY = "Image Generation Stop"
SVG_GEN_CATEGORY = "SVG Generation"
SVG_EDIT_CATEGORY = "SVG Edit"
HTML_GEN_CATEGORY = "HTML Generation"
HTML_EDIT_CATEGORY = "HTML Edit"

# other categories
EMPTY_CATEGORY = "Empty"

# safety categories
SAFE_CATEGORY = "safe"
UNSAFE_CATEGORY = "unsafe"
AZURE_UNSAFE_CATEGORY = "azure_unsafe"
MS_CONTENT_SAFETY_SEVERE_CATEGORY = "ms_content_safety_severe"
MS_CONTENT_SAFETY_SEVERE_CATEGORY_ERROR = "ms_content_safety_severe_error"

ALL_CAPABILITY_CATEGORIES = [
    IMAGE_CATEGORY,
    PDF_CATEGORY,
    ONLINE_CATEGORY,
    OFFLINE_CATEGORY,
    IMAGE_GEN_CATEGORY,
    IMAGE_EDIT_CATEGORY,
    IMAGE_GEN_STOP_CATEGORY,
    SVG_GEN_CATEGORY,
    SVG_EDIT_CATEGORY,
    HTML_GEN_CATEGORY,
    HTML_EDIT_CATEGORY,
]

ALL_OTHER_CATEGORIES = [
    EMPTY_CATEGORY,
    SAFE_CATEGORY,
    UNSAFE_CATEGORY,
    AZURE_UNSAFE_CATEGORY,
    MS_CONTENT_SAFETY_SEVERE_CATEGORY,
    MS_CONTENT_SAFETY_SEVERE_CATEGORY_ERROR,
]

# Topic categories, detected
CODING_CATEGORY = "coding"
MATH_CATEGORY = "math"
REASONING_CATEGORY = "reasoning"
INFORMATIONAL_CATEGORY = "informational"
CREATIVE_CATEGORY = "creative"
TASK_CATEGORY = "task"
OTHER_CATEGORY = "other"
OVERALL_CATEGORY = "Overall"

ORDERED_PROMPT_CATEGORIES = [
    CODING_CATEGORY,
    MATH_CATEGORY,
    REASONING_CATEGORY,
    INFORMATIONAL_CATEGORY,
    CREATIVE_CATEGORY,
    TASK_CATEGORY,
    OTHER_CATEGORY,
]


def is_image_gen_or_edit_category(category: str) -> bool:
    return category in (IMAGE_GEN_CATEGORY, IMAGE_EDIT_CATEGORY)


def has_image_gen_or_edit_category(categories: list[str]) -> bool:
    return any(is_image_gen_or_edit_category(c) for c in categories)


def is_svg_gen_or_edit_category(category: str) -> bool:
    return category in (SVG_GEN_CATEGORY, SVG_EDIT_CATEGORY)


def has_svg_gen_or_edit_category(categories: list[str]) -> bool:
    return any(is_svg_gen_or_edit_category(c) for c in categories)


def is_html_gen_or_edit_category(category: str) -> bool:
    return category in (HTML_GEN_CATEGORY, HTML_EDIT_CATEGORY)


def has_html_gen_or_edit_category(categories: list[str]) -> bool:
    return any(is_html_gen_or_edit_category(c) for c in categories)


# ---------------------------------------------------------------------------
# Consolidated team directory – single source of truth for identity mappings.
# source: https://github.com/yupp-ai/yupp-head/blob/main/apps/web/lib/utils/user-utils.ts
# ---------------------------------------------------------------------------


class TeamMember(NamedTuple):
    email: str | None
    slack_id: str
    linear_name: str | None
    github_login: str | None


TEAM_DIRECTORY: tuple[TeamMember, ...] = (
    TeamMember("amadeus.guan@gmail.com", "U0ASXC300G2", "lguan", "AmaxGuan"),
    TeamMember("wangtianthu@gmail.com", "U0ATRKR7FR6", "tian", "wangtian24"),
)

# --- Derived lookup dicts (all generated from TEAM_DIRECTORY) ---------------

LINEAR_TO_SLACK_ID: dict[str, str] = {m.linear_name: m.slack_id for m in TEAM_DIRECTORY if m.linear_name}

GITHUB_TO_LINEAR_NAME: dict[str, str] = {
    m.github_login: m.linear_name for m in TEAM_DIRECTORY if m.github_login and m.linear_name
}

EMAIL_TO_SLACK_ID: dict[str, str] = {m.email: m.slack_id for m in TEAM_DIRECTORY if m.email}

SLACK_ID_TO_EMAIL: dict[str, str] = {m.slack_id: m.email for m in TEAM_DIRECTORY if m.email}

SLACK_ID_TO_LINEAR_NAME: dict[str, str] = {m.slack_id: m.linear_name for m in TEAM_DIRECTORY if m.linear_name}

EMAIL_TO_LINEAR_NAME: dict[str, str] = {m.email: m.linear_name for m in TEAM_DIRECTORY if m.email and m.linear_name}

LINEAR_TO_EMAIL: dict[str, str] = {m.linear_name: m.email for m in TEAM_DIRECTORY if m.linear_name and m.email}

GITHUB_TO_SLACK_ID: dict[str, str] = {m.github_login: m.slack_id for m in TEAM_DIRECTORY if m.github_login}

GITHUB_TO_EMAIL: dict[str, str] = {m.github_login: m.email for m in TEAM_DIRECTORY if m.github_login and m.email}

LINEAR_TO_GITHUB: dict[str, str] = {
    m.linear_name: m.github_login for m in TEAM_DIRECTORY if m.linear_name and m.github_login
}

SLACK_ID_TO_GITHUB: dict[str, str] = {m.slack_id: m.github_login for m in TEAM_DIRECTORY if m.github_login}

EMAIL_TO_GITHUB: dict[str, str] = {m.email: m.github_login for m in TEAM_DIRECTORY if m.email and m.github_login}

NONE_TAG_NAME = "None"

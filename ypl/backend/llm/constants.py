from enum import Enum


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


NONE_TAG_NAME = "None"

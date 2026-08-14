"""Provider registry and model string parsing.

Models are specified as '{provider}/{model_id}' (e.g., 'anthropic/claude-sonnet-4-6').
"""

from pydantic import BaseModel

from ypl.agent_harness_service.common.constants import (
    DEFAULT_MODEL_ANTHROPIC,
    DEFAULT_MODEL_CEREBRAS,
    DEFAULT_MODEL_DEEPSEEK,
    DEFAULT_MODEL_MINIMAX,
    DEFAULT_MODEL_MOONSHOT,
    DEFAULT_MODEL_OPENAI,
    DEFAULT_MODEL_TOGETHER,
    DEFAULT_MODEL_ZAI,
    PROVIDER_ANTHROPIC,
    PROVIDER_CEREBRAS,
    PROVIDER_DEEPSEEK,
    PROVIDER_MINIMAX,
    PROVIDER_MOONSHOT,
    PROVIDER_OPENAI,
    PROVIDER_TOGETHER,
    PROVIDER_ZAI,
)


class ProviderConfig(BaseModel):
    """Configuration for a model provider."""

    api_base: str
    env_key: str
    sdk: str
    default_model: str


PROVIDERS: dict[str, ProviderConfig] = {
    PROVIDER_ANTHROPIC: ProviderConfig(
        api_base="https://api.anthropic.com",
        env_key="ANTHROPIC_API_KEY",
        sdk="anthropic",
        default_model=DEFAULT_MODEL_ANTHROPIC,
    ),
    PROVIDER_OPENAI: ProviderConfig(
        api_base="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_OPENAI,
    ),
    PROVIDER_ZAI: ProviderConfig(
        api_base="https://api.z.ai/api/paas/v4",
        env_key="ZAI_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_ZAI,
    ),
    PROVIDER_MINIMAX: ProviderConfig(
        api_base="https://api.minimax.io/v1",
        env_key="MINIMAX_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_MINIMAX,
    ),
    PROVIDER_MOONSHOT: ProviderConfig(
        api_base="https://api.moonshot.ai/v1",
        env_key="MOONSHOT_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_MOONSHOT,
    ),
    PROVIDER_CEREBRAS: ProviderConfig(
        api_base="https://api.cerebras.ai/v1",
        env_key="CEREBRAS_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_CEREBRAS,
    ),
    PROVIDER_DEEPSEEK: ProviderConfig(
        api_base="https://api.deepseek.com/v1",
        env_key="DEEPSEEK_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_DEEPSEEK,
    ),
    PROVIDER_TOGETHER: ProviderConfig(
        api_base="https://api.together.ai/v1",
        env_key="TOGETHER_AI_API_KEY",
        sdk="openai",
        default_model=DEFAULT_MODEL_TOGETHER,
    ),
}

# All known models with provider prefix for route_model diversity guarantees
KNOWN_MODELS: list[str] = [
    "anthropic/claude-opus-4-6",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4o",
    "openai/o3",
    "openai/gpt-4o-mini",
    "zai/glm-5",
    "minimax/MiniMax-M2.5",
    "minimax/MiniMax-M2.1",
    "moonshot/kimi-k2.5",
    "cerebras/gpt-oss-120b",
    "cerebras/qwen-3-235b-a22b-instruct-2507",
    "cerebras/zai-glm-4.7",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    # NOTE: deepseek-reasoner (R1) is intentionally NOT exposed here. The raw executor
    # is a tool-calling loop that always sends tool schemas and echoes reasoning_content
    # back on subsequent turns; R1 rejects both (no function calling; HTTP 400 on
    # reasoning_content echo-back). Pricing/context metadata is kept in raw_executor.py
    # so it can be enabled once per-model capability flags exist.
    #
    # Together — an aggregator, so its own model IDs are '{org}/{model}' and the full
    # registry string carries two slashes ('together/zai-org/GLM-5.2'). This is fine:
    # parse_model_string() splits on the first slash only, and the pricing / context
    # tables in raw_executor.py are keyed by the org-qualified model_id, which can
    # never collide with a direct provider's bare model ID.
    "together/deepseek-ai/DeepSeek-V4-Flash-0731",
    "together/moonshotai/Kimi-K3",
    "together/zai-org/GLM-5.2",
    # NOTE: Qwen3.8 is in Together's catalog but its serverless endpoint currently
    # answers 503 "no available server" — a 2.4T model with no spare capacity. It is
    # listed here so it works the moment capacity returns; until then a turn on it
    # fails outright, because a 503 is not one of the rate-limit phrases the fallback
    # chain matches on (see is_rate_limit_failure in common/model_options.py).
    "together/Qwen/Qwen3.8-2.4T-A95B",
    "together/meta-models/Muse-Glimmer-30B",
    "together/thinkingmachines/Inkling",
]


def parse_model_string(model: str) -> tuple[str, str]:
    """Parse '{provider}/{model_id}' into (provider, model_id).

    Args:
        model: Model string (e.g., 'anthropic/claude-sonnet-4-6').

    Returns:
        Tuple of (provider, model_id).

    Raises:
        ValueError: If format is invalid or provider is unknown.
    """
    if "/" not in model:
        raise ValueError(
            f"Invalid model format: {model!r}. Expected '{{provider}}/{{model_id}}' "
            f"(e.g., 'anthropic/claude-sonnet-4-6')."
        )
    provider, model_id = model.split("/", 1)
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r}. Known providers: {list(PROVIDERS.keys())}")
    return provider, model_id


def resolve_model(
    param_model: str | None,
    agent_model: str | None,
    parent_model: str | None,
    default_model: str | None = None,
) -> str:
    """Resolve model using the precedence chain.

    Resolution order:
      1. param_model (from new_task call)
      2. agent_model (from agent config)
      3. parent_model (inherited from parent)
      4. default_model (executor-type default)

    Args:
        param_model: Model override from new_task parameter.
        agent_model: Default model from agent config.
        parent_model: Parent agent's model.
        default_model: Fallback default (e.g., per executor type).

    Returns:
        Resolved model string.

    Raises:
        ValueError: If no model can be resolved from any source.
    """
    model = param_model or agent_model or parent_model or default_model
    if not model:
        raise ValueError("No model specified: provide model in new_task call, agent config, or parent agent.")
    # Validate format
    parse_model_string(model)
    return model


def get_provider_config(provider: str) -> ProviderConfig:
    """Get provider configuration by ID.

    Raises:
        ValueError: If provider is unknown.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r}. Known: {list(PROVIDERS.keys())}")
    return PROVIDERS[provider]


def is_openai_compatible(provider: str) -> bool:
    """Check if a provider uses the OpenAI-compatible API (sdk='openai')."""
    return get_provider_config(provider).sdk == "openai"

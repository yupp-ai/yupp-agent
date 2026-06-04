"""Selectable model options and the global rate-limit fallback chain.

This module is the single source of truth for the set of models a user can pick
for an agent (in Streamlit) and for the ordered fallback chain used when a
harnessed (subscription) turn hits a rate / usage limit.

An *option* is one of three shapes, encoded as a canonical string:

* ``"harnessed:claude-code-cli"`` — a harness with NO explicit model; the CLI
  picks its own default model. Display: ``[HARNESSED] claude-code-cli (default)``.
* ``"harnessed:claude-code-cli:anthropic/claude-opus-4-6"`` — a harness with an
  explicit LLM model. The harness and the model are independent choices (the same
  Anthropic model can run under claude-code-cli or claude-agent-sdk).
  Display: ``[HARNESSED] claude-code-cli · anthropic/claude-opus-4-6``.
* ``"raw:deepseek/deepseek-v4-pro"`` — the raw executor calling a provider API
  directly (pay-per-use, no subscription rate limit). Display: ``[RAW] deepseek/deepseek-v4-pro``.

Layer 0 (common/) constraint: this module must not import other AHS packages.
The Redis-backed getter/setter import ``ypl.backend.feature_flags`` *lazily*
(inside the functions) to keep module import cheap and the architecture test clean.
"""

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel

from ypl.agent_harness_service.common.constants import (
    EXECUTOR_TYPE_HARNESSED,
    EXECUTOR_TYPE_RAW,
    HARNESS_CLAUDE_CODE_CLI,
    HARNESS_CLAUDE_SDK,
    HARNESS_CODEX_APP_SERVER,
    HARNESS_CODEX_CLI,
    HARNESSED_MODELS,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
)
from ypl.agent_harness_service.common.providers import KNOWN_MODELS, parse_model_string

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# Feature-flag key under which the runtime fallback-chain override is stored in Redis.
FALLBACK_CHAIN_FLAG = "agent_fallback_chain"

# Which provider's models each CLI harness can run. The Claude harnesses
# (claude-code-cli / claude-agent-sdk) run Anthropic models; the Codex harnesses
# (codex-cli / codex-app-server) run OpenAI models. Drives the explicit
# "[HARNESSED] {harness} · {provider}/{model}" options.
HARNESS_PROVIDER_MAP: dict[str, str] = {
    HARNESS_CLAUDE_CODE_CLI: PROVIDER_ANTHROPIC,
    HARNESS_CLAUDE_SDK: PROVIDER_ANTHROPIC,
    HARNESS_CODEX_CLI: PROVIDER_OPENAI,
    HARNESS_CODEX_APP_SERVER: PROVIDER_OPENAI,
}

# Default global fallback chain (hardcoded; editable at runtime via Streamlit).
# Claude CLI (subscription, default model) -> Codex CLI (subscription, default
# model) -> raw DeepSeek direct API as the always-available no-limit last resort.
DEFAULT_FALLBACK_CHAIN: list[str] = [
    "harnessed:claude-code-cli",
    "harnessed:codex-cli",
    "raw:deepseek/deepseek-v4-pro",
]


class ModelOption(BaseModel, frozen=True):
    """A single selectable model option.

    For a harness-default option, ``provider``/``model_id`` are None and
    ``harness`` is set. For an explicit harnessed option, all three are set.
    For a raw option, ``harness`` is None and ``provider``/``model_id`` are set.
    """

    executor_type: str  # EXECUTOR_TYPE_RAW | EXECUTOR_TYPE_HARNESSED
    harness: str | None = None
    provider: str | None = None
    model_id: str | None = None

    @property
    def llm_model(self) -> str | None:
        """The ``provider/model_id`` LLM string, or None for harness-default."""
        if self.provider and self.model_id:
            return f"{self.provider}/{self.model_id}"
        return None

    @property
    def canonical(self) -> str:
        """Canonical chain-entry encoding (see module docstring)."""
        if self.executor_type == EXECUTOR_TYPE_RAW:
            return f"raw:{self.llm_model}"
        # harnessed: "harnessed:{harness}" (default model) or
        # "harnessed:{harness}:{provider}/{model}" (explicit model).
        if self.llm_model:
            return f"harnessed:{self.harness}:{self.llm_model}"
        return f"harnessed:{self.harness}"

    @property
    def display_label(self) -> str:
        """Human-facing label shown in the Streamlit dropdown / chain editor."""
        if self.executor_type == EXECUTOR_TYPE_RAW:
            return f"[RAW] {self.llm_model}"
        if self.llm_model:
            return f"[HARNESSED] {self.harness} · {self.llm_model}"
        return f"[HARNESSED] {self.harness} (default)"


def enumerate_model_options() -> list[ModelOption]:
    """Enumerate every selectable option, sorted alphabetically by display label.

    For each harness in ``HARNESSED_MODELS``: a harness-default option plus an
    explicit option for every ``KNOWN_MODELS`` model of the harness's provider
    (so claude-code-cli and claude-agent-sdk each expose all Anthropic models,
    codex-cli / codex-app-server each expose all OpenAI models). Plus a ``[RAW]``
    option for every ``KNOWN_MODELS`` model.
    """
    models_by_provider: dict[str, list[tuple[str, str]]] = {}
    for model in KNOWN_MODELS:
        provider, model_id = parse_model_string(model)
        models_by_provider.setdefault(provider, []).append((provider, model_id))

    options: list[ModelOption] = []
    for harness in HARNESSED_MODELS:
        options.append(ModelOption(executor_type=EXECUTOR_TYPE_HARNESSED, harness=harness))
        provider = HARNESS_PROVIDER_MAP.get(harness, "")
        for prov, model_id in models_by_provider.get(provider, []):
            options.append(
                ModelOption(
                    executor_type=EXECUTOR_TYPE_HARNESSED,
                    harness=harness,
                    provider=prov,
                    model_id=model_id,
                )
            )

    for prov, model_id in (pm for models in models_by_provider.values() for pm in models):
        options.append(ModelOption(executor_type=EXECUTOR_TYPE_RAW, provider=prov, model_id=model_id))

    return sorted(options, key=lambda o: o.display_label)


def parse_chain_entry(entry: str) -> ModelOption:
    """Parse a canonical chain entry into a :class:`ModelOption`.

    Forms:
      * ``raw:{provider}/{model}``
      * ``harnessed:{harness}`` — harness with its default model
      * ``harnessed:{harness}:{provider}/{model}`` — harness with explicit model

    Raises:
        ValueError: on an unknown kind, harness, or provider, or a malformed entry.
    """
    if ":" not in entry:
        raise ValueError(f"Invalid chain entry: {entry!r}. Expected 'harnessed:...' or 'raw:...'.")
    kind, rest = entry.split(":", 1)

    if kind == EXECUTOR_TYPE_RAW:
        provider, model_id = parse_model_string(rest)  # validates provider + 'provider/model' shape
        return ModelOption(executor_type=EXECUTOR_TYPE_RAW, provider=provider, model_id=model_id)

    if kind == EXECUTOR_TYPE_HARNESSED:
        if ":" in rest:
            harness, model_str = rest.split(":", 1)
            if harness not in HARNESSED_MODELS:
                raise ValueError(f"Unknown harness: {harness!r}. Known: {list(HARNESSED_MODELS)}.")
            provider, model_id = parse_model_string(model_str)
            return ModelOption(
                executor_type=EXECUTOR_TYPE_HARNESSED, harness=harness, provider=provider, model_id=model_id
            )
        if rest not in HARNESSED_MODELS:
            raise ValueError(f"Unknown harness: {rest!r}. Known: {list(HARNESSED_MODELS)}.")
        return ModelOption(executor_type=EXECUTOR_TYPE_HARNESSED, harness=rest)

    raise ValueError(f"Unknown chain entry kind: {kind!r} in {entry!r}.")


def format_chain_entry(option: ModelOption) -> str:
    """Inverse of :func:`parse_chain_entry`."""
    return option.canonical


def chain_entry_to_executor(entry: str) -> tuple[str, str | None, str | None]:
    """Map a chain entry to ``(executor_type, harness, llm_model)``.

    * raw: ``("raw", None, "provider/model")``
    * harnessed explicit: ``("harnessed", harness, "provider/model")``
    * harnessed default: ``("harnessed", harness, None)``
    """
    opt = parse_chain_entry(entry)
    return opt.executor_type, opt.harness, opt.llm_model


def canonical_to_display_label(canonical: str) -> str:
    """Render a canonical entry as its human-facing label."""
    return parse_chain_entry(canonical).display_label


def display_label_to_canonical(label: str) -> str:
    """Resolve a dropdown display label back to its canonical entry.

    Raises:
        ValueError: if the label does not match any enumerated option.
    """
    for opt in enumerate_model_options():
        if opt.display_label == label:
            return opt.canonical
    raise ValueError(f"Unknown model option label: {label!r}.")


# --- Rate-limit detection -------------------------------------------------

_RATE_LIMIT_KEYWORDS: tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "usage limit",
    "quota",
    "429",
    "overloaded",
    "too many requests",
    "limit reached",
    "resets in",
)


def is_rate_limit_failure(text: str | None) -> bool:
    """True if ``text`` looks like a provider rate / usage limit error.

    Case-insensitive substring match. Used to decide whether a failed turn
    should trigger a fallback to the next model in the chain.
    """
    if not text:
        return False
    low = text.lower()
    return any(keyword in low for keyword in _RATE_LIMIT_KEYWORDS)


# --- Global fallback chain (Redis-backed, code-default) -------------------


async def get_fallback_chain() -> list[str]:
    """Return the runtime fallback chain, or the hardcoded default.

    Invalid entries (unknown provider/harness/kind) are dropped and logged; if
    nothing valid remains, the hardcoded :data:`DEFAULT_FALLBACK_CHAIN` is used.
    """
    from ypl.backend.feature_flags import get_feature_value

    value = await get_feature_value(FALLBACK_CHAIN_FLAG, default=None)
    if not isinstance(value, list) or not value:
        return list(DEFAULT_FALLBACK_CHAIN)

    validated: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            logger.warning("Dropping non-string fallback chain entry", entry=entry)
            continue
        try:
            parse_chain_entry(entry)
        except ValueError:
            logger.warning("Dropping invalid fallback chain entry", entry=entry)
            continue
        validated.append(entry)

    return validated or list(DEFAULT_FALLBACK_CHAIN)


async def set_fallback_chain(entries: list[str]) -> None:
    """Persist the fallback chain to Redis. Validates every entry first.

    Raises:
        ValueError: if any entry is not a valid canonical chain entry.
    """
    for entry in entries:
        parse_chain_entry(entry)  # raises on invalid
    from ypl.backend.feature_flags import set_feature_value

    await set_feature_value(FALLBACK_CHAIN_FLAG, entries)

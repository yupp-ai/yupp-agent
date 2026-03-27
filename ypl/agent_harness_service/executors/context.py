"""Context management for the raw executor — pruning and compaction.

Phase 2: When the conversation approaches the context limit, this module
provides two recovery mechanisms applied in order:

1. **Pruning** — Replace old tool results with short placeholders. Cheap
   (no LLM call), preserves the last N steps' results intact.
2. **Compaction** — Summarize the entire conversation into a single
   message using a lightweight model (e.g., haiku). Used only when
   pruning alone isn't enough.

Phase 3: Disk-based session history persistence for multi-turn raw executor
sessions. Saves and loads the full messages array (including all tool_use and
tool_result blocks) so the raw executor can resume across turns.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from typing import Any

import anthropic

from ypl.agent_harness_service.common.constants import (
    AHS_SESSIONS_DIR,
    COMPACTION_CONTENT_PREVIEW_CHARS,
    COMPACTION_TOOL_RESULT_PREVIEW_CHARS,
)
from ypl.agent_harness_service.common.models import CompactionConfig
from ypl.agent_harness_service.executors.raw_executor import estimate_messages_tokens
from ypl.structured_logger import get_logger
from ypl.utils import maybe_truncate

logger = get_logger()

COMPACTION_PROMPT = """\
You are a conversation summarizer for an AI agent. The agent was given a task \
and has been working on it using tools. The conversation is getting too long \
and needs to be compacted.

Summarize the conversation so far in a structured format:

## Goal
What is the agent trying to accomplish?

## Progress
What has been done so far? List key actions and their outcomes.

## Key Findings
Important information discovered (file paths, error messages, data values, etc.)

## Current State
Where is the agent in the task? What was it about to do next?

Be concise but preserve all actionable details (file paths, variable names, \
error messages, command outputs). The agent must be able to continue its work \
from this summary alone."""


def prune_tool_results(
    messages: list[dict[str, Any]],
    system_prompt: str,
    context_limit: int,
    target_ratio: float,
    protect_steps: int,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Replace old tool results with short placeholders to reduce context size.

    Walks backwards through messages, skipping the last ``protect_steps``
    tool-result messages, and replaces older tool result content with a
    compact placeholder. Stops once estimated tokens are below
    ``target_ratio * context_limit``.

    Args:
        messages: Conversation messages (mutated in-place).
        system_prompt: System prompt text (for token estimation).
        context_limit: Model's context window size in tokens.
        target_ratio: Target context usage (e.g. 0.7 = 70%).
        protect_steps: Number of recent tool-result messages to keep intact.
        tool_schemas: Tool schemas sent with API calls (for accurate token estimation).

    Returns:
        Tuple of (messages, number of results pruned).
    """
    target_tokens = int(context_limit * target_ratio)
    estimated = estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas)

    if estimated <= target_tokens:
        return messages, 0

    # Collect indices of tool-result messages (both Anthropic and OpenAI formats).
    # A "tool-result message" is:
    #   - Anthropic: role=user, content is a list containing tool_result blocks
    #   - OpenAI: role=tool
    tool_result_indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            # OpenAI format
            tool_result_indices.append(i)
        elif msg.get("role") == "user" and isinstance(msg.get("content"), list):
            # Anthropic format: check if any block is a tool_result
            if any(b.get("type") == "tool_result" for b in msg["content"]):
                tool_result_indices.append(i)

    # Protect the most recent N tool-result messages (protect_steps=0 means prune all)
    prunable = tool_result_indices[: max(0, len(tool_result_indices) - protect_steps)]

    pruned_count = 0
    for idx in prunable:
        if estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas) <= target_tokens:
            break

        msg = messages[idx]
        if msg.get("role") == "tool":
            # OpenAI format: single tool result
            original_len = len(msg.get("content", ""))
            msg["content"] = f"[pruned: tool result, {original_len} chars]"
            pruned_count += 1
        elif isinstance(msg.get("content"), list):
            # Anthropic format: prune each tool_result block
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    original_content = block.get("content", "")
                    original_len = (
                        len(original_content)
                        if isinstance(original_content, str)
                        else len(json.dumps(original_content))
                    )
                    block["content"] = f"[pruned: tool result, {original_len} chars]"
                    pruned_count += 1

    if pruned_count:
        new_estimate = estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas)
        logger.info(
            "Pruned tool results",
            pruned_count=pruned_count,
            tokens_before=estimated,
            tokens_after=new_estimate,
        )

    return messages, pruned_count


async def compact_messages(
    messages: list[dict[str, Any]],
    original_prompt: str,
    system_prompt: str,
    config: CompactionConfig,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Summarize the conversation into a single message using a lightweight model.

    Replaces the entire message history with a compacted form:
    ``[{"role": "user", "content": summary + original_prompt}]``

    On LLM failure, returns the **original messages** unchanged so the caller
    can fall back to the hard-stop path instead of silently losing context.

    Args:
        messages: Current conversation messages.
        original_prompt: The original user prompt (always preserved).
        system_prompt: System prompt (for token estimation).
        config: Compaction configuration.
        tool_schemas: Tool schemas sent with API calls (for accurate token estimation).

    Returns:
        Tuple of (new_messages, stats_dict) where stats_dict contains
        tokens_before, tokens_after, and compaction_cost info.
    """
    tokens_before = estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas)

    # Build the conversation text for summarization
    conversation_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Flatten Anthropic content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[tool_use: {block.get('name', '?')}]")
                    elif block.get("type") == "tool_result":
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            result_content = maybe_truncate(result_content, COMPACTION_TOOL_RESULT_PREVIEW_CHARS)
                        text_parts.append(f"[tool_result: {result_content}]")
            content = "\n".join(text_parts)
        elif isinstance(content, str):
            content = maybe_truncate(content, COMPACTION_CONTENT_PREVIEW_CHARS)
        conversation_parts.append(f"{role}: {content}")

    conversation_text = "\n\n".join(conversation_parts)

    # Cap the conversation text sent to the compaction model
    max_compaction_input = 100_000  # chars (~25k tokens)
    if len(conversation_text) > max_compaction_input:
        conversation_text = conversation_text[:max_compaction_input] + "\n...[conversation truncated for compaction]"

    # Call the compaction model (currently Anthropic-only)
    from ypl.agent_harness_service.executors.providers import parse_model_string
    from ypl.agent_harness_service.executors.raw_executor import _estimate_cost

    compaction_input_tokens = 0
    compaction_output_tokens = 0

    try:
        provider, model_id = parse_model_string(config.model)

        if provider != "anthropic":
            raise NotImplementedError(f"Compaction only supports Anthropic models, got '{provider}'")

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.AsyncAnthropic(api_key=api_key)

        response = await client.messages.create(
            model=model_id,
            max_tokens=2048,
            system=COMPACTION_PROMPT,
            messages=[{"role": "user", "content": conversation_text}],
        )
        first_block = response.content[0] if response.content else None
        summary = (
            first_block.text if first_block and hasattr(first_block, "text") else "[compaction failed: empty response]"
        )
        compaction_input_tokens = response.usage.input_tokens
        compaction_output_tokens = response.usage.output_tokens
    except Exception as e:
        logger.error("Compaction LLM call failed", error=str(e), exc_info=True)
        # Return original messages unchanged so the caller can fall back to
        # the hard-stop path instead of silently losing all context.
        return messages, {
            "tokens_before": tokens_before,
            "tokens_after": tokens_before,
            "compaction_input_tokens": 0,
            "compaction_output_tokens": 0,
            "compaction_cost_usd": 0.0,
            "summary_length": 0,
            "compaction_failed": True,
        }

    # Build the compacted message
    compacted_content = f"""<conversation_summary>
{summary}
</conversation_summary>

---

Original task (continue from where you left off):
{original_prompt}"""

    new_messages = [{"role": "user", "content": compacted_content}]
    tokens_after = estimate_messages_tokens(new_messages, system_prompt, tool_schemas=tool_schemas)

    compaction_cost = _estimate_cost(
        model_id, {"input_tokens": compaction_input_tokens, "output_tokens": compaction_output_tokens}
    )

    stats = {
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "compaction_input_tokens": compaction_input_tokens,
        "compaction_output_tokens": compaction_output_tokens,
        "compaction_cost_usd": compaction_cost,
        "summary_length": len(summary),
    }

    logger.info(
        "Compacted conversation",
        **stats,
    )

    return new_messages, stats


# --- Phase 3: Disk-based session history persistence ---


def get_session_history_path(session_id: str) -> str:
    """Get the file path for a session's raw executor history.

    History lives under the consolidated session workspace:
    ``AHS_SESSIONS_DIR/{session_id}/history/raw_history.json``

    Args:
        session_id: The agent session UUID string.

    Returns:
        Absolute path to the session history JSON file.

    Raises:
        ValueError: If session_id contains path traversal characters.
    """
    if os.sep in session_id or (os.altsep and os.altsep in session_id) or ".." in session_id:
        raise ValueError(f"Invalid session_id for path construction: {session_id!r}")
    return os.path.join(AHS_SESSIONS_DIR, session_id, "history", "raw_history.json")


def save_session_history(
    path: str,
    messages: list[dict[str, Any]],
    provider: str,
) -> bool:
    """Save the full messages array to disk for cross-turn resumption.

    Persists all messages including assistant tool_use blocks and user
    tool_result blocks so the raw executor can resume with full context.

    Args:
        path: File path to write the history to.
        messages: The complete messages list from the raw executor loop.
        provider: Provider name (e.g., "anthropic", "openai") — needed to
            interpret message format on reload.

    Returns:
        True if saved successfully, False on failure.
    """
    payload = {
        "provider": provider,
        "message_count": len(messages),
        "last_updated": datetime.now(UTC).isoformat(),
        "messages": messages,
    }

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Atomic write: write to temp file then rename, so the file is either
        # fully written or untouched (no partial/empty file on crash).
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=None)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(
            "Saved session history",
            path=path,
            message_count=len(messages),
        )
        return True
    except Exception:
        logger.error("Failed to save session history", path=path, exc_info=True)
        return False


def save_pre_compaction_snapshot(
    history_path: str,
    messages: list[dict[str, Any]],
    provider: str,
) -> None:
    """Save a timestamped snapshot of messages before compaction replaces them.

    Written to the same directory as the main history file, with a timestamp
    in the filename so multiple compactions across turns are preserved.

    Args:
        history_path: Path to the main raw_history.json file (used to derive
            the snapshot directory).
        messages: The full messages array before compaction.
        provider: Provider name for the messages format.
    """
    snapshot_dir = os.path.dirname(history_path)
    now = datetime.now(UTC)
    timestamp = now.strftime("%Y%m%dT%H%M%S_%f")
    snapshot_path = os.path.join(snapshot_dir, f"pre_compaction_{timestamp}.json")

    payload = {
        "provider": provider,
        "message_count": len(messages),
        "snapshot_time": now.isoformat(),
        "messages": messages,
    }

    try:
        os.makedirs(snapshot_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=snapshot_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=None)
            os.replace(tmp_path, snapshot_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(
            "Saved pre-compaction snapshot",
            path=snapshot_path,
            message_count=len(messages),
        )
    except Exception:
        logger.error("Failed to save pre-compaction snapshot", path=snapshot_path, exc_info=True)


def load_session_history(path: str) -> tuple[list[dict[str, Any]], str] | None:
    """Load the full messages array from disk.

    Args:
        path: File path to read the history from.

    Returns:
        Tuple of (messages, provider) if the file exists and is valid,
        None otherwise.
    """
    if not os.path.isfile(path):
        return None

    try:
        with open(path) as f:
            payload = json.load(f)

        if not isinstance(payload, dict):
            logger.warning("Session history file has unexpected format", path=path)
            return None

        messages = payload.get("messages", [])
        provider = payload.get("provider", "")

        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            logger.warning("Session history has malformed messages field", path=path)
            return None

        if not messages or not provider:
            logger.warning("Session history file is empty or missing fields", path=path)
            return None

        logger.info(
            "Loaded session history",
            path=path,
            message_count=len(messages),
            provider=provider,
        )
        return messages, provider

    except (json.JSONDecodeError, OSError):
        logger.error("Failed to load session history", path=path, exc_info=True)
        return None


def cleanup_session_history(retention_days: int = 60, dry_run: bool = False) -> dict[str, Any]:
    """Delete session history directories older than the retention period.

    Uses file modification time to determine age — mtime is updated every
    time the raw executor saves at the end of a turn, so it tracks when
    the session was last active.

    Args:
        retention_days: Delete sessions not modified in this many days.
        dry_run: If True, log what would be deleted without actually deleting.

    Returns:
        Stats dict with deleted_count, skipped_count, freed_bytes, and errors.
    """
    import shutil
    import time

    base_dir = AHS_SESSIONS_DIR
    cutoff = time.time() - (retention_days * 86_400)

    stats: dict[str, Any] = {
        "base_dir": base_dir,
        "retention_days": retention_days,
        "dry_run": dry_run,
        "deleted_count": 0,
        "skipped_count": 0,
        "freed_bytes": 0,
        "errors": 0,
    }

    if not os.path.isdir(base_dir):
        logger.info("Session history base dir does not exist, nothing to clean", base_dir=base_dir)
        return stats

    for entry in os.scandir(base_dir):
        if not entry.is_dir():
            continue

        # History lives under {session}/history/raw_history.json
        history_file = os.path.join(entry.path, "history", "raw_history.json")
        if not os.path.isfile(history_file):
            continue

        mtime = os.path.getmtime(history_file)
        if mtime >= cutoff:
            stats["skipped_count"] += 1
            continue

        # Calculate size of the entire session directory
        total_size = 0
        for dirpath, _dirnames, filenames in os.walk(entry.path):
            for fname in filenames:
                try:
                    total_size += os.path.getsize(os.path.join(dirpath, fname))
                except OSError:
                    pass

        if dry_run:
            logger.info(
                "Would delete session (dry run)",
                session_dir=entry.path,
                age_days=round((time.time() - mtime) / 86_400, 1),
                size_bytes=total_size,
            )
            stats["deleted_count"] += 1
            stats["freed_bytes"] += total_size
            continue

        try:
            # TODO: call cleanup_worktree() for each worktree before rmtree to
            # cleanly remove git worktree references and avoid orphaned entries
            # in the parent repo's .git/worktrees/ directory.
            shutil.rmtree(entry.path)
            stats["deleted_count"] += 1
            stats["freed_bytes"] += total_size
        except OSError:
            logger.error("Failed to delete session dir", path=entry.path, exc_info=True)
            stats["errors"] += 1

    logger.info("Session history cleanup complete", **stats)
    return stats

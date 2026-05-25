"""OpenAI Codex CLI runner for Agent Harness Service.

Runs `codex exec --json` as a subprocess and translates the V2 JSONL events
into StreamEvent objects consumed by the service layer.
"""

import asyncio
import json
import shlex
import time
from collections.abc import AsyncIterator

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.executors.mcp_config import build_codex_mcp_args, build_codex_mcp_env
from ypl.agent_harness_service.executors.runner import (
    AGENT_RESPONSE_DEBUG,
    AgentRunner,
    RunContext,
    StreamEvent,
    _handle_stream_limit_error,
    _SessionFileLogger,
    _try_enlarge_pipe_buf,
    build_subprocess_env,
    extract_excerpt,
)
from ypl.agent_harness_service.executors.system_prompt import build_system_prompt
from ypl.structured_logger import get_logger

logger = get_logger()


def _extract_mcp_tool_name(item: dict[str, object]) -> str:
    """Extract the MCP tool name from a Codex item."""
    tool_name = item.get("tool") or item.get("tool_name") or item.get("name") or ""
    return str(tool_name)


def _extract_mcp_tool_input(item: dict[str, object]) -> dict[str, object] | str:
    """Extract MCP tool arguments from a Codex item."""
    arguments = item.get("arguments", item.get("input", {}))
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    return str(arguments)


def _format_mcp_tool_payload(payload: object) -> str:
    """Render an MCP tool payload as a readable string."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        text_parts = [
            block.get("text", "") for block in payload if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if text_parts:
            return " ".join(text_parts)
        return json.dumps(payload, default=str)
    if isinstance(payload, dict):
        content = payload.get("content")
        if content is not None:
            return _format_mcp_tool_payload(content)
        text = payload.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(payload, default=str)
    return str(payload)


def _build_mcp_tool_result(item: dict[str, object]) -> tuple[str, bool]:
    """Extract a normalized tool result payload from a completed MCP tool call."""
    status = str(item.get("status", ""))
    error = item.get("error")
    result = item.get("result", item.get("output"))

    is_error = status == "failed"
    if isinstance(result, dict):
        is_error = is_error or bool(result.get("is_error") or result.get("isError"))

    if error is not None:
        return _format_mcp_tool_payload(error), True

    if result is not None:
        return _format_mcp_tool_payload(result), is_error

    if is_error:
        return "MCP tool call failed", True

    return "", False


class CodexRunner(AgentRunner):
    """Runs OpenAI Codex CLI as a subprocess with JSONL streaming output."""

    def __init__(self, agent_config: AgentConfig) -> None:
        self.config = agent_config

    @staticmethod
    def _toml_escape(value: str) -> str:
        """Escape a string for use as a TOML double-quoted value.

        create_subprocess_exec bypasses the shell, so we need proper TOML escaping
        rather than shell escaping.
        """
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    def _build_args(self, prompt: str, context: RunContext) -> list[str]:
        """Build the CLI args for codex exec.

        NOTE: Codex CLI has no --allowedTools/--disallowedTools equivalent.
        tool_permissions from AgentConfig are not enforced here.
        """
        is_resume = bool(context.llm_session_id)
        args = ["codex", "exec"]

        # Resume existing session or start new one
        if is_resume and context.llm_session_id:
            args += ["resume", context.llm_session_id]

        args.append(prompt)

        args += ["--json", "--full-auto", "--skip-git-repo-check"]

        if self.config.model:
            args += ["-m", self.config.model]

        # -C is rejected by `codex exec resume`; cwd is set via create_subprocess_exec
        if context.workspace and not is_resume:
            args += ["-C", context.workspace]

        # System prompt via config override (only for new sessions — resume inherits)
        if not is_resume:
            system_prompt = build_system_prompt(
                self.config.name,
                session_id=context.session_id,
                slack_session_id=context.slack_session_id,
                is_slack=context.is_slack,
                is_task=context.is_task,
                session_context=context.session_context,
                additional_system_prompt=self.config.additional_system_prompt,
                has_native_skills=True,
                required_tools=self.config.required_tools or None,
                workspace=context.workspace,
            )
            if system_prompt:
                escaped = self._toml_escape(system_prompt)
                args += ["-c", f'developer_instructions="{escaped}"']

        # Inject MCP servers via -c flags (Codex reads config.toml, not .mcp.json).
        # Unlike developer_instructions, these must be passed on resumed turns too.
        # `codex exec resume` accepts `-c`, and omitting MCP config on follow-up turns
        # can cause tools from prior turns to disappear from the callable surface.
        args += build_codex_mcp_args(
            session_id=context.session_id,
            session_context=context.session_context,
            is_slack=context.is_slack,
        )

        return args

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> str:
        """Drain stderr in the background to prevent pipe deadlock."""
        assert proc.stderr is not None
        chunks = [chunk async for chunk in proc.stderr]
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Spawn codex CLI and yield parsed stream events."""
        args = self._build_args(prompt, context)
        cwd = context.workspace
        model_label = self.config.model or "(codex default)"

        cmd_str = shlex.join(args)
        logger.info(
            "Launching Codex CLI",
            session_id=context.session_id,
            cwd=cwd,
            model=model_label,
            command=cmd_str,
        )

        # Codex CLI emits single JSON lines that can exceed asyncio's default
        # 64KB StreamReader limit (e.g. large tool results, file reads).
        # Raise to 10MB to avoid LimitOverrunError (same as ClaudeCodeRunner).
        stream_limit = 10 * 1024 * 1024

        # Inject MCP bearer token env var so Codex CLI can authenticate
        # with the harness MCP server via bearer_token_env_var.
        env = build_subprocess_env()
        env.update(build_codex_mcp_env(context.session_id))

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=stream_limit,
            env=env,
        )

        # Increase OS pipe buffers from 64 KB → 4 MB to reduce context switches
        # on large tool results (file reads, GCP logs, DB query output).
        _try_enlarge_pipe_buf(proc)

        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        file_logger = _SessionFileLogger(context.llm_session_id)

        thread_id: str | None = None
        message_count = 0
        start_time = time.monotonic()

        try:
            assert proc.stdout is not None
            async for line in proc.stdout:
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    raw = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line from Codex CLI", line=line_str[:200])
                    continue

                # Codex CLI emits dot-separated event types (e.g. "item.completed",
                # "turn.started") while the streaming/server wire format uses slashes
                # ("item/completed", "turn/started").  Normalise to slashes once here
                # so all downstream matching works regardless of which format the
                # running binary happens to produce.
                # Guard against malformed events where "type" is null or non-string —
                # treat those as unknown and let the else-branch skip them safely.
                raw_type = raw.get("type")
                codex_type = raw_type.replace(".", "/") if isinstance(raw_type, str) else ""
                item = raw.get("item", {})
                item_type = item.get("type", "") if isinstance(item, dict) else ""

                # Map Codex V2 events to StreamEvent
                if codex_type == "thread/started":
                    thread_id = raw.get("thread_id")
                    event = StreamEvent(
                        type="system",
                        raw={"type": "system", "session_id": thread_id},
                    )

                elif codex_type == "turn/started":
                    continue

                elif codex_type == "item/started" and item_type == "command_execution":
                    event = StreamEvent(
                        type="tool_use",
                        raw={
                            "type": "tool_use",
                            "id": item.get("id", ""),
                            "tool_name": "Bash",
                            "input": item.get("command", ""),
                        },
                    )

                elif codex_type == "item/started" and item_type == "mcp_tool_call":
                    event = StreamEvent(
                        type="tool_use",
                        raw={
                            "type": "tool_use",
                            "id": item.get("id", ""),
                            "tool_name": _extract_mcp_tool_name(item),
                            "input": _extract_mcp_tool_input(item),
                        },
                    )

                elif codex_type == "item/completed" and item_type == "command_execution":
                    event = StreamEvent(
                        type="tool_result",
                        raw={
                            "type": "tool_result",
                            "tool_use_id": item.get("id", ""),
                            "content": item.get("aggregated_output", ""),
                        },
                    )

                elif codex_type == "item/completed" and item_type == "mcp_tool_call":
                    content, is_error = _build_mcp_tool_result(item)
                    event = StreamEvent(
                        type="tool_result",
                        raw={
                            "type": "tool_result",
                            "tool_use_id": item.get("id", ""),
                            "content": content,
                            "is_error": is_error,
                        },
                    )

                elif codex_type == "item/completed" and item_type == "agent_message":
                    message_count += 1
                    text = item.get("text", "")
                    event = StreamEvent(
                        type="assistant",
                        raw={
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": text}],
                            },
                        },
                    )

                elif codex_type == "item/completed" and item_type == "file_change":
                    changes = item.get("changes", [])
                    summary = "; ".join(f"{c.get('kind', '?')} {c.get('path', '?')}" for c in changes)
                    event = StreamEvent(
                        type="tool_result",
                        raw={
                            "type": "tool_result",
                            "tool_use_id": item.get("id", ""),
                            "content": f"file_change: {summary}",
                        },
                    )

                elif codex_type == "item/started" and item_type == "file_change":
                    path = item.get("path", "")
                    kind = item.get("kind", "change")
                    event = StreamEvent(
                        type="tool_use",
                        raw={
                            "type": "tool_use",
                            "id": item.get("id", ""),
                            "tool_name": "FileChange",
                            "input": f"{kind} {path}",
                        },
                    )

                elif codex_type == "item/completed" and item_type == "reasoning":
                    # Internal reasoning — skip
                    continue

                elif codex_type == "turn/completed":
                    turn_status = raw.get("status", raw.get("turn", {}).get("status", ""))
                    if turn_status == "failed":
                        # turn/completed with status "failed" — treat as error
                        error_msg = raw.get("error", {})
                        error_text = (
                            error_msg.get("message", str(error_msg)) if isinstance(error_msg, dict) else str(error_msg)
                        )
                        event = StreamEvent(
                            type="error",
                            raw={"error": f"Codex turn failed: {error_text}"},
                        )
                    else:
                        elapsed_ms = int((time.monotonic() - start_time) * 1000)
                        event = StreamEvent(
                            type="result",
                            raw={
                                "type": "result",
                                "session_id": thread_id,
                                "estimated_cost_usd": None,
                                "duration_ms": elapsed_ms,
                                "num_turns": message_count,
                            },
                        )

                elif codex_type == "error":
                    error_text = raw.get("message", raw.get("error", "Unknown Codex error"))
                    event = StreamEvent(
                        type="error",
                        raw={"error": f"Codex error: {error_text}"},
                    )

                elif codex_type == "item/completed" and item_type == "error":
                    error_text = item.get("message", item.get("text", "Unknown item error"))
                    event = StreamEvent(
                        type="error",
                        raw={"error": f"Codex item error: {error_text}"},
                    )

                else:
                    # Unknown event type — log and skip
                    logger.debug(
                        "Skipping unknown Codex event",
                        codex_type=codex_type,
                        item_type=item_type,
                        session_id=context.session_id,
                    )
                    continue

                excerpt = extract_excerpt(event)
                file_logger.log(model_label, event.type, excerpt, llm_session_id=event.session_id)

                if AGENT_RESPONSE_DEBUG:
                    logger.info(
                        f"Received message from {model_label}: [{event.type}] {excerpt}",
                        session_id=context.session_id,
                    )

                yield event

            await proc.wait()
            stderr_text = await stderr_task

            logger.info(
                "Codex CLI process exited",
                session_id=context.session_id,
                returncode=proc.returncode,
                stderr_preview=stderr_text[:500] if stderr_text else None,
            )

            if proc.returncode and proc.returncode != 0:
                logger.error(
                    "Codex CLI exited with error",
                    session_id=context.session_id,
                    returncode=proc.returncode,
                    stderr=stderr_text[:1000],
                )
                yield StreamEvent(
                    type="error",
                    raw={
                        "error": f"CLI exited with code {proc.returncode}",
                        "stderr": stderr_text[:1000],
                    },
                )

        except ValueError as e:
            yield _handle_stream_limit_error(e, "Codex", context.session_id, stream_limit)
        except Exception as e:
            logger.error(
                "Error during Codex CLI execution",
                session_id=context.session_id,
                error=str(e),
                exc_info=True,
            )
            yield StreamEvent(type="error", raw={"error": str(e)})
        finally:
            # Kill subprocess on any exit (including CancelledError from timeout)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            stderr_task.cancel()
            file_logger.close()

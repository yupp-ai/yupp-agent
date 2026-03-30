---
date: 2026-03-30
topic: service-py-decomposition
---

# Decompose service.py for AI Workability

## Problem Frame

`service.py` is 3,489 lines with 58 functions and 50 imports. Its largest function, `_run_agent_task`, is 821 lines with 73 if-statements and 16 try/except blocks. When an AI agent needs to modify session lifecycle behavior, it must load the entire file into context and reason about a function that exceeds most models' effective reasoning window. This makes AI-assisted development slower, more error-prone, and more expensive (token cost).

9 files across the codebase import from `service.py`, making it the most-imported wiring-layer module.

## Current Structure

```
service.py (3,489 lines, 58 functions)
├── Agent run lifecycle     (~900 lines)  _run_agent_task + setup/teardown helpers
├── Session CRUD            (~880 lines)  create_session, send_message, stop_session, etc.
├── Query/read handlers     (~350 lines)  list_sessions, get_session_detail, list_agents, etc.
├── Agent CRUD              (~130 lines)  create_agent, edit_agent
└── Message/tool helpers    (~200 lines)  _trim_value, _extract_visible_content, tool extractors
```

## Requirements

**R1. Phase-based decomposition of `_run_agent_task`**

Break the 821-line function into sequential phases, each as a standalone async function. The main `_run_agent_task` becomes a ~50-line orchestrator that calls phases in order. Proposed phases:

1. **Setup** (~80 lines): interrupt guard, config loading, sandbox/websearch reset
2. **Executor dispatch** (~120 lines): runner selection, pre-spawn handling, subprocess launch
3. **Response processing** (~200 lines): output parsing, tool use extraction, content extraction
4. **Persistence** (~200 lines): DB writes for messages, metadata, cost tracking
5. **Delivery & cleanup** (~150 lines): gateway delivery, memory sync, session completion, pending message drain

Phase boundaries should be at points where data flows are narrow (a struct or a few values pass between phases), not in the middle of tightly coupled logic.

**R2. Split service.py into a `service/` package**

Convert `service.py` into a package with focused submodules:

| Module | Contents | Est. lines |
|--------|----------|-----------|
| `service/__init__.py` | Re-exports all public names for backward compat | ~40 |
| `service/run_task.py` | `_run_agent_task` orchestrator + phase functions | ~900 |
| `service/session_lifecycle.py` | `create_session`, `send_message`, `stop_session`, `attach_slack`, `send_feedback` | ~880 |
| `service/queries.py` | `list_sessions`, `get_session_detail`, `list_agents`, `get_agent_detail`, `get_session_history` | ~350 |
| `service/agent_crud.py` | `create_agent`, `edit_agent`, `_build_agent_info` | ~130 |
| `service/message_helpers.py` | `_trim_value`, `_scrub_null_bytes`, `_extract_visible_content`, tool extraction fns | ~200 |

**R3. Backward-compatible re-exports**

`service/__init__.py` must re-export all names that are currently importable from `ypl.agent_harness_service.service`. The 9 existing callers must continue working with zero import changes.

**R4. Preserve AHS layering rules**

The new submodules remain in the wiring layer. They may import from Layer 0 (`common/`) and Layer 1 (`core/`, `gateway/`, `executors/`, `tools/`). Layer 1 packages must not import from the new submodules. The existing architecture tests (`test_architecture.py`, `test_imports.py`) must continue to pass.

**R5. No behavioral changes**

This is a pure structural refactor. No logic changes, no new features, no API changes. All existing tests must pass without modification (except import path adjustments in test files if needed).

## Success Criteria

- No function in the codebase exceeds 200 lines
- No single file in the `service/` package exceeds 1,000 lines
- `_run_agent_task` orchestrator is under 80 lines
- All existing tests pass without logic changes
- Architecture tests (`test_architecture.py`, `test_imports.py`) pass
- Lint (ruff), type checking (mypy), and CI all green
- Existing callers work without import changes

## Scope Boundaries

- **Out of scope:** Refactoring `local_mcp_server.py` (2,469 lines) — separate effort
- **Out of scope:** Refactoring `orchestration.py` `run_subagent` (355 lines) — separate effort
- **Out of scope:** Changing any public API behavior or signatures
- **Out of scope:** Adding new abstractions (classes, protocols) beyond what the split requires

## Key Decisions

- **Phase-based over class-based:** A TurnRunner class would be a larger structural change with more risk. Phase functions achieve the same readability gains with less disruption.
- **Re-export over caller migration:** Re-exporting from `__init__.py` means zero migration cost for the 9 importing files. Callers can optionally update to direct imports later.
- **Package over flat files:** A `service/` package groups related modules together and scales better than `service_run_task.py`, `service_queries.py` etc. at the wiring layer root.

## Dependencies / Assumptions

- The architecture tests in `test_architecture.py` and `test_imports.py` may need updates to recognize the new `service/` package as part of the wiring layer
- Module-level state (e.g., `_pre_spawn_tasks` dict) will live in the submodule that uses it, with re-export if needed

## Outstanding Questions

### Deferred to Planning
- [Affects R1][Technical] What is the exact data contract between phases in `_run_agent_task`? Need to read the full function to identify the narrowest boundaries.
- [Affects R2][Technical] Are there circular import risks between the new submodules (e.g., `session_lifecycle.py` calling `run_task.py`)? Need to trace the call graph.
- [Affects R4][Needs research] Do `test_architecture.py` and `test_imports.py` hardcode `service.py` or detect wiring-layer modules dynamically?

## Next Steps

-> `/ce:plan` for structured implementation planning

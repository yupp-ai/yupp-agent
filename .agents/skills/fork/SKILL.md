---
name: fork
description: Fork the current session into a new peer session that keeps the conversation history but evolves independently. For Slack-triggered sessions, opens in a new top-level thread in the same channel. Usage: /fork <additional_instructions>
---

# Fork Session

Fork the current session into a new peer session that snapshots the prior
conversation, then heads in a new direction guided by the caller's
``additional_instructions``. The forked session evolves independently — it has
its own session UUID, its own message stream, and (when triggered from Slack)
its own thread in the same channel.

## Usage

```
/fork <additional_instructions>
```

### Examples

```
/fork now let's focus only on the streaming-timeout angle and write the fix
/fork same context, but explore the alternative design where we keep the worktree
/fork please redo the analysis assuming the user is on a free-tier plan
```

## What fork does

1. Snapshots every completed turn from the source session into a brand-new
   ``AgentSession`` row.
2. For Slack-triggered sources, posts a top-level ``🍴 Forked from ...`` message
   in the same channel to open a fresh thread, and posts a back-link
   ``🍴 Forked to <new thread>`` in the original thread so users can navigate
   the lineage.
3. Records lineage in two places:
   - ``AgentSession.forked_from_session_id`` (indexed column) → Streamlit's
     console renders the fork tree from this.
   - ``context.fork_lineage`` (JSONB list) → accumulates across chained forks
     so a fork-of-a-fork still carries its full ancestry.
4. Appends the caller's ``additional_instructions`` as the first USER turn in
   the new session — the agent picks up where the original conversation left
   off and immediately acts on the new direction.

The original session is left untouched and continues to be usable.

## Authorization

For Slack-triggered source sessions, **only the original thread participant**
(the user whose ``slack_user_id`` matches the source's
``context.slack_user_id``) is allowed to fork. Requests from a different Slack
user are rejected with a clear error.

For non-Slack source sessions (API / cron / agent-triggered / task-triggered),
no authorization gate is enforced — the trust boundary is already inside AHS.

## Procedure

When the user types ``/fork <instructions>`` in Slack:

1. Resolve the source: the current session by default.
2. Call the ``fork_session`` MCP tool with:

   ```
   fork_session(
       additional_instructions=<user's instructions after /fork>,
       target="auto",            # auto-picks slack_new_thread for Slack sources
       include_tool_calls=True,  # full-fidelity history snapshot
   )
   ```

3. The tool handles authorization, cross-link Slack posts, snapshot, and
   dispatch of the new session automatically. It returns:

   ```
   {
     "status": "ok",
     "new_session_id": "<uuid>",
     "forked_from": "<source uuid>",
     "slack_thread_url": "<link to new thread>",
     "snapshot_turn_count": <int>,
     "target": "slack_new_thread" | "headless"
   }
   ```

4. If ``status`` is ``"ok"``, do not produce any additional Slack output —
   the cross-link messages posted by ``fork_session`` are the user-facing
   confirmation. If ``status`` is ``"error"`` or ``"partial"``, surface the
   error message to the user with a brief explanation.

## When to use ``target=headless``

Pass ``target="headless"`` explicitly when forking from a non-Slack source
and the new session should not be visible in any Slack channel. The new
session is still fully usable via the AHS API / Lit console — you just
won't get a clickable Slack link in the return value.

## Caveats

- **Workspace is fresh.** The fork starts with an empty workspace (no
  worktrees, no downloaded attachments). File paths referenced in the
  replayed history won't resolve. The fork can call ``request_write_access``
  again if it needs to edit code.
- **In-progress turns are skipped.** Only messages with
  ``completion_status=SUCCESS`` are snapshotted. If the source has an
  inflight turn, it's omitted from the fork.
- **Memory is shared.** Agent / user / topic memories live in the artifact
  registry, scoped by ``(user_id, agent_name)`` — the fork inherits them
  automatically.

## Internal MCP tool

The same primitive is exposed as the ``fork_session`` MCP tool for any
agent to call programmatically. The tool's parameters and semantics are
identical to the Slack-driven usage above; the only difference is that the
calling agent picks ``target`` explicitly and consumes the returned
``new_session_id`` for follow-up work.

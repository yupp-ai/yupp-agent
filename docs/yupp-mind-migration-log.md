# yupp-mind -> yupp-agent Migration Log

This document tracks which yupp-mind PRs have been migrated into yupp-agent, and notes for resuming future migrations.

## Migration Baseline

- **Cutoff**: Thursday 2026-03-26 17:00 PT (Friday 2026-03-27 00:00 UTC)
- **yupp-mind main at migration**: commit `835972be7` (latest as of 2026-03-29)
- **yupp-agent branch**: `tw/catchup`

## Migrated PRs (chronological)

| yupp-mind PR | Title | Author | Merged (UTC) | yupp-agent commit |
|---|---|---|---|---|
| #11273 | [AHS] Send Slack courtesy messages on SIGTERM and restart | wangtian24 | 2026-03-27 06:45 | `6b5a45b` |
| #11277 | [Executor v2][A1] BCH proxy -- Go binary + build script | wangtian24 | 2026-03-27 07:43 | `684dacd` |
| #11279 | [AHS] Background STALE sweep for hung ACTIVE sessions (6h inactivity) | wangtian24 | 2026-03-27 08:04 | `7f840c8` |
| #11282 | [Executor v2][C1] codex sidecar with Python WS runner | wangtian24 | 2026-03-27 07:50 | `db34b1d` |
| #11284 | [AHS] Add yupp-agent repo support | wangtian24 | 2026-03-27 08:58 | `ffff105` |
| #11275 | feat(search): [V0] ILIKE plain-text search service | wangtian24 | 2026-03-27 00:20 | `e66a703` |
| #11285 | [TUI] Add Schedules screen (Ctrl+H) with three-pane layout | wangtian24 | 2026-03-27 17:02 | `03f1a5c` |
| #11295 | [AHS] Add POST /ahs/search endpoint and wire up search router | wangtian24 | 2026-03-27 09:09 | `1113634` |
| #11299 | [AHS] Add LINEAR_API_KEY secret to agent-harness-service | wangtian24 | 2026-03-27 17:07 | `eb0fa8a` |
| #11301 | [AHS] Remove dead warm-pool code causing ModuleNotFoundError | wangtian24 | 2026-03-27 17:14 | absorbed into #11282 |
| #11318 | [deployment] Add POSTGRES_CONNECTION_* secrets, disable auto AHS deploy | wangtian24 | 2026-03-29 18:35 | absorbed into #11299 |
| #11292 | [SAG] Fix msg_too_long: UTF-16-aware length check in buffer.py | AmaxGuan | 2026-03-27 19:52 | `4249d33` |
| #11293 | [MCP] Assign UUID per session in streamable_http handler for auditing | AmaxGuan | 2026-03-28 00:04 | `f44ff45` |

## Notes & Gotchas

### Migration method

We copy files at `origin/main` HEAD from yupp-mind rather than cherry-picking individual commits, because the two repos have diverged base SHAs so `git am --3way` often fails. This means:
- When multiple PRs touch the same file, only the final state is brought over.
- PRs #11301 and #11318 were "absorbed" into earlier commits that touched the same files (service.py and secret-env-var-map.yml respectively).

### .gitignore divergence

yupp-agent's `.gitignore` is a subset of yupp-mind's. When bringing over PRs that modify `.gitignore`, the patch won't apply cleanly. Manually add the relevant lines instead. Key additions made this round:
- `!ypl/agent_harness_service/**/*.json` (allow tracking agent config JSON files)
- `ypl/agent_harness_service/executors/bin/ahs-command-handler` (ignore compiled binary)

### agent_configs

The `ypl/agent_harness_service/deploy/agent_configs/*/config.json` files are tracked in yupp-mind via the gitignore exception above. After adding that exception to yupp-agent, these files needed to be explicitly `git add`ed.

### data/secret-env-var-map.yml

DO NOT copy this file wholesale from yupp-mind. yupp-mind's version has 218 entries for all services (backend, risk-service, cronjob, etc.) but yupp-agent's Pydantic validator only allows 4 services: `agent-harness-service`, `mcp-server`, `slack-agent-gateway`, `streamlit-server`. The yupp-agent version is a curated subset. When migrating PRs that touch this file, manually add only the new entries relevant to agent services.

### .github/workflows/ not migrated

yupp-agent has its own deployment workflows. The yupp-mind workflow changes in #11318 (`deploy-v2-deploy-only.yml`, `deploy-v2-internal-servers.yml`) were NOT migrated since yupp-agent uses different CI/CD.

### services/command-handler/ (Go binary)

PR #11277 added a new Go service (`services/command-handler/`). This was migrated in full as a new directory.

## How to Resume Migration

To bring over future yupp-mind PRs:

1. **Find new PRs since last migration**:
   ```bash
   cd /path/to/yupp-mind && git fetch origin
   git log origin/main --since="2026-03-29T18:36:00Z" --oneline --no-merges \
     | grep -iE 'AHS|SAG|MCP|streamlit|executor|TUI|agent'
   ```
   (Use the `mergedAt` timestamp of the last migrated PR as the `--since` value.)

2. **For each PR, identify changed files**:
   ```bash
   git diff-tree --no-commit-id -r --name-only <commit-sha>
   ```

3. **Copy files from yupp-mind HEAD**:
   ```bash
   git show origin/main:<filepath> > /path/to/yupp-agent/<filepath>
   ```
   Create directories as needed. Skip files that don't belong in yupp-agent (e.g., `analysis/`, backend-only code).

4. **Commit per-PR** with the original PR title and number for traceability.

5. **Update this log** with the new PRs.

### Relevant file areas to watch

- `ypl/agent_harness_service/` -- AHS core
- `ypl/mcp_server/` -- MCP server
- `ypl/slack_agent_gateway/` -- SAG
- `ypl/streamlit_server/` -- Streamlit pages
- `services/` -- Go/sidecar services
- `data/secret-env-var-map.yml` -- secret mappings

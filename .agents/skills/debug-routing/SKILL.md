---
name: debug-routing
description: Debug model routing issues using GCP logs and database queries. Use when investigating why specific models were chosen, routing failures, or unexpected routing behavior, such as a model appearing or not showing up unexpectedly, or seeing duplicate models or similar models that are not fit the context or following the user request or frontend instructions.
allowed-tools: mcp__yuppster-mcp-server__search_gcp_logs, mcp__yuppster-mcp-server__query_yuppdb, mcp__yuppster-mcp-server__query_bigquery, mcp__yuppster-mcp-server__create_yuppaste, Bash, Read, Write
---

# Model Routing Debug Guide

Use this skill when debugging routing issues - understanding why certain models were selected, investigating routing failures, or tracing the routing decision process for a specific turn.

## Important: Timestamp-Based Queries

**Prefer using specific timestamp ranges** over `hours_back` to reduce impact on our logging API quota:

```
# Preferred: Use timestamp range when you know the approximate time
search_gcp_logs(
    query='textPayload:"{turn_id} routing" timestamp>="2026-01-25T17:40:00Z" timestamp<="2026-01-25T17:45:00Z"',
    max_results=100
)

# Fallback: Use hours_back only when time is unknown
search_gcp_logs(
    query='textPayload:"{turn_id} routing"',
    hours_back=2,
    max_results=100
)
```

When you have a turn_id, first query the database for `created_at` to get the timestamp, then use that in your log search.

```sql
-- Example query to get the timestamp for a turn
SELECT created_at FROM turns WHERE turn_id = '{turn_id}';
```

## Quick Reference: The Best Log Search Patterns

**The most effective way to find all routing-related logs for a turn:**

```
search_gcp_logs(
    query='textPayload:"{turn_id} routing"',
    hours_back=2,
    max_results=100
)
```

**You can also search by chat_id or user_id:**

```
# Search by chat_id
search_gcp_logs(
    query='textPayload:"{chat_id} routing"',
    hours_back=2,
    max_results=100
)

# Search by user_id
search_gcp_logs(
    query='textPayload:"{user_id} routing"',
    hours_back=2,
    max_results=100
)
```

These patterns capture all routing-related log entries, including:
- Prompt labeling results (categories, tags, safety)
- Model feature building
- Router chain processing (filters, scorers, choosers)
- Final routing decisions
- Debug information

## Routing System Overview

Routing decides which models to show users. It happens at every "round" (new turn or "Show More AIs"). The process:

1. **Request** → Contains user prompt, intent (NEW_CHAT/NEW_TURN/SHOW_ME_MORE), model selectors, filters
2. **Prompt Labeling** → Parallel LLM labelers extract categories (topic, reasoning, online, safety, etc.)
3. **Feature Building** → Collect model features (static + contextual)
4. **Category Resolution** → Resolve conflicts between user selections, inherited models, and detected categories
5. **Router Chain** → Filters → Early Choosers → Promotions → Ranking → Choosers → Fallback
6. **Post Processing** → Generate reasons, store to routing_info table

## Entry Points (chat.py)

| Function | Intent | Description |
|----------|--------|-------------|
| `start_new_turn()` | NEW_CHAT, NEW_TURN | Creates chat (if NEW_CHAT), creates turn, user message, calls `select_models()`, stores routing_info |
| `show_more()` | SHOW_ME_MORE | Validates turn exists, reuses existing user message content, calls `select_models()` |

Both functions call `select_models()` which runs the full routing pipeline. Key differences:
- **NEW_CHAT**: Creates new Chat, Turn, and ChatMessage records; `turns_context` is empty
- **NEW_TURN**: Looks up existing chat; `turns_context` loaded from DB with past turns info
- **SHOW_ME_MORE**: Reuses same turn's prompt from DB; categories reused from current turn's first routing

**Key log messages:**
- `"Created new chat"` - Chat record created (NEW_CHAT only)
- `"Created new turn"` - Turn record created
- `"Completed start_new_turn"` - Full flow completed with routing response

## Category & Model Resolution (select_models.py)

The `_resolve_models_and_categories()` function handles the critical resolution logic.

### Sources of Categories
1. **Labeler results** - From running prompt classifiers (topic, category, online, reasoning, safety, image-gen, yapp)
2. **Model filters** - IMAGE, PDF from attachments; IMAGE_GENERATION from UI toggle
3. **Inherited categories** - From previous turns in the same chat

### Key Resolution Logic
- **IMAGE_GEN_STOP** removes inherited `IMAGE_GEN`/`IMAGE_EDIT` categories (allows switching back to text mode)
- **Image edit** requires previous turn to have image gen (otherwise `IMAGE_EDIT` is stripped)
- **SVG/HTML generation** adds `coding` category automatically
- **User-selected all-image-gen models** → adds `IMAGE_GEN` category
- Final result: `resolve_categories_conflicts()` produces `resolved_categories`

### Model Inheritance
- **user_selected_models**: From picker selectors, persists across turns until user changes them
- **inherited_models**: From `turns_context.infer_inherited_models()` - previous turn's models unless PREF/NOPE happened
- Both are filtered by `model_has_abilities()` against resolved_categories (models without required abilities are dropped)

### Short-Circuit Case
If user selected + inherited models already satisfy all ability requirements AND have enough models (≥ num_models * 2), the router chain is **skipped entirely**. Look for `"Model routing: short-circuiting"` in logs.

## What's Stored in routing_info

The `create_routing_info()` function in `select_models.py` builds the RoutingInfo record. See `ypl/db/routing_info.py` for the full schema.

**Key columns for debugging:**
- `categories` vs `resolved_categories` - Compare to see what resolution changed
- `routing_outcome` - JSON with full model selection details including `selection_criteria` (USER_SELECTED, INHERITED, PROMOTED, etc.)
- `selector` - What user explicitly selected in the picker
- `model_filters` - What capability filters were active (IMAGE, PDF, etc.)
- `left_model_id` / `right_model_id` - The primary models chosen

## Router Chain Detailed Stages

The router chain in `router_chain.py` processes models through these stages in order:

### Stage 1: Filter Stage
Remove models that are definitely not usable for this turn:

| Filter | Log Marker | Description |
|--------|------------|-------------|
| Ability Filters | (varies) | Image, PDF, Online, ImageGen based on categories |
| Specialized Model Exclusion | `-exclSpec(...)` | Exclude specialized models not needed |
| ContextLengthFilter | | Prompt too long for model's context window |
| RateLimitFilter | | Provider is rate limited |
| Exclude Shown Models | `-exclShownModels` | Models already shown in this turn |
| Exclude Shown Taxonomies | `-exclShownTaxos` | Taxonomies already shown |
| Exclude Shown Families | `-exclShownFamilies` | Families shown (only for SHOW_ME_MORE) |
| CostManagementFilter | | User over budget (skipped for onboarding/yuppsters) |

### Stage 2: Early Choosers
Select models with priority before main choosers:

| Chooser | Log Marker | Description |
|---------|------------|-------------|
| GuaranteeUserSelectedModels | | User's explicit model selections (always included) |
| OptimizeCostForUserSelectedModels | | Pick cheapest provider for user-selected models |
| Dedupe for User Selected | `-dupsForUserSelected` | Remove duplicates |
| GuaranteeInheritedModels | | Models from past turns (not on first turn) |
| OptimizeCostForInheritedModels | | Pick cheapest provider for inherited |
| Dedupe for Inherited | `-dupsForInherited` | Remove duplicates |

### Stage 3: Promotions & Yapps (skipped for onboarding)

| Module | Description |
|--------|-------------|
| **PromotionPacer** | Check promotions, pick one if eligible (see `debug-model-promotion` skill) |
| ExcludeOtherCloakedModels | `-exclOtherCloaked` - Remove other cloaked models if one was chosen |
| Dedupe for Promoted | `-dupsForPromoted` |
| **YappChooser** | Check if Yapp agents should be used (not for SHOW_ME_MORE) |
| ExcludeUnchosenYappModels | Remove Yapp models not chosen |
| MaxModelFilter | Enforce user's max model quota |

### Stage 4: Ranking Stage

| Module | Description |
|--------|-------------|
| DedupeByTaxonomyType | Pick one provider per taxonomy (prefer low-cost providers like AWS/Google/Azure) |
| ScoreRerankerV2 | Score all remaining models for weighted sampling |
| VendorReranker | Special reranker for eval vendors (paid raters) |

### Stage 5: Main Choosers

| Chooser | Description |
|---------|-------------|
| **FirstPrimaryChooser** | Left side model - must be PRO and STRONG (relaxed if special abilities needed) |
| **SecondPrimaryChooser** | Right side model - must be STRONG, FAST, REASONING, or LIVE |
| RemainingPrimaryChooser | Additional models if num_models > 2 |

### Stage 6: Final Stage

| Module | Description |
|--------|-------------|
| PositionMatchReranker | Reorder for consistent positioning (follow-up turns only) |
| FallbackChooser | Choose fallback models for each primary |
| RoutingDecisionLogger | Log final routing decision with all debug info |

## Investigation Methodology

### Step 1: Identify the Turn

Get the turn_id you want to investigate. If you have a chat_id:

```sql
SELECT turn_id, sequence_id, created_at
FROM turns
WHERE chat_id = '<chat_id>'
ORDER BY sequence_id DESC
LIMIT 10
```

### Step 2: Get Routing Info from Database

Query the `routing_info` table for the turn's routing decisions:

```sql
SELECT 
    ri.routing_info_id,
    ri.turn_id,
    ri.categories,
    ri.resolved_categories,
    ri.routing_outcome,
    ri.created_at
FROM routing_info ri
WHERE ri.turn_id = '<turn_id>'
ORDER BY ri.created_at
```

**Note**: A single turn may have multiple `routing_info` entries if user clicked "Show More AIs".

### Step 3: Search GCP Logs for Routing Details

Use the turn_id to search for all routing logs:

```
search_gcp_logs(
    query='textPayload:"{turn_id} routing"',
    hours_back=2,
    max_results=100
)
```

For more specific searches:

**Find prompt labeling results:**
```
search_gcp_logs(
    query='textPayload:"{turn_id}" textPayload:"categories"',
    hours_back=2,
    max_results=50
)
```

**Find router chain processing:**
```
search_gcp_logs(
    query='textPayload:"{turn_id}" textPayload:"router"',
    hours_back=2,
    max_results=50
)
```

**Find model selection/exclusion:**
```
search_gcp_logs(
    query='textPayload:"{turn_id}" textPayload:"selected" OR textPayload:"excluded"',
    hours_back=2,
    max_results=50
)
```

### Step 4: Analyze Log Results

When results are large, save to file and analyze:

**Chronological view:**
```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message | tostring | .[0:500])"' | sort
```

**Search for specific model:**
```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message)"' | grep -i "<model_name>"
```

**Find filter/exclusion reasons:**
```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message)"' | grep -i "filter\|exclude\|reject"
```

## Database Queries for Routing

### Power Query: Full Chat Debug View

This comprehensive query returns all messages for a chat with their metadata, routing info, model details, and evaluations - everything needed to debug a chat's routing history:

```sql
SELECT 
    -- Turn info
    t.sequence_id AS turn_seq,
    t.created_at AS turn_created,
    
    -- Message info
    cm.turn_sequence_number AS msg_seq,
    cm.message_type,
    LEFT(cm.content, 256) AS content,
    cm.completion_status,
    cm.model_error_type,
    cm.streaming_metrics,
    
    -- Model info
    lm.name AS model_name,
    lm.status AS model_status,
    lmt.language_model_taxonomy_id AS taxonomy_id,
    p.name AS provider_name,
    
    -- Evaluation info
    e.eval_type,
    me.score AS eval_score,
    
    -- Routing info (all columns)
    ri.routing_info_id,
    ri.intent,
    ri.modality,
    ri.categories,
    ri.resolved_categories,
    ri.routing_outcome,
    ri.selector,
    ri.model_filters,
    
    -- IDs for further lookup
    t.chat_id,
    t.turn_id,
    cm.message_id,
    lm.language_model_id

FROM chat_messages cm 
JOIN turns t ON cm.turn_id = t.turn_id
LEFT JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
LEFT JOIN language_model_taxonomy lmt ON lmt.language_model_taxonomy_id = lm.taxonomy_id 
LEFT JOIN providers p ON p.provider_id = lm.provider_id
LEFT JOIN routing_info ri ON ri.turn_id = cm.turn_id
LEFT JOIN message_evals me ON me.message_id = cm.message_id
LEFT JOIN evals e ON e.eval_id = me.eval_id

WHERE t.chat_id = '<chat_id>'
ORDER BY t.sequence_id, cm.turn_sequence_number
```

**What this query shows:**
- Every message in the chat ordered by turn and message sequence
- Model used for each assistant message with provider info
- Routing decisions (`categories`, `resolved_categories`, `routing_outcome`)
- Any evaluations (PREF/NOPE) on messages
- Streaming metrics and error info for debugging failures

**Note:** May return duplicate rows when a turn has multiple `routing_info` entries (from "Show More AIs") or multiple evaluations.

## Common Debugging Scenarios

### Scenario: Model Not Selected

1. Check if model is in `resolved_categories` abilities
2. Search logs for model name to find exclusion reason
3. Check filters: context length, rate limit, already shown, cost

```
search_gcp_logs(
    query='textPayload:"{turn_id}" textPayload:"{model_name}"',
    hours_back=2,
    max_results=50
)
```

### Scenario: Wrong Category Detected

1. Query `routing_info.categories` to see raw labeler outputs
2. Search logs for "categories" to see labeling process
3. Check `resolved_categories` for final resolution

### Scenario: Unexpected Model Selection

1. Check for active promotions in `model_promotions` table
2. Check for user selections in routing request
3. Check inherited models from previous turns

```sql
SELECT * FROM model_promotions 
WHERE status = 'ACTIVE' 
  AND (end_date IS NULL OR end_date > NOW())
```

### Scenario: Routing Failure/Error

1. Search for ERROR severity logs around the turn
2. Check for timeout or provider errors

```
search_gcp_logs(
    query='textPayload:"{turn_id}" severity="ERROR"',
    hours_back=2,
    max_results=50
)
```

---

## Log Field Reference

Key fields in routing logs:

| Field | Description |
|-------|-------------|
| `timestamp` | When logged |
| `severity` | INFO, WARNING, ERROR |
| `message` | Log content (often JSON with routing details) |
| `trace` | Request trace ID for correlation |
| `labels.instanceId` | Container instance for correlation |

## Code References

Key files for understanding routing logic:

| File | Purpose |
|------|---------|
| `ypl/backend/llm/chat.py` | Entry points: `start_new_turn()`, `show_more()` |
| `ypl/backend/llm/routing/select_models.py` | Main `select_models()`, `_resolve_models_and_categories()`, `create_routing_info()` |
| `ypl/backend/llm/routing/router_chain.py` | Router chain assembly - shows all stages in order |
| `ypl/backend/llm/routing/router_state.py` | RouterState data structure with selected/excluded models |
| `ypl/backend/llm/routing/modules/` | Individual router modules (filters, choosers, etc.) |
| `ypl/backend/llm/routing/routing_common.py` | `resolve_categories_conflicts()`, `resolve_model_filter_conflicts()` |
| `ypl/backend/llm/turns_context.py` | TurnsContext with past turns info, inherited models/categories |
| `ypl/backend/llm/labeler/` | Prompt labelers (category, online, reasoning, etc.) |
| `ypl/backend/llm/constants.py` | Category constants (IMAGE_CATEGORY, ONLINE_CATEGORY, etc.) |

**Database schemas** (for table structure details):
| File | Tables |
|------|--------|
| `ypl/db/routing_info.py` | `routing_info` |
| `ypl/db/chats.py` | `chats`, `turns`, `chat_messages` |
| `ypl/db/language_models.py` | `language_models`, `language_model_taxonomy`, `providers` |

**Related skill:** For promotion/cloaked testing debugging, see the `debug-model-promotion` skill.

## Tips

1. **Start with `{turn_id} routing`** - This single search pattern usually reveals the full routing story.

2. **Check multiple routing_info entries** - A turn with "Show More AIs" will have multiple entries.

3. **Correlate with chat_messages** - Compare routing decisions with actual served models.

4. **Look for debug info** - Routing logs include per-model debug info showing each RouterModule's effect.

5. **Check timing** - If logs are sparse, the turn might have used cached routing or hit an early exit.

6. **Use time windows** - Narrow down with timestamp filters if there are many logs.

## Preserving Evidence with Yuppaste

When investigating issues that may lead to a PR fix, **preserve critical evidence** using the `create_yuppaste` MCP tool. It's best to create a separate paste for each distinct piece of evidence (e.g., one for logs, another for database results). This creates shareable links that can be included in PR descriptions.

### What to Preserve

- **Routing logs**: Full routing decision chain for the problematic turn
- **Database state**: `routing_info` records, model configurations, promotion settings
- **Category resolution**: How categories were resolved and conflicts handled
- **Filter/exclusion reasons**: Why certain models were filtered out

### How to Use

```
create_yuppaste(
    content="<formatted logs or query results>",
    name="Routing Debug: <turn_id or description>"
)
```

The tool returns a go-link (e.g., `http://go/p/<uuid>`) that you can include in PR descriptions. Note that the content size is limited to 10MB.

### PR Description Format

When creating a PR to fix a routing issue, include:

```markdown
## Investigation Evidence

- Routing logs: http://go/p/<uuid1>
- routing_info state: http://go/p/<uuid2>
- Model filter analysis: http://go/p/<uuid3>
```

This provides reviewers with full context without cluttering the PR description.

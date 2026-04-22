---
name: debug-model-promotion
description: Debug model promotion and cloaked testing issues. Use when investigating why a promoted model is or isn't appearing, traffic pacing issues, or cloaked testing behavior.
allowed-tools: mcp__agcouch-mcp-server__search_gcp_logs, mcp__agcouch-mcp-server__query_yuppdb, mcp__agcouch-mcp-server__query_bigquery, mcp__agcouch-mcp-server__add_artifact, Bash, Read, Write
---

# Model Promotion Debug Guide

Use this skill when debugging promotion issues - understanding why a promoted or cloaked model is or isn't appearing, investigating traffic pacing, or tracing promotion eligibility.

## Important: Timestamp-Based Queries

**Prefer using specific timestamp ranges** over `hours_back` to reduce impact on our logging API quota:

```
# Preferred: Use timestamp range when you know the time
search_gcp_logs(
    query='textPayload:"PromotionPacer" textPayload:"{turn_id}" timestamp>="2026-01-25T17:40:00Z" timestamp<="2026-01-25T17:45:00Z"',
    max_results=50
)

# Fallback: Use hours_back only when time is unknown
search_gcp_logs(
    query='textPayload:"PromotionPacer" textPayload:"{turn_id}"',
    hours_back=2,
    max_results=50
)
```

When you have a turn_id, first query the database for `created_at` to get the timestamp, then use that in your log search. You can use a query like this:

```sql
-- Example query to get the timestamp for a turn
SELECT created_at FROM turns WHERE turn_id = '{turn_id}';
```

## Quick Reference: Promotion Log Search

**Search for PromotionPacer logs for a specific turn:**
```
search_gcp_logs(
    query='textPayload:"PromotionPacer" textPayload:"{turn_id}"',
    hours_back=2,
    max_results=50
)
```

**Search for all promotion skip reasons:**
```
search_gcp_logs(
    query='textPayload:"PromotionPacer" textPayload:"skipping"',
    hours_back=2,
    max_results=50
)
```

## Promotion System Overview

Promotions allow boosting certain models' appearance in routing. The `PromotionPacer` module in the router chain handles this.

### Promotion Types

| Type | Description |
|------|-------------|
| **Regular Promotion** | Normal model promotion with configurable probability |
| **Cloaked Testing** | Hidden A/B testing for model evaluation (blind to users) |

### Where Promotions Fit in Routing

Promotions are handled in **Stage 3** of the router chain (after filters and early choosers):
1. `PromotionPacer` - Check promotions, pick one if eligible
2. `ExcludeOtherCloakedModels` - Remove other cloaked models if one was chosen
3. Dedupe for Promoted

**Important:** Promotions are skipped entirely for onboarding users.

## Pacing Methods

### 1. Traffic-Based Pacing (Goal-Oriented)

Used when `pace_by_traffic_goal` is enabled. Estimates current eval traffic and adjusts weights to meet daily eval goals.

**Traffic Pacing Statuses:**
| Status | Meaning |
|--------|---------|
| `ON_TRACK` | Achieved evals match expected rate |
| `BEHIND` | Behind schedule, will increase promotion weight |
| `OVERSHOOT` | Exceeded max_overshoot_ratio, model excluded |

**Key log messages to search for:**
```
search_gcp_logs(
    query='textPayload:"PromotionPacer" textPayload:"Traffic-based"',
    hours_back=2,
    max_results=50
)
```

### 2. Probability-Based Pacing

Fallback when traffic-based pacing doesn't pick a model. Uses age-in-window decay to reduce promotion probability over time.

**Key log messages:**
```
search_gcp_logs(
    query='textPayload:"PromotionPacer" textPayload:"Probability-based"',
    hours_back=2,
    max_results=50
)
```

## Promotion Log Patterns

**Common log messages and their meanings:**

| Log Message Pattern | Meaning |
|---------------------|---------|
| `skipping - no promotables` | No active promotions configured |
| `skipping - no more quota to choose` | Already have enough primary models chosen |
| `skipping - already have promoted models` | A promotion was already chosen earlier |
| `skipping - no eligible promotables` | Promotions exist but none eligible for this request |
| `skipping - no promotable candidates after internal name picking` | Promotable models not in candidate set |
| `Traffic-based: evaluating N promotable candidates` | Starting traffic-based evaluation |
| `Traffic-based: final pick: MODEL_NAME` | Traffic pacing chose a model |
| `Traffic-based: final pick: NONE` | Traffic pacing didn't choose (all on-track or overshoot) |
| `traffic-based promotion failed, trying probability-based` | Falling back to probability pacing |
| `Probability-based: [MODEL] won sampling` | Probability pacing chose a model |
| `Probability-based: no promotion got sampled` | Random sampling didn't pick any promotion |

## Promotion Eligibility

A promotion is eligible when:
1. It's active (status = 'ACTIVE')
2. Current time is within start_date and end_date
3. It's the first turn or follow-up turn (not SHOW_ME_MORE)
4. User is not in onboarding
5. For vendors: `allow_promotion_to_vendors` setting must be enabled
6. The model hasn't been downvoted or lost a battle in the same chat
7. The model is in the current candidate set (not filtered out earlier by ability, rate limit, etc.)

## Database Queries

**Check active promotions:**
```sql
SELECT 
    mp.model_promotion_id,
    mp.status,
    mp.start_date,
    mp.end_date,
    mp.language_model_id,
    mp.taxonomy_id,
    mp.model_selector,
    lm.internal_name,
    lmt.model_family
FROM model_promotions mp
LEFT JOIN language_models lm ON mp.language_model_id = lm.language_model_id
LEFT JOIN language_model_taxonomy lmt ON mp.taxonomy_id = lmt.language_model_taxonomy_id
WHERE mp.status = 'ACTIVE'
  AND (mp.start_date IS NULL OR mp.start_date <= NOW())
  AND (mp.end_date IS NULL OR mp.end_date > NOW())
ORDER BY mp.created_at DESC
```

**Check cloaked testing:**
```sql
SELECT 
    ct.cloaked_testing_id,
    ct.status,
    ct.start_date,
    ct.end_date,
    ct.eval_goal,
    lm.internal_name,
    lmt.model_family
FROM cloaked_testing ct
LEFT JOIN language_models lm ON ct.language_model_id = lm.language_model_id
LEFT JOIN language_model_taxonomy lmt ON ct.taxonomy_id = lmt.language_model_taxonomy_id
WHERE ct.status = 'ACTIVE'
ORDER BY ct.created_at DESC
```

**Check if a model was promoted in a turn:**
```sql
SELECT 
    ri.routing_outcome,
    ri.created_at
FROM routing_info ri
WHERE ri.turn_id = '<turn_id>'
-- Then examine routing_outcome JSON for selection_criteria containing "PROMOTED" or "CLOAKED"
```

## Debugging Scenarios

### Scenario: Promotion Not Appearing

1. **Check if promotion is active:**
   ```sql
   SELECT * FROM model_promotions WHERE status = 'ACTIVE'
   ```

2. **Search logs for eligibility check:**
   ```
   search_gcp_logs(
       query='textPayload:"PromotionPacer" textPayload:"skipping"',
       hours_back=2,
       max_results=50
   )
   ```

3. **Check if model was filtered before PromotionPacer:**
   Look for the model in earlier filter stages (ability, context length, rate limit, etc.)
   ```
   search_gcp_logs(
       query='textPayload:"{turn_id}" textPayload:"{model_name}"',
       hours_back=2,
       max_results=50
   )
   ```

4. **For traffic-based: check if OVERSHOOT:**
   ```
   search_gcp_logs(
       query='textPayload:"PromotionPacer" textPayload:"OVERSHOOT"',
       hours_back=2,
       max_results=50
   )
   ```

### Scenario: Promotion Appearing Too Often/Rarely

1. **Check traffic pacing status:**
   ```
   search_gcp_logs(
       query='textPayload:"PromotionPacer" textPayload:"Traffic-based"',
       hours_back=2,
       max_results=100
   )
   ```

2. **Look for achieved vs expected evals in logs:**
   Search for `achieved` and `expected` in PromotionPacer logs

3. **Check promotion settings:**
   - `expected_daily_evals` - Target evals per day
   - `max_overshoot_ratio` - When to stop promoting
   - `show_probability` - For probability-based pacing

### Scenario: Cloaked Model Behavior

1. **Check if cloaked testing is active:**
   ```sql
   SELECT * FROM cloaked_testing WHERE status = 'ACTIVE'
   ```

2. **Search for cloaked model exclusions:**
   ```
   search_gcp_logs(
       query='textPayload:"exclOtherCloaked"',
       hours_back=2,
       max_results=50
   )
   ```

3. **Check if cloaked model was already shown in chat:**
   The `allow_only_one_per_chat` setting may limit cloaked models to one per chat

## Code References

| File | Purpose |
|------|---------|
| `ypl/backend/llm/routing/modules/promotion_pacer.py` | Main PromotionPacer logic, traffic/probability pacing |
| `ypl/backend/llm/routing/promotion_common.py` | Promotion eligibility checks, PromotableItem |
| `ypl/backend/llm/routing/promotions_db_helper.py` | Database queries for promotions |
| `ypl/backend/llm/routing/router_chain.py` | Where PromotionPacer fits in the chain |

**Database schemas:**
| File | Tables |
|------|--------|
| `ypl/db/promotions.py` | `model_promotions`, `cloaked_testing` |

## Tips

1. **Start with PromotionPacer logs** - Search for `"PromotionPacer"` with the turn_id to see all promotion decisions.

2. **Check "skipping" reasons** - Most promotion issues are explained by skip messages.

3. **Traffic vs Probability** - Traffic-based runs first; probability-based is fallback.

4. **OVERSHOOT means excluded** - If a model is OVERSHOOT, it won't be promoted until traffic catches up.

5. **Cloaked = one per chat** - By default, only one cloaked model per chat to avoid bias.

6. **Check earlier filters** - A model must survive all filters before PromotionPacer even sees it.

## Preserving Evidence with Artifact

When investigating issues that may lead to a PR fix, **preserve critical evidence** using the `add_artifact` MCP tool (set `artifact_type="TEXT"`). It's best to create a separate artifact for each distinct piece of evidence (e.g., one for logs, another for database results). This creates shareable links that can be included in PR descriptions.

### What to Preserve

- **PromotionPacer logs**: Full decision chain showing eligibility checks and pacing decisions
- **Promotion configuration**: Active promotions, cloaked testing settings from database
- **Traffic pacing state**: Achieved vs expected evals, overshoot status
- **Routing context**: How the model was filtered before reaching PromotionPacer

### How to Use

```
add_artifact(
    artifact_type="TEXT",
    title="Promotion Debug: <model_name or description>",
    content="<formatted logs or query results>",
)
```

The tool returns a go-link (e.g., `https://artifacts.agcouch.com/artifacts/<uuid>`) that you can include in PR descriptions. Note that the content size is limited to 10MB.

### PR Description Format

When creating a PR to fix a promotion issue, include:

```markdown
## Investigation Evidence

- PromotionPacer logs: http://go/p/<uuid1>
- Promotion config state: http://go/p/<uuid2>
- Traffic pacing analysis: http://go/p/<uuid3>
```

This provides reviewers with full context without cluttering the PR description.

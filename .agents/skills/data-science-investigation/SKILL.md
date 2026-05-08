---
name: data-science-investigation
description: Data science investigation and analysis on Yupp production data. Use for leaderboard ranking analysis (why does model A rank above B?), engagement and usage analytics, geographic/demographic breakdowns, feature impact analysis, model performance deep-dives, and any ad-hoc data questions requiring narrative + evidence.
allowed-tools: mcp__harness__query_bigquery, mcp__harness__query_yuppdb, mcp__harness__add_artifact, Bash, Read, Write, Glob, Grep, Task, Skill
---

# Data Science Investigation Guide

Use this skill for analytical investigations on Yupp production data: understanding leaderboard rankings, analyzing engagement patterns, evaluating feature impact, and producing evidence-backed narratives with visualizations.

## TL;DR

- **Use `/query-leaderboard` skill** first for leaderboard-related questions — it provides pre-computed rankings, battle analysis, and head-to-head stats
- **Use `/fetch-from-db` skill** first when querying BigQuery or the production DB directly — it provides schema context
- **BigQuery** (`query_bigquery`) is the workhorse for ad-hoc SQL on raw battle/eval/user data
- **Leaderboard API** (via `curl`) provides pre-computed analysis with 50+ filter dimensions
- **Output as Jupyter notebook** for rich analysis with charts and narrative; use markdown tables for quick answers
- **Always narrate findings** — raw numbers without context are not useful

---

## Investigation Methodology

### Step 1: Clarify the Question

Before querying anything, articulate:
- **What** exactly is being asked? (e.g., "Why does GPT-4o rank above Claude 3.5 Sonnet?")
- **What would a convincing answer look like?** (e.g., "GPT-4o wins more in Coding and Math, which make up 40% of battles")
- **What data would support or refute the hypothesis?**

### Step 2: Gather Data

Use the appropriate data source for each question:

| Question Type | Best Data Source | Tool |
|--------------|-----------------|------|
| Model rankings, scores, confidence intervals | Leaderboard API `/leaderboard` | `curl` via Bash |
| Head-to-head analysis, win rates, demographics | Leaderboard API `/leaderboard/model_analysis` | `curl` via Bash |
| Specific battle records | Leaderboard API `/leaderboard/matches` | `curl` via Bash |
| Rating history over time | Leaderboard API `/leaderboard/history` | `curl` via Bash |
| Ad-hoc SQL on raw battles/evals | BigQuery `prodyuppdb_public` | `query_bigquery` |
| User engagement, retention, signups | BigQuery `prodyuppdb_public` | `query_bigquery` |
| Real-time production data | Production Postgres | `query_yuppdb` |
| Chat content, message details | Production Postgres | `query_yuppdb` |

**Important**: Before querying the leaderboard API, invoke `/query-leaderboard` to load the API reference. Before querying BigQuery or Postgres, invoke `/fetch-from-db` to load schema context.

### Step 3: Analyze and Visualize

Produce clear artifacts:
- **Markdown tables** for quick comparisons (inline in conversation)
- **Jupyter notebooks** for rich multi-step analysis with charts
- **Artifact** for sharing detailed evidence

### Step 4: Narrate Findings

Every investigation should produce a narrative:
1. **Question**: What was asked
2. **Key Finding**: 1-2 sentence answer
3. **Evidence**: Tables, charts, and numbers supporting the finding
4. **Caveats**: Sample sizes, time ranges, confounders
5. **Recommendations**: If applicable

---

## Common Investigation Patterns

### Pattern A: Why Does Model X Rank Above Model Y?

This is the most common question. Follow this sequence:

**1. Get current rankings for both models**

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{"collect_stats": true, "hide_cloaked": false}' | jq '.models[] | select(.model_rating.taxonomy_label | test("Model X|Model Y")) | {label: .model_rating.taxonomy_label, rating: .model_rating.rating, rank: .model_rating.rank, wins: .model_rating.wins, losses: .model_rating.losses, win_rate: .model_rating.win_rate}'
```

**2. Get battle analysis for both models** (call twice, once per model)

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard/model_analysis" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{"taxo_label": "Model X", "top_n": 20, "min_votes": 100}'
```

**3. Compare across dimensions**, looking for divergence:
- **Head-to-head record**: What happens when they directly face each other?
- **Category breakdown**: Does one dominate in high-volume categories?
- **Opponent strength**: Is one facing tougher opponents on average?
- **Blinded vs unblinded**: Does brand recognition affect results?
- **Speed metrics**: Is latency/TPS correlated with wins?
- **Temporal trends**: Has one been improving/declining recently?
- **Demographic splits**: Different results in different geographies?
- **Eval notes**: What do evaluators say about each model's wins/losses?

**4. Compute head-to-head directly** via BigQuery:

Use the `leaderboard.battles_production` view — it pre-joins evals, messages, models, and taxonomy with winner/loser columns already resolved. See `queries/leaderboard/` for view definitions.

```sql
SELECT
  b.winner_taxo_label AS winner,
  b.loser_taxo_label AS loser,
  COUNT(*) AS battles,
  b.prompt_category_name AS category
FROM leaderboard.battles_production b
WHERE b.winner_taxo_label IN ('Model X', 'Model Y')
  AND b.loser_taxo_label IN ('Model X', 'Model Y')
GROUP BY winner, loser, category
ORDER BY battles DESC
```

### Pattern B: Engagement Analysis

**User engagement by geography:**

```sql
SELECT
  u.country_code,
  COUNT(DISTINCT u.user_id) AS users,
  COUNT(DISTINCT t.turn_id) AS turns,
  ROUND(COUNT(DISTINCT t.turn_id) * 1.0 / COUNT(DISTINCT u.user_id), 1) AS turns_per_user,
  COUNT(DISTINCT e.eval_id) AS evals,
  ROUND(COUNT(DISTINCT e.eval_id) * 1.0 / COUNT(DISTINCT t.turn_id), 2) AS eval_rate
FROM prodyuppdb_public.users u
JOIN prodyuppdb_public.turns t ON u.user_id = t.creator_user_id
LEFT JOIN prodyuppdb_public.evals e ON t.turn_id = e.turn_id
WHERE t.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY u.country_code
HAVING COUNT(DISTINCT u.user_id) >= 10
ORDER BY COUNT(DISTINCT u.user_id) DESC
LIMIT 30
```

**Feature adoption (e.g., QuickTake):**

QuickTake is tracked via `chat_messages.message_type = 'QUICK_RESPONSE_MESSAGE'` (or `evals.eval_type = 'QUICK_TAKE'`).

```sql
SELECT
  DATE(cm.created_at) AS day,
  COUNT(DISTINCT cm.message_id) AS total_messages,
  COUNT(DISTINCT CASE WHEN cm.message_type = 'QUICK_RESPONSE_MESSAGE' THEN cm.message_id END) AS quicktake_messages,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN cm.message_type = 'QUICK_RESPONSE_MESSAGE' THEN cm.message_id END)
    / COUNT(DISTINCT cm.message_id), 1) AS quicktake_pct
FROM prodyuppdb_public.chat_messages cm
WHERE cm.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND cm.message_type IN ('ASSISTANT_MESSAGE', 'QUICK_RESPONSE_MESSAGE')
GROUP BY day
ORDER BY day DESC
```

### Pattern C: Model Performance Over Time

**Rating trajectory:**

Use the leaderboard history API:
```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard/history" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{"taxonomy_id": "<uuid>", "num_results": 200}'
```

**Win rate trend from raw data:**

```sql
SELECT
  DATE(b.eval_created_at) AS day,
  'Model X' AS model,
  COUNT(*) AS total_battles,
  COUNTIF(b.winner_taxo_label = 'Model X') AS wins,
  ROUND(100.0 * COUNTIF(b.winner_taxo_label = 'Model X') / COUNT(*), 1) AS win_rate
FROM leaderboard.battles_production b
WHERE (b.winner_taxo_label = 'Model X' OR b.loser_taxo_label = 'Model X')
  AND b.eval_created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
GROUP BY day
ORDER BY day
```

### Pattern D: Category-Specific Analysis

**Which models dominate each category:**

```sql
SELECT
  b.prompt_category_name AS category,
  b.winner_taxo_label AS model,
  COUNT(*) AS wins,
  RANK() OVER (PARTITION BY b.prompt_category_name ORDER BY COUNT(*) DESC) AS category_rank
FROM leaderboard.battles_production b
WHERE b.eval_created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY category, model
QUALIFY category_rank <= 5
ORDER BY category, category_rank
```

### Pattern E: Eval Quality and Notes

**Eval note distribution for a model:**

The `/leaderboard/model_analysis` response includes `eval_notes_stats` and `eval_notes_win_lift` — showing what evaluators said and how each note correlates with win rate. Use `include_eval_notes: true` on the `/leaderboard` endpoint for eval notes across all models (expensive).

---

## Output Formats

### Quick Answer (Markdown Tables)

For simple questions, output directly in the conversation:

```markdown
| Model | Score | Rank | Wins | Losses | Win Rate |
|-------|-------|------|------|--------|----------|
| GPT-4o | 1523.4 | 1 | 4521 | 2103 | 68.3% |
| Claude 3.5 Sonnet | 1498.7 | 3 | 3890 | 2450 | 61.4% |
```

### Jupyter Notebook (Rich Analysis)

For complex, multi-step analyses, generate a notebook:

1. **Create the notebook** in `notebooks/` with a descriptive name
2. **Structure cells as**:
   - Cell 1 (markdown): **Executive Summary** — a concise overview at the top with: the question being investigated, 3-5 key findings as bullet points (each with a one-line rationale/evidence), and any actionable recommendations. This lets readers get the full picture without scrolling through the analysis.
   - Cell 2 (markdown): Question and detailed context
   - Cell 3 (code): Data loading (imports, API calls or BigQuery queries)
   - Cell 4 (code): Data processing and transformation
   - Cell 5 (code): Visualization (plotly or matplotlib)
   - Cell 6 (markdown): Findings narrative
   - Repeat cells 3-6 for each analysis dimension
   - Final cell (markdown): Summary and recommendations
3. **IMPORTANT: Always execute the notebook** to produce a viewable artifact with populated outputs and rendered charts. Without this step the notebook is just source code:
   ```bash
   poetry run jupyter nbconvert --execute --to notebook --inplace notebooks/<name>.ipynb
   ```
   If execution fails, fix the failing cell and re-run. The notebook must compile cleanly.
4. **Generate a companion HTML file** alongside the notebook for easy viewing. Use `--no-input` to strip code cells, leaving only markdown narrative and chart outputs:
   ```bash
   poetry run jupyter nbconvert --to html --no-input notebooks/<name>.ipynb
   ```
   This produces `notebooks/<name>.html` alongside the notebook — an interactive, self-contained report that can be opened in any browser without Jupyter.
5. **Upload the HTML to artifact** for easy sharing. Use `content_type="text/html"` so it renders correctly:
   ```python
   add_artifact(
       artifact_type="TEXT",
       title="DS Report: <brief description>",
       content=html_content,  # Read from notebooks/<name>.html
       content_type="text/html",
   )
   ```
   This returns a go-link (e.g., `https://artifacts.agcouch.com/artifacts/<uuid>`) that can be shared in Slack or linked from PRs. The HTML renders interactively with all charts and styling intact.

**Report styling**: Since the HTML companion is the primary deliverable, use styled HTML in markdown cells to produce a polished, readable report. The notebook should look like a professional briefing, not raw markdown.

- **Section headers**: Use colored left-border accents via inline HTML `<div>` blocks to visually separate sections (e.g., `border-left: 4px solid #2980b9; padding-left: 12px`). Use a consistent color palette for section headers.
- **Callout boxes**: Wrap key insights in styled `<div>` blocks with background colors and borders — e.g., light-blue for findings (`background: #eaf4fc; border-left: 4px solid #2980b9`), light-green for positive results (`background: #eafaf1; border-left: 4px solid #27ae60`), light-yellow for caveats/warnings (`background: #fef9e7; border-left: 4px solid #f39c12`), light-red for negative findings (`background: #fdedec; border-left: 4px solid #e74c3c`).
- **Executive summary**: Style as a distinct block with a dark header bar and key findings in callout boxes.
- **Section transitions**: Use `<hr style="...">` with subtle styling between major sections.
- **Evidence tags**: Style inline evidence citations with a small gray pill/badge (e.g., `<span style="background: #ecf0f1; padding: 2px 8px; border-radius: 10px; font-size: 0.85em">source: model_analysis API</span>`).
- **Tables**: Use standard markdown tables — they render well in both notebook and HTML.
- **No emojis**: Use color and structure for visual hierarchy, not emoji.

**Notebook visualization patterns:**

```python
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

# Bar chart for category comparison
fig = px.bar(df, x="category", y=["model_a_win_rate", "model_b_win_rate"],
             barmode="group", title="Win Rate by Category")
fig.show()

# Time series for rating trajectory
fig = px.line(df, x="date", y="score", color="model",
              title="Rating Trajectory Over Time")
fig.show()

# Heatmap for head-to-head matrix
fig = px.imshow(pivot_df, text_auto=True,
                title="Head-to-Head Win Rate Matrix")
fig.show()
```

### Artifact (Shareable Evidence)

For findings that need to be shared (e.g., in Slack or PRs):

```python
# For plain text/markdown content
add_artifact(
    artifact_type="TEXT",
    title="DS Analysis: <brief description>",
    content="<formatted analysis with tables and findings>",
)

# For HTML reports (e.g., from Jupyter notebook export)
add_artifact(
    artifact_type="TEXT",
    title="DS Report: <brief description>",
    content=html_content,
    content_type="text/html",
)
```

**Prefer HTML for rich reports** — the HTML output from `jupyter nbconvert` includes interactive Plotly charts and styled markdown. Upload it with `content_type="text/html"` so it renders correctly in the browser.

---

## Data Source Reference

### BigQuery Tables (prodyuppdb_public)

| Table / View | Key Columns | Use For |
|-------|-------------|---------|
| `leaderboard.battles_production` | winner/loser_taxo_label, winner/loser_taxonomy_id, prompt_category_name, eval_created_at, blinded, prompt_difficulty | **Battle analysis** — pre-joined view with winner/loser already resolved. Preferred over raw tables. |
| `prodyuppdb_public.evals` | eval_id, eval_type (SELECTION/DOWNVOTE/QUICK_TAKE), turn_id, deleted_at, quality_score | Raw eval records (no winner/loser columns) |
| `prodyuppdb_public.message_evals` | message_eval_id, eval_id, message_id, rating (GOOD/BAD), reasons | Per-message feedback |
| `prodyuppdb_public.chat_messages` | message_id, content, assistant_language_model_id, category_id, message_type, streaming_metrics, completion_status | Message details, latency |
| `prodyuppdb_public.turns` | turn_id, chat_id, creator_user_id, created_at | Turn-level data |
| `prodyuppdb_public.chats` | chat_id, title, is_public | Chat metadata |
| `prodyuppdb_public.language_model_taxonomy` | language_model_taxonomy_id, taxo_label, model_publisher/family/class | Model identity |
| `prodyuppdb_public.language_models` | language_model_id, internal_name, provider_id, taxonomy_id | Provider-specific models |
| `prodyuppdb_public.users` | user_id, country_code, age_bucket, education_level | User demographics |
| `prodyuppdb_public.categories` | category_id, name | Prompt categories |
| `prodyuppdb_public.rating_history` | taxonomy_id, score, rank, snapshot_timestamp | Historical rankings |

### Leaderboard API Endpoints

Invoke `/query-leaderboard` for full API reference. Key endpoints:

| Endpoint | Returns |
|----------|---------|
| `/leaderboard` | Rankings with scores, CI, wins/losses, leaderboard stats |
| `/leaderboard/model_analysis` | 40+ dimensions of battle analysis for one model |
| `/leaderboard/matches` | Paginated battle records |
| `/leaderboard/history` | Rating time series |

---

## Important

The SQL examples, table schemas, and column references in this skill are illustrative starting points. **The actual database schema and codebase are always the authoritative source of truth.** If you encounter discrepancies between this skill and the live schema (e.g., column names, table structures, join paths), trust the database and code — they evolve faster than this document. Use `/fetch-from-db` and `INFORMATION_SCHEMA` queries to verify before running anything.

---

## Tips

1. **Always check sample sizes** — a 90% win rate over 10 battles is not meaningful. State the N alongside every percentage.
2. **Use confidence intervals** — the leaderboard API provides `rating_lower` and `rating_upper` (95% CI) in `model_rating`. If CIs overlap, the ranking difference may not be significant.
3. **Control for confounders** — blinded vs unblinded, vendor vs community, category mix, and opponent strength all affect raw win rates.
4. **Prefer the leaderboard API** over raw SQL when possible — it handles weighting, bias correction, and bootstrap CIs that are hard to replicate in ad-hoc queries.
5. **BigQuery is eventually consistent** — data may lag production by minutes to hours. For real-time data, use `query_yuppdb`.
6. **Be specific in narratives** — "GPT-4o wins 72% of Coding battles (N=3,200) vs Claude's 65% (N=2,800)" is better than "GPT-4o does better at coding."
7. **Consider the audience** — when writing for Slack/artifact, lead with the punchline, then provide supporting evidence.
8. **For temporal analysis**, use at least 7-day windows to smooth out day-of-week effects.
9. **Category names are lowercase** in the leaderboard API — use `"coding"`, not `"Coding"`. Capitalized names silently return no matches.
10. **Leaderboard response nesting** — model data is under `.model_rating` (rating, rank, wins, losses, win_rate) and `.model_info` (publisher, family, cost). Use jq paths like `.models[].model_rating.taxonomy_label`.
11. **For cloaked models**, set `"hide_cloaked": false` in the leaderboard request. Cloaked models appear under "Mystery" publisher.
12. **`/model_analysis` is the richest endpoint** — it returns eval notes (as_winner/as_loser arrays), blinded/vendor stats, opponent strength, difficulty distributions, speed metrics, response length, and weekly trends. Embed the data directly in notebooks rather than calling the API from notebook code (API calls are slow and require auth setup).
13. **Plotly for notebooks** — use `plotly.express` for quick charts and `plotly.graph_objects` for customized ones. Always call `fig.show()` so outputs render when the notebook is executed.

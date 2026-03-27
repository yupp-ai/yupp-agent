---
name: query-leaderboard
description: Query the Yupp leaderboard API for rankings, model analysis, battle matches, and rating history. Use when you need leaderboard data that goes beyond raw database queries — rankings with confidence intervals, head-to-head analysis, win rates, and demographic breakdowns.
allowed-tools: Bash
---

# Leaderboard API Reference

Use this skill when querying the Yupp leaderboard API. The leaderboard API provides pre-computed rankings, battle analysis, match browsing, and rating history — all powered by a Bradley-Terry (Choix) ranker with bootstrap confidence intervals.

## How to Call the API

All endpoints are **POST** with JSON bodies. Use `curl` against the leaderboard base URL:

```bash
# Production
LEBO_URL="https://lebo-production.yupp.ai"

# Staging
LEBO_URL="https://lebo-staging.yupp.ai"
```

**Authentication**: Include the `X-API-KEY` header. The key may already be set in your environment; if not, export it from `.env`:

```bash
# Check if already set, otherwise export from .env
[ -z "$X_API_KEY" ] && export $(grep '^X_API_KEY=' .env | tail -1 | tr -d "'\"")
```

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{"collect_stats": true, "num_results": 20}' | jq
```

**Tip**: Responses are large, pipe to `jq` for pretty-printing or for field extraction.

**Important defaults** (from `LeaderboardRequestDefaults` in `ypl/leaderboard/ranking_structs.py`):
- `collect_stats` defaults to **`false`** — set to `true` to get the `stats` block (battle counts, user counts, category/country/language distributions)
- `live_models` is a **tristate** field: `true` = only live models, `false` (default) = exclude live models, `null` = include all. For active/public models, use `active_models_only: true` instead
- `image_generation_models` defaults to **`false`** (excludes image gen models)
- `hide_cloaked` defaults to **`true`** (hides cloaked/testing models)
- `obfuscate_votes` defaults to **`true`** — set to `false` for exact vote counts
- `include_eval_notes` defaults to **`false`** — set to `true` to get win/loss notes (expensive)
- `min_turn_quality_score` defaults to **`1.0`** — set to `null` to include all battles regardless of quality
- `num_results` defaults to **`1000`** — set explicitly to limit response size

---

## Endpoints

### 1. `/leaderboard` — Main Rankings

Returns ranked models with scores, confidence intervals, win/loss counts, and leaderboard statistics.

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{
    "collect_stats": true,
    "num_results": 50,
    "sort_by": "score",
    "sort_reverse": true
  }'
```

**Response shape** (each model entry has nested `model_rating` and `model_info` objects):
```json
{
  "models": [
    {
      "model_rating": {
        "taxonomy_id": "...",
        "taxonomy_label": "GPT-4o",
        "rating": 1523.4,
        "rating_lower": 1510.2,
        "rating_upper": 1536.1,
        "rank": 1,
        "wins": 4521,
        "losses": 2103,
        "downvotes": 45,
        "win_rate": 0.683,
        "votes": 6624,
        "is_cloaked": false
      },
      "model_info": {
        "taxonomy_id": "...",
        "taxonomy_label": "GPT-4o",
        "model_publisher": "OpenAI",
        "model_family": "GPT-4o",
        "model_class": "GPT-4o",
        "first_token_avg_latency_ms": 820.5,
        "output_p50_tps": 45.2,
        "input_cost_usd_per_million_tokens": 2.5,
        "output_cost_usd_per_million_tokens": 10.0,
        "context_window_tokens": 128000,
        "parameter_count": null
      },
      "language_models": null
    }
  ],
  "stats": {
    "num_battles": 125000,
    "num_ties": 15000,
    "num_downvotes": 3200,
    "num_users": 8500,
    "from_date": "2025-01-01T00:00:00Z",
    "to_date": "2026-02-10T00:00:00Z",
    "category_name_counts": {"coding": 25000, "math": 12000, ...},
    "user_country_code_counts": {"US": 45000, "IN": 12000, ...},
    "language_code_counts": {"en": 80000, "zh": 5000, ...}
  },
  "total_count": 150,
  "data_updated_at": "2026-02-10T12:00:00Z"
}
```

**Extracting model data with jq**:
```bash
# Summary table
jq '.models[] | {label: .model_rating.taxonomy_label, rating: .model_rating.rating, rank: .model_rating.rank, wins: .model_rating.wins, losses: .model_rating.losses, win_rate: .model_rating.win_rate}'

# Search for a model by name
jq '.models[] | select(.model_rating.taxonomy_label | test("GPT-4o")) | .model_rating'
```

### 2. `/leaderboard/model_analysis` — Battle Analysis for a Model

Comprehensive battle statistics for a single model: win rates, opponent matchups, category/difficulty/demographic breakdowns, speed metrics, eval notes, and temporal trends.

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard/model_analysis" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{
    "taxo_label": "GPT-4o",
    "top_n": 10
  }'
```

**Request fields**:
| Field | Type | Description |
|-------|------|-------------|
| `taxo_label` | string | Model taxonomy label (provide this OR taxonomy_id) |
| `taxonomy_id` | string | Model taxonomy UUID (provide this OR taxo_label) |
| `top_n` | int | Number of top opponents to return (default: 10) |
| `from_date` | datetime | Filter battles from this date |
| `to_date` | datetime | Filter battles until this date |
| `category` | string | Filter by prompt category (e.g., "coding", "math") |
| `is_svg_prompt` | bool | Filter to SVG prompts only |

**Response includes**:
- `total_battles`, `wins`, `losses`, `win_rate`
- `top_wins_against` / `top_losses_to` — best/worst matchups with counts and win rates
- `head_to_head_summary` — record against every opponent
- `category_distribution_as_winner` / `as_loser` — category breakdown
- `difficulty_distribution_as_winner` / `as_loser`
- `blinded_stats` — win rate in blinded vs unblinded battles
- `vendor_stats` — performance with vendor vs community raters
- `user_stats` — unique users, country/age/education distributions, risk scores
- `weekly_wins_losses` / `daily_wins_losses` — temporal trends
- `language_breakdown`, `country_breakdown`, `tags_breakdown`, `platform_breakdown`
- `reasoning_effort_breakdown`
- `elapsed_time_stats`, `time_to_first_token_stats`, `tps_stats` — speed percentiles (p10-p99)
- `*_histogram` — histogram data for elapsed time, TTFT, TPS (winner vs loser)
- `speed_vs_win_rate`, `win_rate_by_elapsed_bucket`, `win_rate_by_ttft_bucket`, `win_rate_by_tps_bucket`
- `eval_notes_stats` / `eval_notes_win_lift` — what evaluators said and how it correlates with wins
- `opponent_rating` / `opponent_ranking` — strength of opponents faced
- `selection_source_stats`, `completion_status_stats`

### 3. `/leaderboard/matches` — Browse Battles

Paginated battle records with full filtering. Extends `LeaderboardRequest` with match-specific options.

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard/matches" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{
    "num_results": 20,
    "offset": 0,
    "sort_by": "eval_created_at",
    "sort_reverse": true,
    "winner_taxonomy_labels": ["GPT-4o"],
    "loser_taxonomy_labels": ["Claude 3.5 Sonnet"]
  }'
```

**Note**: `sort_by` defaults to `"eval_created_at"` (not `"score"` like the main endpoint). Also supports `"random"` — use with `sort_seed` for reproducible random sampling.

**Extra fields** (beyond LeaderboardRequest):
| Field | Type | Description |
|-------|------|-------------|
| `include_downvotes` | bool | Include downvote records |
| `include_users` | bool | Include user details |
| `sort_seed` | int | Random seed for reproducibility when `sort_by` is `"random"` |

**Response**: `{ "total": N, "matches": [...], "downvotes": [...], "users": [...] }`

### 4. `/leaderboard/history` — Rating History

Time series of rating snapshots for a model.

```bash
curl -s -X POST "$LEBO_URL/api/v1/leaderboard/history" \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $X_API_KEY" \
  -d '{
    "taxonomy_id": "<uuid>",
    "from_date": "2025-06-01",
    "to_date": "2026-02-10",
    "num_results": 100
  }'
```

**Request fields**:
| Field | Type | Description |
|-------|------|-------------|
| `taxonomy_id` | UUID | Model taxonomy ID (or language_model_id) |
| `language_model_id` | UUID | Alternative to taxonomy_id |
| `category_name` | string | Filter by category |
| `from_date` / `to_date` | datetime | Date range |
| `min_score` / `max_score` | float | Score range filter |
| `min_rank` / `max_rank` | int | Rank range filter |
| `offset` / `num_results` | int | Pagination |

Other endpoints exist but are less commonly needed:
- `/leaderboard/discover` — curated highlights, tiles, and best/worst matchups
- `/leaderboard/model_info` — detailed info for a single model by taxonomy_id, with optional example matches
- `/leaderboard/predict`, `/leaderboard/trust`, `/leaderboard/tags`
- `/leaderboard/reload_data` — trigger a data refresh (admin)

See `ypl/backend/routes/v1/rank.py` for their full definitions.

---

## LeaderboardRequest Filter Reference

The `/leaderboard` and `/leaderboard/matches` endpoints accept the full `LeaderboardRequest` filter set. These are the most commonly useful filters:

### Date Filters
| Field | Type | Description |
|-------|------|-------------|
| `from_date` | datetime | Start of date range |
| `to_date` | datetime | End of date range |
| `lookback_days` | int | Rolling window from latest data (overrides from/to_date) |
| `user_from_date` / `user_to_date` | datetime | Filter by user signup date |
| `model_from_date` / `model_to_date` | datetime | Filter by model release date |

### Content Filters
| Field | Type | Description |
|-------|------|-------------|
| `category_names` | tuple[str] | Prompt categories (lowercase): coding, math, reasoning, informational, creative, task, other |
| `language_codes` / `language_names` | tuple[str] | User language |
| `tags` | tuple[str] | Battle tags |
| `min_prompt_difficulty` / `max_prompt_difficulty` | int | Difficulty 1-10 |
| `min_prompt_length` / `max_prompt_length` | int | Prompt text length |
| `is_svg_prompt` / `is_html_prompt` | bool | Markup generation |
| `blinded` | bool | Blinded evaluations only |
| `is_public` | bool | Public vs private chats |
| `min_turn_sequence_id` / `max_turn_sequence_id` | int | Turn position in conversation |

### User Filters
| Field | Type | Description |
|-------|------|-------------|
| `user_country_codes` | tuple[str] | Geographic filter (ISO codes) |
| `user_age_buckets` | tuple[str] | Age buckets |
| `user_education_levels` | tuple[str] | Education levels |
| `user_max_risk_score` | float | Max fraud risk score |
| `is_vendor` | bool | Professional raters only |
| `user_ids` | tuple[str] | Specific users |
| `user_platforms` | tuple[str] | Desktop, Mobile, etc. |

### Model Filters
| Field | Type | Description |
|-------|------|-------------|
| `image_generation_models` | bool | Image generation models |
| `reasoning_models` | bool | Reasoning-capable models |
| `reasoning_effort` | str | low / medium / high |
| `live_models` | bool/null | Tristate: `true` = live only, `false` = exclude live, `null` = all |
| `model_names` | tuple[str] | Filter by model name |
| `model_family_filter` | tuple[str] | Filter by model family (JSON array) |
| `winner_taxonomy_labels` / `loser_taxonomy_labels` | tuple[str] | Specific matchups |
| `min_votes` / `min_votes_for_ranking` | int | Popularity thresholds |

### Ranking Parameters
| Field | Type | Description |
|-------|------|-------------|
| `alpha` | float | Choix regularization parameter |
| `blinded_battle_weight_multiplier` | float | Extra weight for blinded battles |
| `high_quality_battle_weight_multiplier` | float | Extra weight for high-quality battles |
| `use_style_control_weights` | bool | Style-aware ranking |
| `use_speed_control_weights` | bool | Speed-aware ranking |
| `num_bootstrap_iterations` | int | Confidence interval precision |

### Post-Processing (don't affect ranking calculation)
| Field | Type | Description |
|-------|------|-------------|
| `sort_by` | str | Valid keys: `score`, `rating`, `votes`, `wins`, `losses`, `downvotes`, `downvotes_per_battle`, `win_rate`, `name`, `abort_rate_30d`, `parameter_count`, `context_window_tokens`, `knowledge_cutoff_date`, `input_cost_usd_per_million_tokens`, `output_cost_usd_per_million_tokens`, `per_request_cost_usd`, `first_token_avg_latency_ms`, `first_token_p50_latency_ms`, `first_token_p90_latency_ms`, `output_avg_tps`, `output_p50_tps`, `output_p90_tps`, `per_image_cost_usd` |
| `sort_reverse` | bool | Ascending/descending |
| `offset` / `num_results` | int | Pagination |
| `hide_cloaked` | bool | Hide cloaked/testing models |
| `active_models_only` | bool | Only currently active models |
| `grouped` | bool | Group by model family |
| `min_parameter_count` / `max_parameter_count` | int | Model size filter |
| `min_context_window_tokens` / `max_context_window_tokens` | int | Context length |
| `min_input_cost_usd_per_million_tokens` / `max_input_cost_usd_per_million_tokens` | float | Input cost range |
| `min_output_cost_usd_per_million_tokens` / `max_output_cost_usd_per_million_tokens` | float | Output cost range |

---

## Common Patterns

### Get full leaderboard sorted by score
```json
{"collect_stats": true, "num_results": 200, "sort_by": "score", "sort_reverse": true}
```

### Category-specific leaderboard (e.g., coding)
```json
{"collect_stats": true, "category_names": ["coding"], "num_results": 50}
```

### Compare two models head-to-head
Use `/leaderboard/matches` with `winner_taxonomy_labels` and `loser_taxonomy_labels` to see battles where A beat B, then reverse to see B beat A.

### Leaderboard for a specific geography
```json
{"collect_stats": true, "user_country_codes": ["US"], "num_results": 50}
```

### Recent battles only (last 30 days)
```json
{"collect_stats": true, "lookback_days": 30}
```

### Include cloaked/testing models
```json
{"collect_stats": true, "hide_cloaked": false, "category_names": ["coding"]}
```

### Rating trajectory for a model
Use `/leaderboard/history` with the model's `taxonomy_id` and a date range.

---

## Important

The API request/response shapes, field names, and filter options documented here are illustrative starting points. **The actual codebase (see `ypl/leaderboard/ranking_structs.py` and `ypl/backend/routes/v1/rank.py`) is always the authoritative source of truth.** If you encounter discrepancies between this skill and the live API behavior, trust the code — it evolves faster than this document.

---

## Tips

1. **Start with `/leaderboard`** to get taxonomy IDs and labels, then use those in other endpoints.
2. **Use `min_votes`** to filter out models with insufficient data for reliable rankings.
3. **`/leaderboard/model_analysis`** is the most powerful endpoint for understanding why a model ranks where it does — it returns 40+ dimensions of analysis.
4. **Large responses**: Pipe through `jq` for field extraction, e.g., `jq '.models[] | {label: .model_rating.taxonomy_label, rating: .model_rating.rating, rank: .model_rating.rank}'`.
5. **Timeouts**: The leaderboard API can be slow for large queries. Use `--max-time 120` with curl.
6. **The API key** is the same one used across Yupp services. Check `.env` for `X_API_KEY`.
7. **Category names are lowercase** — use `"coding"`, not `"Coding"`. Capitalized names will silently return no matches.
8. **Response nesting** — model data is nested under `.model_rating` (scores, rank, wins/losses) and `.model_info` (publisher, family, cost, latency). Use jq paths like `.models[].model_rating.taxonomy_label`.

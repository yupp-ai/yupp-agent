---
name: fetch-from-db
description: Comprehensive Yupp database schema reference. Use when querying language models/taxonomies/providers, chats/turns/messages, routing info, evaluations/feedback, promotions, cost metrics, leaderboard data, or any production database queries.
allowed-tools: mcp__agcouch-mcp-server__query_yuppdb, mcp__agcouch-mcp-server__query_bigquery
---

# Yupp Database Schema Knowledge

Use this skill when querying the Yupp production database via the `query_yuppdb` MCP tool to understand table relationships and common query patterns. For authoritative, up-to-date schema ground truth, refer to the database schema directly. ORM definitions with field-level details and comments can be found in Python files under `ypl/db`.

**Database Environments**:
- Production and staging DBs have the same schema but separate data sets.
- Read-only replicas exist in BigQuery: `devyuppdb_public` (staging) and `prodyuppdb_public` (production).
- Local development is backed by a reseeded copy of the staging DB.

**Tips and Best Practices:**
- Search for table names in the codebase to find ORM definitions and links to relevant schema migration PRs.
- If your Postgres query is slow, check whether the queried columns are indexed, and see if current indexes match your query shape (look up composite, partial, or expression indexes as needed). If the table lacks an optimal index and it's feasible to add one—especially for repeated analytics workloads—consider proposing an index addition.
- Prefer querying the primary (Postgres) DB for up-to-date and transactional data, but if you encounter persistent performance issues due to scale or indexing, you can use the BigQuery read replica as a fallback for large analytic or reporting queries.
- When using BigQuery, remember that data will be slightly delayed and fully read-only; transact and mutate only via Postgres.

---

## Models and Taxonomies

Yupp offers hundreds of models from dozens of inference API providers.

### Core Model Tables

#### language_model_taxonomy (LMT)
- One entry per model taxonomy/type - the unit for Leaderboard entries
- Contains model info independent of providers: max token windows, attachment types, model flags (pro/max/image-generation/reasoning/internal)
- **Primary key**: `language_model_taxonomy_id` (FK'd as `taxonomy_id` in LM table)
- **Key components**: `{model_publisher, model_family, model_class, model_version}` form a taxonomy path (not primary keys)
- Separate entries exist for model variants (e.g., high-thinking vs low-thinking)
- Duplicate entries have `canonical_id` pointing to the canonical entry

#### language_models (LM)
- One entry per model from a specific inference provider
- Contains provider-specific info like `internal_name`
- Multiple LM entries can link to the same LMT (same model, different providers)
- May override LMT fields like `support_attachment_mime_types` or `tier`
- **Foreign keys**: `taxonomy_id` -> LMT, `provider_id` -> providers

#### language_model_families (LMF)
- Family-level info keyed on `{model_publisher, model_family}` (optionally `{model_publisher, model_family, model_class}`)
- Stores UI assets: icons and colors for Model Picker and chat responses

#### providers
- Provider (inference API) details: API path, API key name, short code for internal name disambiguation
- Every LM entry must link to a provider

#### yapps
- Yupp's agentic models stored as normal language models with own LM and Provider entries

### Model Table Projections
- LMF – 1:n – LMT
- LMT – 1:n – LM
- providers – 1:n – LM

### Other Model Tables

| Table | Description |
|-------|-------------|
| `language_model_discovery` | Discovered models from provider API monitoring; triggers Slack notifications |
| `language_model_licenses` | License types and IDs for models |
| `organizations` | Organization info for models (not actively used) |
| `language_model_response_statuses` | Response statuses from periodic validations and chat messages |

---

## Chats

### Core Chat Tables

#### chat_messages (CM)
- One entry per message: USER_MESSAGE, QUICK_RESPONSE (quicktake), or ASSISTANT_MESSAGE
- Stores content, serving model, streaming latency metrics, tsvectors
- **turn_sequence_number (tsn)**: 0 = user message, 1 = quicktake, 2+ = AI messages (resets each turn)
- Links to `turns` via `turn_id`

#### turns
- One entry per turn (always starts with user message)
- "Show more AIs" creates new rounds within same turn (informal concept, no DB entity)
- **Key fields**: `creator_user_id`, `chat_id`

#### chats
- Chat-level info: creator, onboarding status, etc.

#### attachments
- Metadata for attachments (user uploads or AI-generated images)
- Actual content stored in GCS buckets

### Chat Table Projections
- chats – 1:n – turns – 1:n – chat_messages

### Prompt Modifier Tables
User-configurable response styles (e.g., concise):
- `prompt_modifier_assocs`
- `prompt_modifiers`

### Prompt Suggestion Tables
Suggested prompts from benchmarks, news, or previous turns:
- `external_benchmark_dataset_*` (multiple tables)
- `suggested_trending_topic_prompts`
- `suggested_turn_prompts`
- `suggested_user_prompts`
- `news_stories`

---

## Categories, Tags, Embeddings, and Memories

### Memories Tables
| Table | Description |
|-------|-------------|
| `categories` | Categories for user prompt labeling |
| `tags` | Tags on messages with tsvectors |
| `chat_message_tags` | Tag associations for messages |
| `embedding_models` | Embedding model definitions |
| `tag_embeddings` | Tag vector embeddings |
| `chat_message_embeddings` | Message vector embeddings |
| `chat_message_memory_associations` | Memory links for messages |
| `chat_message_supported_prompt_assocs` | Supported prompt associations |
| `memories` | Extracted and consolidated memories |
| `memory_embeddings` | Memory vector embeddings |

### Other Chat Tables
- `chat_instrumentations`
- `chat_sharing_audits`

---

## Routing

Routing selects models for new turns or "show more AIs" rounds based on prompt, user, and context.

### routing_info
- One entry per routing decision (turn start or "show more AIs")
- **Key columns**:
  - `routing_info_id` (UUID) - Primary key
  - `turn_id` (UUID) - Foreign key to `turns.turn_id`
  - `categories` (JSONB) - Raw categories from routing
  - `resolved_categories` (JSONB array) - Final resolved category list
  - `routing_outcome` (VARCHAR) - The routing outcome
- **Note**: A single turn may have multiple `routing_info` entries

**Important**: 
- Relationship is `routing_info.turn_id` -> `turns.turn_id` (routing_info references turns)
- Do NOT rely on `chat_messages.routing_info_id` - it may be NULL. Always use `routing_info.turn_id`

### Other Routing Tables
| Table | Description |
|-------|-------------|
| `routing_reasons` | Keys and messages for routing UI display |
| `routing_rules` | **(DEPRECATED)** Rules for model acceptance/rejection by category |

### Routing Projections
- turns – 1:n – routing_info

**Example query to get routing categories for a chat:**
```sql
SELECT t.turn_id, t.sequence_id, ri.resolved_categories
FROM turns t
JOIN routing_info ri ON ri.turn_id = t.turn_id
WHERE t.chat_id = '<chat_id_here>'
ORDER BY t.sequence_id
```

---

## Promotions

Special promotions and cloaked testing for models to increase exposure or collect evals faster.

### model_promotions
- Individual promotion entries
- Specify model (LM), taxonomy (LMT), or model selector (LMT path patterns)
- Parameters: appearance frequency, competition strength with other promotions
- Has status and optional start/end dates

### cloaked_testing
- Partner cloaked testing management
- Stores start/end dates, eval collection goals, leaderboard visibility

### model_promotion_campaigns
- Campaign info with shared settings for all models in a promotion

---

## Cost Management

### language_model_cost_usage_metrics
- Per-inference cost info (per-chat or per-LLM Labeler)
- Records: language_model_id, input/output token counts
- Used for spending tracking and budget control

### language_model_spending_statuses
- Per-user, per-model, and overall spending status
- Budget types: text, image-gen, max models, etc.
- Statuses: on budget, soft over budget (projected), over budget

### turn_costs
- Appears unused

---

## Evaluations and Feedback

### evals
One entry per eval action, linked to one or more `message_evals`.
- **Eval types**:
  - `SELECTION` - Two AI messages has one winner and one loser (aka PREF)
  - `TIE` - Two AI messages are having a tie
  - `DOWNVOTE` - One AI message (aka NOPE: negative feedback)
  - `QUICKTAKE` - Quicktake message (positive/negative)
- Links to a turn via `turn_id`

### message_evals
- Per-message eval linked to `evals`
- Stores: rating (GOOD/BAD/NEUTRAL - only GOOD/BAD used), user comments, reasons, blinded status
- Links to message via `message_id`

### message_review_evals
- Evals on AI-generated message reviews

### Automated Evals (LLM Judge)
| Table | Description |
|-------|-------------|
| `evals_automated` | LLM-generated evals on turns for human eval validation |
| `message_evals_automated` | Message-level automated eval results |

### Eval Projections
- turns – 1:n – evals – 1:n – message_evals
- turns – 1:1 – evals_automated – 1:n – message_evals_automated

### Turn Evaluations

#### turn_qualities
- Extracted info from user prompts and AI responses
- Helps understand turn difficulty/novelty for value assessment

### User Evaluation History
| Table | Description |
|-------|-------------|
| `user_model_eval_history` | Per-user eval history on models (used for routing) |
| `user_turn_likes` | Appears unused |

### Other Feedback Tables
| Table | Description |
|-------|-------------|
| `app_feedback` | User feedback submitted via side panel |
| `vendors` | Third-party professional rater companies (also platform users) |
| `user_vendor_profiles` | Info for users from vendors |

---

## Eval Export Chat ID Mappings

When working with eval exports, chat IDs are mapped between internal and external formats:

- **Table**: `data_export_chat_id_mappings`
- **Columns**:
  - `internal_chat_id` (UUID) - The internal chat ID used in `chats` and `turns` tables
  - `external_chat_id` (VARCHAR) - The hashed/external chat ID used in exports

**Example query to find internal chat ID from external:**
```sql
SELECT internal_chat_id, external_chat_id
FROM data_export_chat_id_mappings
WHERE external_chat_id = '<external_chat_id_here>'
```

### Other Export Tables
- `data_export_attachment_mappings`
- `eval_data_exports`

---

## Common Query Patterns

### Get all messages for a chat
```sql
SELECT c.chat_id, t.turn_id, t.sequence_id, cm.*
FROM chats c
JOIN turns t ON t.chat_id = c.chat_id
JOIN chat_messages cm ON cm.turn_id = t.turn_id
WHERE c.chat_id = '<chat_id>'
ORDER BY t.sequence_id, cm.turn_sequence_number
```

### Get model info with provider
```sql
SELECT lm.*, lmt.model_publisher, lmt.model_family, lmt.model_class, p.name as provider_name
FROM language_models lm
JOIN language_model_taxonomy lmt ON lm.taxonomy_id = lmt.language_model_taxonomy_id
JOIN providers p ON lm.provider_id = p.provider_id
WHERE lm.language_model_id = '<model_id>'
```

### Get evals with message details
```sql
SELECT e.*, me.rating, me.user_comment, cm.content
FROM evals e
JOIN message_evals me ON me.eval_id = e.eval_id
JOIN chat_messages cm ON cm.chat_message_id = me.chat_message_id
WHERE e.turn_id = '<turn_id>'
```

---

## Summary: Key Table Relationships

```
chats.chat_id -> turns.chat_id (1:n)
turns.turn_id -> chat_messages.turn_id (1:n)
turns.turn_id <- routing_info.turn_id (1:n)
turns.turn_id -> evals.turn_id (1:n)
evals.eval_id -> message_evals.eval_id (1:n)
turns.turn_id -> evals_automated.turn_id (1:1)
evals_automated.eval_id -> message_evals_automated.eval_id (1:n)

language_model_families -> language_model_taxonomy (1:n)
language_model_taxonomy -> language_models (1:n)
providers -> language_models (1:n)
```

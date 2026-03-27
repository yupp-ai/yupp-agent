---
name: debug-model-streaming
description: Debug model streaming issues by analyzing completion status, error types, and GCP logs. Use when investigating streaming failures, timeouts, provider errors, or any model response issues for a specific model.
allowed-tools: mcp__yuppster-mcp-server__search_gcp_logs, mcp__yuppster-mcp-server__query_yuppdb, mcp__yuppster-mcp-server__query_bigquery, mcp__yuppster-mcp-server__create_yuppaste, mcp__yuppster-mcp-server__search_slack, mcp__yuppster-mcp-server__read_slack_thread, Bash, Read, Write
---

# Model Streaming Debug Guide

Use this skill when debugging streaming issues - understanding why a specific model is failing, investigating streaming errors, timeouts, rate limits, or any model response problems.

## TL;DR

- **Query `chat_messages` table** for `completion_status` and `model_error_type` to quantify error frequency
- **Search GCP logs** with model name + "Streaming: error" to find stack traces and error details
- **Check error type importance**: BILLING/POST_PROCESSING = High (act now), RATE_LIMIT/PROVIDER_ERROR = Medium (monitor), TIMEOUT/CONTEXT_LENGTH = Low (tune config)
- **Compare across providers**: Same taxonomy under different providers helps isolate if issue is provider-specific or model-specific
- **Key code files**: `chat_completions.py` (streaming), `management_common.py` (error classification), `chats.py` (DB schema)

## Action/Inaction Advisory

| Error Type | Urgency | Action Required |
|------------|---------|-----------------|
| `BILLING` | 🔴 **Immediate** | Top up provider account NOW - service is blocked |
| `POST_PROCESSING` | 🔴 **Immediate** | Code bug - investigate and fix immediately |
| `UNCATEGORIZED` | 🟠 **High** | Unknown error - examine details, may need ERROR_KEYWORDS_MAP update |
| `RATE_LIMIT` | 🟡 **Monitor** | Check if traffic spike; usually transient |
| `PROVIDER_ERROR` | 🟡 **Monitor** | Provider issue; monitor frequency, escalate if persistent |
| `INVALID_INPUT` | 🟡 **Investigate** | Check request format, may indicate API incompatibility |
| `EMPTY_RESPONSE` | 🟡 **Investigate** | May indicate provider degradation |
| `TIMEOUT` | 🟢 **Tune** | Adjust timeout config or accept as expected for slow models |
| `CONTEXT_LENGTH` | 🟢 **Tune** | Improve token estimation or context trimming |
| `CONTENT_POLICY` | 🟢 **Expected** | Normal for certain prompts, no action unless excessive |
| `ATTACHMENT_ERROR` | 🟢 **Expected** | User exceeded limits, no action unless pattern emerges |
| `DECOMMISSIONED` | ℹ️ **Auto-handled** | Model auto-marked inactive, verify if needed |

---

## Important: Timestamp-Based Queries

**Prefer using specific timestamp ranges** over `hours_back` to reduce impact on our logging API quota:

```
# Preferred: Use timestamp range when you know the time window
search_gcp_logs(
    query='textPayload:"{model_name}" severity>="WARNING" timestamp>="2026-01-25T17:00:00Z" timestamp<="2026-01-25T18:00:00Z"',
    max_results=100
)

# Fallback: Use hours_back only when time is unknown
search_gcp_logs(
    query='textPayload:"{model_name}" severity>="WARNING"',
    hours_back=2,
    max_results=100
)
```

When investigating a specific message or turn, first query the database for `created_at` to get the timestamp, then use that in your log search. You can use a query like this:

```sql
-- Example query to get the timestamp for a turn
SELECT created_at FROM turns WHERE turn_id = '{turn_id}';
```

## Quick Reference: Required Information

To debug a model streaming issue, you need:
1. **Model name** - The internal name (e.g., `gpt-4o-mini<>OAI`) or full name (e.g., `openai/gpt-4o-mini<>OAI`)
2. **Error type** (if known) - e.g., TIMEOUT, RATE_LIMIT, PROVIDER_ERROR, etc.
3. **Time window** - When the errors started occurring

---

## Step 1: Check Error Frequency in Database

First, understand how often this error is happening for the model by querying the `chat_messages` table:

### Query completion status distribution for a model

```sql
SELECT 
    cm.completion_status,
    cm.model_error_type,
    COUNT(*) as error_count,
    MIN(cm.created_at) as first_seen,
    MAX(cm.created_at) as last_seen
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
  AND cm.created_at > NOW() - INTERVAL '24 hours'
  AND cm.message_type = 'ASSISTANT_MESSAGE'
GROUP BY cm.completion_status, cm.model_error_type
ORDER BY error_count DESC
```

### Query recent failures with details

```sql
SELECT 
    cm.message_id,
    cm.completion_status,
    cm.model_error_type,
    cm.created_at,
    cm.streaming_metrics,
    t.turn_id,
    t.chat_id,
    lm.name as model_name,
    p.name as provider_name
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
JOIN providers p ON lm.provider_id = p.provider_id
JOIN turns t ON cm.turn_id = t.turn_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
  AND cm.completion_status IN ('STREAMING_ERROR', 'STREAMING_ERROR_FIRST_TOKEN_TIMEOUT', 'PROVIDER_ERROR', 'SYSTEM_ERROR')
  AND cm.created_at > NOW() - INTERVAL '24 hours'
ORDER BY cm.created_at DESC
LIMIT 50
```

### Check error rate trends over time

```sql
SELECT 
    date_trunc('hour', cm.created_at) as hour,
    COUNT(*) FILTER (WHERE cm.completion_status = 'SUCCESS') as success_count,
    COUNT(*) FILTER (WHERE cm.completion_status != 'SUCCESS') as failure_count,
    ROUND(100.0 * COUNT(*) FILTER (WHERE cm.completion_status != 'SUCCESS') / COUNT(*), 2) as error_rate_pct
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
  AND cm.created_at > NOW() - INTERVAL '48 hours'
  AND cm.message_type = 'ASSISTANT_MESSAGE'
GROUP BY hour
ORDER BY hour DESC
```

## Step 2: Search GCP Logs for Error Details

### Search by model name and error

```
search_gcp_logs(
    query='textPayload:"{model_internal_name}" severity>="WARNING"',
    hours_back=2,
    max_results=100
)
```

### Search for specific error type

```
search_gcp_logs(
    query='textPayload:"{model_internal_name}" textPayload:"{error_type}"',
    hours_back=2,
    max_results=100
)
```

### Search for streaming errors with stack traces

```
search_gcp_logs(
    query='textPayload:"{model_internal_name}" textPayload:"Streaming: error"',
    hours_back=2,
    max_results=100
)
```

### Search by specific turn/message for detailed investigation

```
search_gcp_logs(
    query='textPayload:"{turn_id}" textPayload:"Streaming"',
    hours_back=2,
    max_results=50
)
```

## Model Error Types Reference

| Error Type | Importance | Meaning | Action |
|------------|------------|---------|--------|
| `BILLING` | High | Account balance/quota issues | Top up the provider account immediately |
| `POST_PROCESSING` | High | Error after streaming completed | Check code for post-processing bugs |
| `UNCATEGORIZED` / `UNKNOWN` | High | Unrecognized error | Examine error details, update ERROR_KEYWORDS_MAP if pattern found |
| `RATE_LIMIT` | Medium | Too many requests | Check traffic spike, configure rate limiting |
| `INVALID_INPUT` | Medium | Wrong input format to model | Check request formatting, API compatibility |
| `PROVIDER_ERROR` | Medium | Provider-side error | Usually transient, monitor frequency |
| `EMPTY_RESPONSE` | Medium | Model returned nothing | May indicate provider issues |
| `CONTEXT_LENGTH` | Low | Input too long for model | Improve token estimation, trim context |
| `CONTENT_POLICY` | Low | Content filtered | Expected for certain prompts |
| `TIMEOUT` | Low | First token timeout | Adjust timeout or investigate slow response |
| `ATTACHMENT_ERROR` | Low | Attachment processing failed | Check attachment limits for model |
| `DECOMMISSIONED` | FYI | Model no longer available | Auto-handled, model marked inactive |

## Completion Status Reference

| Status | Description |
|--------|-------------|
| `SUCCESS` | Streaming completed successfully |
| `USER_ABORTED` | User stopped the stream |
| `STREAMING_ERROR` | Generic streaming error occurred |
| `STREAMING_ERROR_FIRST_TOKEN_TIMEOUT` | Timeout waiting for first token |
| `STREAMING_ERROR_WITH_FALLBACK` | Error occurred but fallback was used |
| `STREAMING_ERROR_MAX_TOKENS` | Model reached max_tokens limit |
| `PROVIDER_ERROR` | Provider returned an error |
| `SYSTEM_ERROR` | Internal system error |
| `STREAMING_ERROR_CACHE_WARMUP` | Streaming error during cache warmup (retryable) |

## Step 3: Analyze Log Results

When you have log results, look for these patterns:

### Finding stack traces

```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message)"' | grep -i "traceback\|exception\|error"
```

### Finding error excerpts

```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message)"' | grep -i "{error_type}"
```

### Timeline analysis

```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message | tostring | .[0:200])"' | sort
```

## Step 4: Common Debugging Scenarios

### Scenario: Timeout Errors

1. Check if model is experiencing high latency:
```sql
SELECT 
    AVG((streaming_metrics->>'firstContentTokenTimestamp')::float - (streaming_metrics->>'requestTimestamp')::float) as avg_ttft_ms,
    MAX((streaming_metrics->>'firstContentTokenTimestamp')::float - (streaming_metrics->>'requestTimestamp')::float) as max_ttft_ms
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
  AND cm.created_at > NOW() - INTERVAL '24 hours'
  AND cm.completion_status = 'SUCCESS'
```

2. Check timeout configuration in `get_TTFT_timeout_for_model()`:
   - Image generation: Uses `TTFT_timeout_for_image_gen`
   - Reasoning models: Uses `TTFT_timeout_for_reasoning`
   - Regular models: Uses `TTFT_timeout_for_non_reasoning`

### Scenario: Rate Limit Errors

1. Check traffic volume:
```sql
SELECT 
    date_trunc('minute', cm.created_at) as minute,
    COUNT(*) as request_count
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
  AND cm.created_at > NOW() - INTERVAL '1 hour'
GROUP BY minute
ORDER BY minute DESC
```

2. Check if rate limit flag is set in Redis (look for log: "Streaming: marking model as rate limited in redis")

### Scenario: Provider Errors

1. Check if affecting single model or entire provider:
```sql
SELECT 
    lm.internal_name,
    COUNT(*) as error_count
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
JOIN providers p ON lm.provider_id = p.provider_id
WHERE p.name = '{provider_name}'
  AND cm.model_error_type = 'PROVIDER_ERROR'
  AND cm.created_at > NOW() - INTERVAL '24 hours'
GROUP BY lm.internal_name
ORDER BY error_count DESC
```

2. Search for provider-specific errors in logs

### Scenario: Empty Response

1. Check if specific to certain prompt patterns:
```sql
SELECT 
    t.turn_id,
    LEFT(um.content, 200) as user_prompt,
    cm.created_at
FROM chat_messages cm
JOIN turns t ON cm.turn_id = t.turn_id
JOIN chat_messages um ON um.turn_id = t.turn_id AND um.message_type = 'USER_MESSAGE'
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
  AND cm.model_error_type = 'EMPTY_RESPONSE'
  AND cm.created_at > NOW() - INTERVAL '24 hours'
LIMIT 20
```

### Scenario: Context Length Errors

1. Check if model has adequate context window:
```sql
SELECT 
    lm.internal_name,
    lmt.context_window_tokens,
    lmt.taxo_label
FROM language_models lm
JOIN language_model_taxonomy lmt ON lm.taxonomy_id = lmt.language_model_taxonomy_id
WHERE lm.internal_name LIKE '%{model_internal_name}%'
```

2. Check if prompts are being truncated properly

## Error Detection Keywords

The system uses `ERROR_KEYWORDS_MAP` in `management_common.py` to classify errors. Key patterns:

| Error Type | Sample Keywords |
|------------|----------------|
| DECOMMISSIONED | "has been decommissioned", "model not found", "no longer supported" |
| INVALID_INPUT | "input validation error", "parameter validation failed" |
| RATE_LIMIT | "rate limit", "too many requests", "429", "throttling" |
| CONTEXT_LENGTH | "context length", "too many tokens", "max tokens" |
| ATTACHMENT_ERROR | "pdf pages", "too many images", "document size" |
| CONTENT_POLICY | "content policy", "nsfw", "safety system" |
| EMPTY_RESPONSE | "no generation chunks", "no data returned" |
| PROVIDER_ERROR | "internal server error", "service unavailable" |
| TIMEOUT | "timeout", "timed out" |
| BILLING | "billing", "quota", "credit", "unauthorized" |

## Code References

Key files for understanding streaming logic:

| File | Purpose |
|------|---------|
| `ypl/backend/routes/v1/chat_completions.py` | Main streaming endpoint, `stream_content()`, error handling |
| `ypl/backend/llm/streaming.py` | Helper functions: `post_error()`, `log_streaming_error()`, `upsert_chat_message()` |
| `ypl/backend/llm/streaming_common.py` | `StreamingContext`, `ChatRequest`, `StreamResponse` |
| `ypl/backend/llm/model/management_common.py` | `ERROR_KEYWORDS_MAP`, `matches_error_keywords()`, error classification |
| `ypl/db/chats.py` | `ChatMessage`, `CompletionStatus`, `ModelErrorType` schemas |
| `ypl/backend/llm/provider/provider_clients.py` | Provider client initialization, `get_provider_client()` |

## Slack Channels

- **#alert-model-streaming-errors** / **#alert-model-streaming-errors-staging** - All streaming errors
- **#alert-backend** / **#alert-backend-staging** - High-priority errors (BILLING)
- **#alert-model-validation-errors** - Periodic validation errors
- **#alert-model-management** - General model management notifications

### Searching Slack for Related Alerts

Use `search_slack` to find related streaming error reports or past investigations:

```
search_slack(query="<model_name> in:#alert-model-streaming-errors")
search_slack(query="<error_type> <model_name> in:#alert-backend")
```

If you find a relevant thread, use `read_slack_thread` to get the full conversation context:
```
read_slack_thread(channel="<channel_id>", thread_ts="<thread_ts>")
```

This helps determine if the issue has been seen before, if someone is already investigating, or if a fix was previously deployed.

## Superset Dashboards

- **Model Errors** (go/model-errors) - Detailed model error types breakdown
- **Model Streaming Performance** - TTFT, TPS, latency metrics
- **Model Traffic** - Traffic by model, provider, family

## Tips

1. **Start with DB query** - Get the error frequency first to understand the scope
2. **Check if provider-wide** - One failing model may indicate provider issues
3. **Look at streaming_metrics** - Contains TTFT, token counts useful for debugging
4. **Check recent changes** - Model parameters, provider config, or code changes
5. **Compare with siblings** - Same taxonomy under different providers may help isolate issue
6. **Use time correlation** - Match error spikes with deployments or provider incidents

## Suggested Investigation Flow

1. **Quantify the problem**: Query DB for error frequency and distribution
2. **Identify patterns**: Is it specific error type? Time-based? Traffic-correlated?
3. **Search logs**: Find detailed error messages and stack traces
4. **Examine code paths**: Based on error type, review relevant code
5. **Check external factors**: Provider status, recent deployments, config changes
6. **Propose fix**: Update ERROR_KEYWORDS_MAP, adjust timeouts, escalate to provider, etc.

## Preserving Evidence with Yuppaste

When investigating issues that may lead to a PR fix, **preserve critical evidence** using the `create_yuppaste` MCP tool. It's best to create a separate paste for each distinct piece of evidence (e.g., one for logs, another for database results). This creates shareable links that can be included in PR descriptions.

### What to Preserve

- **Error logs**: Stack traces, streaming errors, and provider responses
- **Error frequency data**: Query results showing error distribution over time
- **Streaming metrics**: TTFT, token counts, latency data from `streaming_metrics`
- **Provider comparison**: Error rates across different providers for same taxonomy

### How to Use

```
create_yuppaste(
    content="<formatted logs or query results>",
    name="Streaming Debug: <model_name or description>"
)
```

The tool returns a go-link (e.g., `http://go/p/<uuid>`) that you can include in PR descriptions. Note that the content size is limited to 10MB.

### PR Description Format

When creating a PR to fix a streaming issue, include:

```markdown
## Investigation Evidence

- Error logs and stack traces: http://go/p/<uuid1>
- Error frequency analysis: http://go/p/<uuid2>
- Streaming metrics: http://go/p/<uuid3>
```

This provides reviewers with full context without cluttering the PR description.

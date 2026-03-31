---
name: review-pr-yupp-mind
description: Repo-specific review guidelines for yupp-mind (Python backend — routing, evaluations, chat completions).
---

# Review PR — yupp-mind

Repo-specific guidelines for reviewing PRs in `yupp-ai/yupp-mind`.

## What to watch for

- **Routing logic**: Changes to model routing, selection, or fallback logic are high-risk. Verify edge cases around model availability, rate limits, and provider failover.
- **Database migrations**: Must be additive (nullable columns or defaults). Verify backward compatibility with running code during deploy.
- **API contract changes**: Backend endpoints consumed by yupp-head. Never remove or rename fields that clients depend on without a transition period.
- **Cost implications**: Changes that affect token usage, model selection, or retry logic can have significant cost impact. Flag any changes that might increase API spend.
- **Evaluation/feedback paths**: Changes to how evaluations are stored or processed affect leaderboard integrity.
- **Provider integrations**: New or modified LLM provider integrations should handle timeouts, streaming errors, and malformed responses gracefully.

# Slack Agent Gateway - Design Document

## Overview

A Slack gateway service that acts as a frontend for multiple AI agents. Each agent has its own Slack app (with unique name and avatar), but all apps route to the same backend service. Users can naturally mention agents like `@Confucius` or `@Socrates`.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Slack Workspace                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ @Confucius  │  │  @Socrates   │  │  @Einstein   │   ...    │
│  │   (App 1)    │  │   (App 2)    │  │   (App 3)    │          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
└─────────┼─────────────────┼─────────────────┼──────────────────┘
          │                 │                 │
          └────────────────┬┴─────────────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │  POST /slack/events  │
                │                      │
                │ Slack Agent Gateway  │──────▶ Agent Service
                │  (identifies app_id, │
                │   routes to agent)   │
                └──────────────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │    Redis    │
                    │  (sessions, │
                    │   buffers,  │
                    │   queues)   │
                    └─────────────┘
```

---

## Key Design Decisions

| Decision | Choice |
|----------|--------|
| Multi-agent support | Multiple Slack apps → single backend service |
| Session ID format | `{channel_id}:{thread_ts}:{app_id}` |
| Thread history | Agent uses MCP tools directly (e.g., `read_slack_thread`) |
| Service authentication | API key in `X-API-Key` header |
| Session expiration | 30 minutes soft expiration (reactivates on new message) |
| Multi-user threads | Include sender info in each message |
| Error handling | Queue messages when Agent Service is down |
| Storage | Redis-only (no database) |
| Rate limiting | Slack Agent Gateway buffers appends, flushes at Slack's pace |

---

## Multi-App Architecture

### Why Multiple Slack Apps?

- Each agent gets a true `@AgentName` mention with autocomplete
- Each agent can have its own avatar and display name
- Single backend service handles all agents
- Clean UX - users interact with distinct bot identities

### How It Works

1. **Create Multiple Slack Apps**: Each agent gets its own Slack app with unique bot name and avatar
2. **Same Webhook URL**: All apps point to the same backend endpoint
3. **Signature Verification**: Since we can't read `api_app_id` until after verification, try verification against all configured signing secrets (fail if none match)
4. **Identify via `api_app_id`**: After signature verification succeeds, parse payload and identify app
5. **Route to Agent**: Backend maps app_id → agent_name and uses correct token for replies

### App Configuration

Store in environment variables or secrets manager:

```
SLACK_APP_CONFUCIOUS_ID: "A123..."
SLACK_APP_CONFUCIOUS_BOT_TOKEN: "xoxb-..."
SLACK_APP_CONFUCIOUS_SIGNING_SECRET: "..."

SLACK_APP_SOCRATES_ID: "A456..."
SLACK_APP_SOCRATES_BOT_TOKEN: "xoxb-..."
SLACK_APP_SOCRATES_SIGNING_SECRET: "..."
```

### Adding a New Agent

1. Create new Slack app in Slack API dashboard
2. Set bot name and avatar
3. Subscribe to events → same webhook URL
4. Install to workspace
5. Add config entry with app_id, token, secret, agent_name

No code deployment needed - just config update.

---

## APIs

### 1. Slack → Slack Agent Gateway (Inbound from Slack)

| Endpoint | Description |
|----------|-------------|
| `POST /slack/events` | Receives Slack events from ALL agent apps. Identifies app via `api_app_id`, creates sessions, forwards to Agent Service. |

**Slack Delivery Semantics:**
- Acknowledge within 3 seconds (return 200 OK immediately, process async)
- Handle retries via `X-Slack-Retry-Num` and `X-Slack-Retry-Reason` headers
- Deduplicate using `event_id` (store in Redis with short TTL, ignore if seen before)
- Idempotent processing to handle duplicate deliveries gracefully

### 2. Slack Agent Gateway → Agent Service (Outbound)

| Endpoint | Description |
|----------|-------------|
| `POST /agents/sessions` | Create a new agent session (body: `agent_name`, `session_id`, `message`) |
| `POST /agents/messages` | Send a new message to the agent (body: `agent_name`, `session_id`, `message`) |

### 3. Agent Service → Slack Agent Gateway (Callbacks)

All callbacks require `X-API-Key` header for authentication.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sessions/info` | POST | Get Slack context for session (body: `session_id`) |
| `/sessions/reply` | POST | Add a new reply message (body: `session_id`, `text`) |
| `/sessions/reply/append` | POST | Append text to last reply (body: `session_id`, `text`) |
| `/sessions/reply/update` | POST | Replace entire content of last reply (body: `session_id`, `text`) |

---

## Data Models

### AgentAppConfig

Configuration for each Slack agent app.

- `app_id` - Slack app ID (e.g., "A123CONFUCIOUS")
- `agent_name` - Internal agent name (e.g., "confucius")
- `bot_token` - Slack bot token for this app
- `signing_secret` - Slack signing secret for this app
- `display_name` - Human-readable name (e.g., "Confucius")

### AgentSession

Tracks a conversation session between Slack and an agent.

Identity:
- `session_id` - Format: `{channel_id}:{thread_ts}:{app_id}`
  - Uses `app_id` (stable) instead of `agent_name` (can be renamed)
  - `thread_ts` is normalized: `event.thread_ts ?? event.ts` (top-level mentions have no thread_ts)
  - Delimiter `:` is safe since channel_id, thread_ts, and app_id don't contain colons

Slack context:
- `channel_id`
- `channel_name` (optional)
- `thread_ts`
- `creator_slack_user_id`
- `creator_slack_username` (optional)
- `app_id` - Which Slack app received this session

Agent context:
- `agent_name`

Reply tracking:
- `last_reply_ts` (optional)
- `last_reply_content`

State:
- `status` - ACTIVE or EXPIRED
- `created_at`
- `last_activity_at`

### Message (sent to Agent Service)

- `text` - Message content
- `sender.slack_user_id` - Who sent the message (primary identifier)
- `sender.username` - Slack username
- `sender.display_name` - Display name
- `ts` - Message timestamp

Note: Email is intentionally excluded from the default payload (PII). Agent Service can fetch user profile on-demand via MCP tools if needed.

### SlackSessionInfoResponse

Response for `POST /sessions/info`

- `session_id`
- `channel_id`
- `channel_name` (optional)
- `thread_ts`
- `creator_slack_user_id`
- `creator_slack_username` (optional)
- `agent_name`
- `status`
- `created_at`
- `last_activity_at`

---

## Session Lifecycle

### State Machine

```
                    ┌──────────────────────────────────────────┐
                    │                                          │
                    ▼                                          │
┌─────────┐    ┌─────────┐    30 min    ┌─────────┐    new    │
│  NEW    │───▶│ ACTIVE  │─────────────▶│ EXPIRED │───────────┘
└─────────┘    └─────────┘  inactivity  └─────────┘  message
                    ▲                        │       reactivates
                    │                        │
                    └────────────────────────┘
                         any activity
```

### Behavior

| State | User sends message | Agent calls callback |
|-------|-------------------|---------------------|
| ACTIVE | Updates `last_activity_at` | Works normally |
| EXPIRED | Reactivates session → ACTIVE | Works normally (reactivates) |

### Storage

Sessions stored in Redis with a longer TTL (e.g., 24 hours) than the soft expiration window. Soft expiration model:
- Session has `expires_at` timestamp field (set to `now + 30 min` on each activity)
- Session has `status` field: ACTIVE (expires_at > now) or EXPIRED (expires_at <= now)
- On access, check `expires_at` to determine if session is expired
- Redis TTL is set longer (e.g., 24 hours) to retain data for reactivation
- Background cleanup job can hard-delete sessions with TTL > 24 hours past expiration

---

## Multi-User Thread Handling

Session ID includes app_id: `{channel_id}:{thread_ts}:{app_id}`

When any user sends a message in the thread, include sender info:

```
POST /agents/messages

{
  "agent_name": "confucius",
  "session_id": "C123:1234567890.123:A123CONFUCIUS",
  "message": {
    "text": "What about happiness?",
    "sender": {
      "slack_user_id": "U456ALICE",
      "username": "alice",
      "display_name": "Alice Smith"
    },
    "ts": "1234567891.456"
  }
}
```

The agent can differentiate users and respond appropriately.

---

## Callback API Behavior

| Method | Slack Agent Gateway Action |
|--------|---------------------------|
| `add_new_reply(text)` | Posts immediately to Slack (new message) |
| `append_to_last_reply(text)` | Buffers text, flushes at Slack's rate limit pace |
| `update_last_reply(text)` | Discards pending buffer, then replaces content with single API call |

---

## Rate Limiting (Append Buffer)

Slack's `chat.update` is a Tier 3 method (~50 requests/minute). The Slack Agent Gateway handles this transparently.

**Rate Limit Handling:**
- Default flush interval: ~1.2s (stays within 50 req/min)
- Adaptive backoff on 429 responses using `Retry-After` header
- Max message length: 40,000 characters (Slack limit)
- Circuit breaker if repeated 429s to prevent cascading failures

### How It Works

```
Agent Service                 Slack Agent Gateway                   Slack
     │                                  │                               │
     │  append("The")                   │                               │
     │─────────────────────────────────▶│                               │
     │  append("journey")               │  ┌─────────────┐              │
     │─────────────────────────────────▶│  │   Buffer    │              │
     │  append("of")                    │  │ "The        │              │
     │─────────────────────────────────▶│  │  journey    │              │
     │  append("a")                     │  │  of a"      │              │
     │─────────────────────────────────▶│  └──────┬──────┘              │
     │                                  │         │ flush (~1.2s)        │
     │                                  │         └─────────────────────▶│ chat.update
     │  append("thousand")              │  ┌─────────────┐              │
     │─────────────────────────────────▶│  │   Buffer    │              │
     │  append("miles")                 │  │ "thousand   │              │
     │─────────────────────────────────▶│  │  miles"     │              │
     │                                  │         │ flush (~1.2s)        │
     │                                  │         └─────────────────────▶│ chat.update
```

### Buffer Storage (Redis)

| Key | Data | TTL |
|-----|------|-----|
| `buffer:{session_id}` | Pending text to append | 5 min |
| `buffer_ts:{session_id}` | Last flush timestamp | 5 min |

### Flush Logic

- Centralized flush manager (single background task) using Redis sorted set
  - Sessions scored by next scheduled flush time
  - Manager wakes at next flush time, processes all due sessions
  - Reschedules processed sessions for next flush
  - More scalable than per-session tasks
- Flushes every ~1.2s (configurable, stays within Slack's Tier 3 rate limit of ~50 req/min)
- Force flush when buffer exceeds max size (e.g., 500 chars)
- On `update_last_reply`, discard pending buffer (no flush needed)

### Agent Service Simplicity

Agent Service just calls append as tokens arrive - no need to know about Slack's rate limits.

---

## Error Handling

### Agent Service Unavailable

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│   Slack API     │────────▶│ Slack Agent GW   │────X───▶│  Agent Service  │
└─────────────────┘         │                  │         └─────────────────┘
                            │  ┌────────────┐  │
                            │  │   Queue    │  │          (unavailable)
                            │  │ (pending   │  │
                            │  │  messages) │  │
                            │  └────────────┘  │
                            └──────────────────┘
```

### Behavior

| Agent Service Status | Slack Agent Gateway Action |
|---------------------|---------------------------|
| Healthy | Forward message immediately |
| Unavailable | Queue message, post status to Slack ("⏳ Message queued...") |
| Recovers | Drain queue, forward pending messages |

### Queue Storage (Redis)

| Key | Data | TTL |
|-----|------|-----|
| `queue:{session_id}` | List of pending messages | 1 hour |

### Agent Service Resume

Agent Service can resume any session by:
1. Calling `POST /sessions/info` with `{ "session_id": "..." }` for context
2. Using MCP tools (`read_slack_thread`) for conversation history

---

## Storage (Redis)

All storage is Redis-based. No database required.

### Key Patterns

| Data | Key Pattern | TTL |
|------|-------------|-----|
| Sessions | `session:{channel_id}:{thread_ts}:{app_id}` | 24 hours (soft expiry via expires_at field) |
| Event dedup | `event:{event_id}` | 5 min |
| Append buffer | `buffer:{session_id}` | 5 min |
| Buffer timestamp | `buffer_ts:{session_id}` | 5 min |
| Message queue | `queue:{session_id}` | 1 hour |

### Benefits

- No database migrations
- Fast reads/writes
- Natural TTL handling
- Easy to scale

---

## Usage Patterns

### Pattern 1: Status → Final Reply

1. `add_new_reply("🤔 Thinking...")`
2. _(processing)_
3. `update_last_reply("Here's your answer: ...")`

### Pattern 2: Streaming Response

1. `add_new_reply("")`
2. `append_to_last_reply(token)` _(repeated for each token - Gateway buffers)_

### Pattern 3: Multi-part Response

1. `add_new_reply("Part 1: ...")`
2. `add_new_reply("Part 2: ...")`
3. `add_new_reply("Part 3: ...")`

### Pattern 4: Status → Streaming

1. `add_new_reply("🤔 Thinking...")`
2. `update_last_reply("")` _(clear for streaming)_
3. `append_to_last_reply(token)` _(repeated)_

---

## Example Flow

1. **User posts in Slack:**
   `@Confucius give me a quote about life`

2. **Slack sends event to Gateway:**
   - Event includes `api_app_id: "A123CONFUCIOUS"`
   - Gateway looks up config: app_id → agent_name = "confucius"
   - Verifies signature with correct signing secret

3. **Slack Agent Gateway:**
   - Creates session in Redis: `session:C123:1234567890.123:A123CONFUCIUS`
   - Calls Agent Service: `POST /agents/sessions`
     - Body: `{ agent_name: "confucius", session_id, message: { text, sender: { ... } } }`

4. **Agent Service:**
   - Fetches context: `POST /sessions/info` with `{ session_id }`
   - Posts status: `POST /sessions/reply` with `{ session_id, text: "🤔 Thinking..." }`
   - Streams response:
     - `POST /sessions/reply/append` with `{ session_id, text: "The " }`
     - `POST /sessions/reply/append` with `{ session_id, text: "journey " }`
     - (Gateway buffers and flushes to Slack at safe rate)

5. **User B sends follow-up in same thread:**
   `Can you explain that?`

6. **Slack Agent Gateway:**
   - Finds existing session
   - Forwards to Agent Service with sender = User B's info

7. **Agent Service:**
   - Sees message from different user
   - Responds: "Of course, @UserB. This quote means..."

---

## Slack App Setup Checklist (Per Agent)

For each new agent, create a Slack app with:

1. **Basic Information**
   - App name: Agent's name (e.g., "Confucius")
   - App icon: Agent's avatar

2. **OAuth & Permissions**
   - Bot Token Scopes:
     - `app_mentions:read` - Receive mention events
     - `chat:write` - Post messages

3. **Event Subscriptions**
   - Request URL: `https://your-service.com/slack/events`
   - Subscribe to bot events:
     - `app_mention` - When bot is @mentioned

4. **Install to Workspace**
   - Install and copy Bot Token

5. **Add to Config**
   - Map app_id → agent_name, bot_token, signing_secret

---

## Implementation Plan

| PR | Title | Description |
|----|-------|-------------|
| **1** | [slack_agent_gateway] Design doc | This document |
| **2** | [slack_agent_gateway] Project setup & data models | Service structure, Pydantic models, Redis client |
| **3** | [slack_agent_gateway] Slack event handling | `/slack/events` endpoint, signature verification |
| **4** | [slack_agent_gateway] Session management | Session CRUD, `POST /sessions/info`, TTL |
| **5** | [slack_agent_gateway] Reply callbacks | `add_new_reply`, `update_last_reply`, API key auth |
| **6** | [slack_agent_gateway] Append buffering | `append_to_last_reply`, buffer, background flush |
| **7** | [slack_agent_gateway] Agent Service integration | Outbound calls, forward messages |
| **8** | [slack_agent_gateway] Message queue & resilience | Queue when Agent Service down |

---

## Summary

| Component | Description |
|-----------|-------------|
| **Multiple Slack Apps** | One per agent, each with unique name/avatar |
| **Single Backend** | All apps route to same `/slack/events` endpoint |
| **Redis Storage** | Sessions, buffers, queues - all ephemeral |
| **Session ID** | `{channel_id}:{thread_ts}:{app_id}` |
| **30 min soft expiry** | Sessions reactivate on new message |
| **Append buffering** | Gateway handles rate limits |
| **Message queue** | Resilience when Agent Service is down |
| **API key auth** | Secures callbacks between services |

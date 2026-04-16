# AGENTS.md — apps/war-room

Next.js 16 admin UI for AHS. Runs standalone on port 3009.

## Stack

- Next.js 16 App Router, React 19, Bun
- shadcn/ui + Base UI, Tailwind v4
- tRPC + TanStack React Query
- `@yupp/agents-protocol`, `@yupp/agents-runtime`, `@yupp/agents-ui` (public npm)

## Backend contract

REST-only against AHS (`AHS_HOST`, `AHS_API_KEY`) and the Slack Agent Gateway (`SLACK_AGENT_GATEWAY_HOST`, `SLACK_AGENT_GATEWAY_API_KEY`). Client code lives in `lib/ahs/` and `lib/slack-agent-gateway/`. Zod schemas are generated from OpenAPI (`lib/ahs-openapi.json`, `lib/sag-openapi.json`).

## Common commands

```bash
bun install
bun run dev        # port 3009
bun run build
bun run lint       # ultracite
bun test
```

## Do not

- Import Python code from `ypl/` — this app is a pure frontend; it talks to AHS over HTTP only.
- Add `workspace:*` or `@repo/*` dependencies. War Room is intentionally standalone; use npm packages only.

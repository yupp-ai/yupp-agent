# AGENTS.md — apps/couch

Next.js 16 web frontend for AHS. Runs standalone on port 3010.

## Stack

- Next.js 16 App Router, React 19, Bun
- shadcn/ui + Base UI, Tailwind v4
- TanStack React Query (no tRPC)
- `@yupp/agents-protocol`, `@yupp/agents-runtime`, `@yupp/agents-ui` (public npm)

## Backend contract

REST + WebSocket against AHS only (`AHS_HOST`, `AHS_API_KEY`). Server-side
client lives in `lib/ahs/server/`. Browser-side fetches go through Server
Actions or `app/api/*` route handlers — never direct.

## Common commands

```bash
bun install
bun run dev        # port 3010
bun run build
bun run lint       # ultracite
bun test
```

## Local dev with a remote AHS

```bash
~/scripts/ahs-tunnel.sh   # forwards :8090 to ahs-mono-prod
# Set AHS_HOST=http://localhost:8090 in .env.local
bun run dev
```

## Do not

- Import Python code from `ypl/` — this app is a pure frontend; it talks to
  AHS over HTTP only.
- Add `workspace:*` or `@repo/*` dependencies. Couch is intentionally
  standalone; use npm packages only.
- Add admin features (agent management, edit prompts, etc.). Those belong
  in `apps/war-room/`.

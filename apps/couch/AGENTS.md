# AGENTS.md — apps/couch

Next.js 16 web frontend for AHS. Runs standalone on port 3010. Production:
`https://couch.agcouch.com`.

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

## Deployment

Deployed by `deploy/bare-metal/deploy-latest.sh`. The script learns Bun apps
through a `BUN_APPS=(apps/couch)` array, runs `bun install --production
&& bun run build` per app, copies the systemd unit at
`apps/couch/deploy/couch.service` to `/etc/systemd/system/`, and restarts
the `couch` service.

Domain: `couch.agcouch.com` (Cloudflare → monolith VM, terminating TLS at
Cloudflare and proxying :3010). The reverse-proxy block is server-side
config and not part of this repo.

## Do not

- Import Python code from `ypl/` — this app is a pure frontend; it talks to
  AHS over HTTP only.
- Add `workspace:*` or `@repo/*` dependencies. Couch is intentionally
  standalone; use npm packages only.
- Add admin features (agent management, edit prompts, etc.). Those belong
  in `apps/war-room/`.

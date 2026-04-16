# War Room

Yupp's command center to create, manage, and monitor AHS agents. Next.js 16 + React 19 frontend that talks REST to the Agent Harness Service and the Slack Agent Gateway.

## Setup

```bash
cd apps/war-room
cp .env.local.example .env.local   # fill in values
bun install
bun run dev                        # http://localhost:3009
```

See `.env.local.example` for required environment variables. `AHS_HOST` should point at your local AHS (see the repo root `CLAUDE.md` for how to start it) or a deployed one.

## Commands

| Command | Purpose |
|---|---|
| `bun run dev` | Dev server on port 3009 |
| `bun run build` | Production build |
| `bun run start` | Serve the production build |
| `bun run lint` | Ultracite lint |
| `bun run fix` | Ultracite auto-fix |
| `bun test` | Unit tests |

## Stack

- Next.js 16 (App Router), React 19, Bun
- shadcn/ui + Base UI, Tailwind v4
- tRPC + TanStack React Query
- `@yupp/agents-protocol` / `@yupp/agents-runtime` / `@yupp/agents-ui` (published on public npm)

## Ported from yupp-head

War Room used to live in `yupp-head/apps/war-room`. It was moved here to co-locate the admin UI with AHS. See the initial port commit for the full diff.

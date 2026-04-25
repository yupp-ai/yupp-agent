# Couch

Devin-style AHS web frontend for end users. Lean Next.js 16 app on port
3010. No admin chrome — that lives in `apps/war-room/`.

## Local dev

```bash
~/scripts/ahs-tunnel.sh             # forwards :8090 to ahs-mono-prod
cp .env.example .env.local          # then fill in AHS_API_KEY + GOOGLE_OAUTH_*
bun install
bun run dev                         # http://localhost:3010
```

## Stack

- Next.js 16 App Router, React 19, Bun
- Tailwind v4 + shadcn/ui (`base-maia` style, light theme)
- TanStack React Query, Zod, `@yupp/agents-ui`, `@yupp/agents-protocol`
- Auth, AHS REST client, and WebSocket hook copied from `apps/war-room`

## What's wired

- Sessions list (mine_only) in left rail with Pin / Rename (localStorage)
- Landing prompt with agent dropdown (last selection persisted)
- Session view: chat (history + WS live items), follow-up input, status pill
- Right artifact panel with tabs — TEXT (markdown) and PR (link) rendered
  inline; other types fall back to a download CTA
- Session …-menu: Copy ID, Stop, Toggle tool calls, Rename, Pin
- Google OAuth — verified Google account + present in AHS users DB (via `resolveUserByEmail`)

See `AGENTS.md` for backend contract and conventions. Design and roadmap:
`docs/frontend/2026-04-24-couch-ahs-frontend-prd.md`. Plan:
`docs/frontend/2026-04-24-couch-ahs-frontend-plan.md`.

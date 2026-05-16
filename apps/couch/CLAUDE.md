# CLAUDE.md — apps/couch

Next.js 15 web frontend for AHS. Runs standalone on port 3010.
Production: `https://couch.agcouch.com`.

## Stack

- Next.js 15.1 (App Router), React 19, Node + npm
- Vanilla server / client components (no shadcn, no tRPC, no @yupp/* deps)
- react-markdown + remark-gfm, zod, @playwright/test

## Backend contract

REST + WebSocket against AHS only. Auth + key handling:

- `AHS_BASE_URL`, `AHS_API_KEY` are server-side only. The browser never
  sees `AHS_API_KEY`.
- Server components call AHS directly via `lib/ahs.ts` (raw `fetch` with
  `X-API-Key` header).
- Browser HTTP calls hit the proxy at `/api/ahs/[...path]/route.ts`,
  which checks the Couch session cookie via `getInternalSession()` and
  injects `X-API-Key` server-side before forwarding to AHS.
- Browser WebSocket opens `/api/ahs/session/{id}/ws` with no auth in the
  URL; a `beforeFiles` rewrite in `next.config.mjs` injects
  `?api_key=<server env>` and forwards the upgrade to AHS.
- Couch auth is Google OAuth → signed session cookie. `middleware.ts`
  gates everything except `/login`, `/api/authentication/*`,
  `/api/healthz`, and `/_next/*`.

## Common commands

```bash
npm install
npm run dev          # port 3010
npm run build
npm run typecheck    # tsc --noEmit
npm run lint
npm run test:e2e     # playwright smoke tests
```

## Local dev with a remote AHS

```bash
~/scripts/ahs-tunnel.sh   # forwards :8090 to ahs-mono-prod
# Set AHS_BASE_URL=http://localhost:8090 in .env.local
npm run dev
```

To skip Google login locally:

```bash
# .env.local
COUCH_DEV_BYPASS_AUTH_EMAIL=tian.wang@angellist.com
COUCH_DEV_BYPASS_AUTH_USER_ID=<ahs-user-id>
```

## Deployment

Currently NOT auto-deployed. The systemd unit at `deploy/couch.service`
is wired up but `deploy/bare-metal/deploy-latest.sh` leaves
`SERVICES`/`NPM_APPS` empty for Couch. To enable: install Node + npm on
ahs-mono-prod, then add `couch` to `SERVICES` and `apps/couch` to
`NPM_APPS`.

Domain: `couch.agcouch.com` (Cloudflare → monolith VM, terminating TLS
at Cloudflare and proxying :3010).

## Do not

- Import Python code from `ypl/` — pure frontend; talks to AHS over
  HTTP/WS only.
- Add `workspace:*` or `@repo/*` dependencies. Couch is intentionally
  standalone; npm packages only.
- Re-introduce `NEXT_PUBLIC_AHS_API_KEY` or any other public env that
  carries the AHS API key. The proxy in `/api/ahs/[...path]` and the
  WS rewrite are the only paths the browser uses.

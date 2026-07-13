# couch

Next.js 15 (App Router) front-end for AHS. Runs standalone on port 3010.
Production: `https://couch.example.com`.

Replaces the previous Bun + Next 16 + heavy tRPC stack — a single
ported app with all user and admin pages included.

## Stack

- Next.js 15.1, React 19, Node + npm
- Vanilla server components and client components (no shadcn / no tRPC)
- React-markdown + remark-gfm for chat rendering
- zod for input validation
- @playwright/test for the smoke suite

## Setup

```bash
cd apps/couch
cp .env.example .env.local
# Fill in AHS_BASE_URL, AHS_API_KEY, GOOGLE_CLIENT_ID/SECRET, AUTH_SECRET.
npm install
npm run dev          # http://localhost:3010
```

For local development against a remote AHS, run `~/scripts/ahs-tunnel.sh`
first to forward `localhost:8090` to `your-vm-host`.

To skip the Google login flow locally (e.g., for Playwright):

```bash
# .env.local
COUCH_DEV_BYPASS_AUTH_EMAIL=admin@example.com
COUCH_DEV_BYPASS_AUTH_USER_ID=<your-ahs-user-id>
```

## Backend contract

- All AHS traffic flows through the Couch server. The AHS API key
  (`AHS_API_KEY`) lives only on the server.
- Server components call AHS directly via `lib/ahs.ts`.
- Browser HTTP traffic hits the proxy at `/api/ahs/[...path]`, which
  verifies the session cookie and injects `X-API-Key` server-side.
- Browser WebSocket connects to `/api/ahs/session/{id}/ws`; the
  `beforeFiles` rewrite in `next.config.mjs` forwards the upgrade to AHS
  with `api_key=<server env>` appended. Browser never sees the key.
- Auth: Google OAuth (server-side); session is a signed cookie. Routes
  are gated by `middleware.ts`.

## Common commands

```bash
npm run dev          # port 3010
npm run build        # production build
npm run typecheck    # tsc --noEmit
npm run lint         # next lint
npm run test:e2e     # playwright smoke tests
```

## Tests

`tests/e2e/smoke.spec.ts` covers the auth gate without needing AHS:
- `/` redirects to `/login`
- `/login` renders the Sign in button
- protected routes preserve `?redirectTo=`
- the logo asset loads

Run via `npx playwright test`. Playwright spins up `npm run dev` itself
with stub env vars (see `playwright.config.ts`).

## Deployment

The systemd unit at `deploy/couch.service` expects:
- `npm` on PATH
- `/data/ahs/.env` with `AHS_BASE_URL`, `AHS_API_KEY`,
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `AUTH_SECRET`,
  `OAUTH_REDIRECT_HOST`.

When ready to enable on `your-vm-host`, add `couch` to `SERVICES` and
`apps/couch` to `NPM_APPS` in `deploy/bare-metal/deploy-latest.sh`, and
ensure Node + npm are installed system-wide on the VM.

Domain: `couch.example.com` (Cloudflare → monolith VM → :3010).

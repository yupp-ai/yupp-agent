---
name: review-pr-yupp-head
description: Repo-specific review guidelines for yupp-head (Next.js frontend — chat UI, leaderboard, admin dashboards).
---

# Review PR — yupp-head

Repo-specific guidelines for reviewing PRs in `yupp-ai/yupp-head`.

## What to look for

**Read `CODE-REVIEW-GUIDELINES.md` at the repo root before every review.** It defines the substantive concerns to evaluate: deployment safety, backwards compatibility, flag protection, PR size, maintainability, correctness, bundle boundaries, type safety, naming, visual verification, and architecture consistency.

## Deployment safety

We deploy twice a day. Users can have stale client code for 3–7 days. Database migrations and the Python backend (yupp-mind) may deploy before or after the Next.js frontend.

- **Never remove or rename a server endpoint, tRPC action, or API response field** that clients depend on without a transition period.
- **Database migrations must be additive.** Columns can be added (nullable or with defaults) but not removed.
- **Feature flags**: New user-facing behavior must be behind a flag unless trivially low-risk. Test both flag states.
- **Stale client awareness**: tRPC actions and API routes are vulnerable to stale clients. Server-component props are safe due to build-ID mechanism.

## Additional repo-specific steps

- **Validate visual changes** using dev browser or preview deploys. Check both desktop and mobile viewports.
- **Refer to `SMALL-PRs.md`** and ask the author to split the PR if it exceeds ~500 lines.
- **Feature flag CLI**: Use `pnpm --filter @yupp/web-e2e-tests encrypt-flags` to force flag states for testing.
- **Bundle size**: Server-only code must not leak into the client bundle. Heavy imports should be dynamically imported.
- **Component API design**: Props should work independently. Avoid `!important`. Prefer `className` over custom styling props.

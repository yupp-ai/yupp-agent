---
name: review-pr-yupp-soul
description: Repo-specific review guidelines for yupp-soul (internal admin dashboard — Next.js + tRPC).
---

# Review PR — yupp-soul

Repo-specific guidelines for reviewing PRs in `yupp-ai/yupp-soul`.

## What to watch for

- **Admin action safety**: Admin actions should log to Slack for audit trails. Verify that destructive operations have confirmation steps.
- **Role-based access control**: Ensure proper role checks (ADMIN, READONLY). Non-production environments auto-assign ADMIN — verify production role checks are in place.
- **API integration**: All calls go through `fetchYuppMind`. Verify proper error handling for backend failures.
- **Authentication**: NextAuth.js with Google OAuth. Verify auth guards on new routes/pages.
- **tRPC patterns**: Follow existing router patterns in `lib/trpc/routers/`. Use Zod for validation.

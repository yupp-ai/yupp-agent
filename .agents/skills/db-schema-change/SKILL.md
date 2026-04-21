---
name: db-schema-change
description: Safely add, modify, or drop DB schema via an Alembic migration. Always uses ``alembic revision --autogenerate`` against a running local Postgres so the generated migration, ``current_head.txt``, and the new revision ID all stay in sync. Enforces the "DB schema changes ship in their own PR, separate from feature work" rule.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
---

# DB Schema Change

Anything that adds, alters, or drops columns / tables / indexes / enums in
the application database.

## Hard rules

1. **Each DB schema change ships in its own PR.** No mixing migrations with
   feature code. Reviewers need to see the migration in isolation; deploys
   need to apply it cleanly ahead of the code that depends on it.
2. **Never hand-write the migration file.** Always autogenerate it from the
   ORM diff against a live Postgres — hand-written files miss enum/index
   nuances and desync from `current_head.txt`.
3. **Always update `ypl/db/alembic/current_head.txt`** alongside the
   migration. The ``alembic.ini`` post-write hook does this for you when
   you use ``alembic revision --autogenerate`` — do not edit it by hand.

## Workflow

### 1. Start clean

```bash
cd /path/to/yupp-agent
git checkout main && git pull --ff-only
git checkout -b db-schema/<short-description>
```

### 2. Make sure local Postgres is at the current head

Autogenerate diffs the ORM against the live DB, so the DB must already be
at the revision recorded in ``current_head.txt`` — otherwise every
missing-since-then migration will also show up in the output.

**Do not start a database automatically.** The operator is expected to have
one already running (either via ``docker compose``, a native Homebrew /
systemd service, or a hosted instance). Check the connection first; if it
fails, surface a clear warning and *ask* before trying to bring one up.

```bash
# Sanity-check the connection using whatever POSTGRES_* settings are in .env.
# alembic exits non-zero if it can't reach the DB.
poetry run alembic -c alembic.ini current
```

If that command fails to connect, stop and tell the operator. Offer (don't
execute) something like:

> Local Postgres is not reachable at `<host:port>`. If you want me to start
> the docker-compose service, run:
>
>     docker compose up -d postgres redis
>
> Then re-run step 2.

Once the DB is reachable, apply any pending migrations and verify the head
matches the file:

```bash
poetry run alembic -c alembic.ini upgrade head
poetry run alembic -c alembic.ini current    # should match current_head.txt
cat ypl/db/alembic/current_head.txt
```

### 3. Edit the ORM model

Modify the appropriate file under ``ypl/db/`` — e.g. add nullable columns,
rename a field, drop a table. Keep the ORM change minimal and focused on
the schema-level diff. Avoid mixing business logic updates here (those
belong in a follow-up PR).

### 4. Autogenerate the migration

```bash
poetry run alembic -c alembic.ini revision --autogenerate \
  -m "<short imperative sentence describing the change>"
```

This generates:

- ``ypl/db/alembic/versions/<timestamp>-<revid>_<slug>.py`` — the migration
- **Updates ``ypl/db/alembic/current_head.txt``** via the
  ``write_current_head`` post-hook in ``alembic.ini``

### 5. Review the generated file

Open the new file. Alembic autogenerate is good but not perfect. Check for:

- **Column renames** — autogenerate emits ``drop_column`` + ``add_column``,
  losing data. Replace with ``op.alter_column(..., new_column_name=...)``.
- **Enum changes** — autogenerate often misses enum value additions /
  renames. Verify the generated SQL matches the intent.
- **Index / constraint regressions** — cross-check against other recent
  migrations in ``versions/`` for style consistency.
- **Unrelated noise** — if autogenerate picks up drift that is not part of
  this change, the local DB is out of sync. Stop and re-run step 2.

Edit the file directly if adjustments are needed; do **not** regenerate
unless the intent changed.

### 6. Apply locally and test

```bash
poetry run alembic -c alembic.ini upgrade head

# Optional: round-trip the downgrade to make sure it's reversible.
poetry run alembic -c alembic.ini downgrade -1
poetry run alembic -c alembic.ini upgrade head

# Run the alembic test suite (requires Postgres):
ALEMBIC_TEST_DB_URL=postgresql://postgres:postgres@localhost:5432/test_yadb \
  poetry run pytest --test-alembic tests/conftest.py tests/alembic/
```

### 7. Commit and open the PR

```bash
git add ypl/db/ ypl/db/alembic/
git commit -m "schema: <short description>"
git push -u origin HEAD
gh pr create --title "schema: <short description>" --body "..."
```

**The PR should contain only:**

- The ORM change under ``ypl/db/``
- The generated migration under ``ypl/db/alembic/versions/``
- The updated ``ypl/db/alembic/current_head.txt``
- Nothing else. If the feature that depends on this schema also needs
  code changes, open a second PR that stacks on this one once it's
  merged.

## Common mistakes

- **Hand-editing ``current_head.txt``** — always let the alembic post-hook
  write it. Editing by hand leads to conflict markers that pass ruff /
  mypy but fail at deploy time.
- **Generating the migration against an out-of-date DB** — leads to the
  new file including unrelated "catch-up" changes. Fix by running
  ``alembic upgrade head`` first.
- **Bundling the schema change into the feature PR** — forbidden. Even
  for small additions, the migration lands first in its own PR.
- **Picking your own revision ID** — don't. Alembic's auto-generated ID
  is fine and the file name encodes the creation timestamp for ordering.

## Skipping this skill

The only acceptable reasons to bypass this workflow:

- The change is purely a data backfill with no schema delta → still use
  an alembic revision, but a blank ``--autogenerate`` (no diff) and a
  hand-written ``upgrade()`` that runs the backfill SQL.
- You're rebasing an existing schema PR onto a new head → re-run step 2,
  regenerate, delete the stale file, force-push.

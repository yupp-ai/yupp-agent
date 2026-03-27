---
name: query-local-db
description: Query the local PostgreSQL database for debugging during local development. Use when testing features locally and need to verify data was persisted correctly.
allowed-tools: Bash
---

# Query Local Database

Use this skill when running the backend locally and need to query the local PostgreSQL database to verify data, debug issues, or inspect persisted state.

## Prerequisites

- Local PostgreSQL must be running (typically via Docker or local install)
- Optionally seed with realistic data from staging or production (see `install_local_pg.sh` and `seed_data_from_source.sh`)

## Connection

The connection URL depends on your local setup. Common configurations:

```bash
# Default local setup (password: local)
psql "postgresql://postgres:local@127.0.0.1:5432/yuppdb"

# Using environment variable (recommended)
psql "$DATABASE_URL"
```

**Note**: If using docker-compose, check your `POSTGRES_HOST_PORT` and `POSTGRES_*` environment variables.

## Quick Queries

### Run a single query
```bash
psql "$DATABASE_URL" -c "SELECT * FROM table_name LIMIT 5"
```

### Get table schema
```bash
psql "$DATABASE_URL" -c "\d table_name"
```

### List all tables
```bash
psql "$DATABASE_URL" -c "\dt"
```

## Common Debug Queries

### Check recent chat messages with context_metadata
```sql
SELECT
    cm.message_id,
    cm.created_at,
    cm.context_metadata,
    lm.label as model_label
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
ORDER BY cm.created_at DESC
LIMIT 10
```

### Find messages by model label
```sql
SELECT cm.message_id, cm.created_at, cm.context_metadata
FROM chat_messages cm
JOIN language_models lm ON cm.assistant_language_model_id = lm.language_model_id
WHERE lm.label ILIKE '%Nano Banana%'
ORDER BY cm.created_at DESC
LIMIT 5
```

### Find a specific message by ID
```sql
SELECT * FROM chat_messages WHERE message_id = '<uuid>'
```

## Schema Reference

For comprehensive schema documentation, see the `/fetch-from-db` skill which covers:
- Language models and taxonomies
- Chats, turns, and messages
- Routing info
- Evaluations and feedback
- Promotions and cost metrics

## Key Differences from Production

- Local DB may be seeded from staging or production data
- Connection uses local host/port (check your environment variables)
- No read replicas - queries go directly to local Postgres
- Data may be stale compared to production/staging

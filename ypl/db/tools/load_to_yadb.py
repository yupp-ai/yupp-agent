#!/usr/bin/env python3
"""Load CSV dump files into the new yadb database.

Usage:
    python -m ypl.db.tools.load_to_yadb \
        --host 34.48.17.132 --port 6432 --db yadb \
        --user schema_manager --password '<PASSWORD>' \
        --input-dir ./dump

Reads CSV files produced by dump_from_yuppdb.py and loads them into yadb.
Tables are loaded in dependency order (parents before children).
Existing rows with conflicting PKs are skipped (ON CONFLICT DO NOTHING).

IMPORTANT: Run alembic migrations on yadb BEFORE running this script so that
all tables and enum types exist.
"""

import argparse
import csv
import os
import sys

import psycopg2
import psycopg2.extras

# Load order respects foreign key dependencies (parents first).
# Each entry: (csv_filename_without_ext, table_name)
LOAD_ORDER = [
    # Independent tables
    ("users", "users"),
    ("roles", "roles"),
    ("agents", "agents"),
    ("mcp_dev_tokens", "mcp_dev_tokens"),
    ("slack_agents", "slack_agents"),
    ("slack_oauth_tokens", "slack_oauth_tokens"),
    # Depends on roles
    ("role_permissions", "role_permissions"),
    # Depends on users + roles
    ("user_roles", "user_roles"),
    # Depends on agents
    ("agent_sessions", "agent_sessions"),
    ("agent_schedules", "agent_schedules"),
    ("agent_projects", "agent_projects"),
    ("agent_memory_sections", "agent_memory_sections"),
    # Depends on agent_sessions
    ("agent_session_messages", "agent_session_messages"),
    ("agent_feedbacks", "agent_feedbacks"),
    # Depends on agent_schedules
    ("agent_schedule_runs", "agent_schedule_runs"),
    # Depends on agent_projects
    ("agent_tasks", "agent_tasks"),
    # Depends on agent_memory_sections
    ("agent_memory_section_embeddings", "agent_memory_section_embeddings"),
    # Depends on mcp_dev_tokens
    ("mcp_audit_logs", "mcp_audit_logs"),
    # Depends on yuppaste_comment_threads
    ("yuppaste_comment_threads", "yuppaste_comment_threads"),
    ("yuppaste_comments", "yuppaste_comments"),
]

# Primary key columns for ON CONFLICT DO NOTHING.
TABLE_PKS: dict[str, list[str]] = {
    "users": ["user_id"],
    "roles": ["role_id"],
    "role_permissions": ["role_id", "permission"],
    "user_roles": ["user_id", "role_id"],
    "agents": ["agent_id"],
    "agent_sessions": ["agent_session_id"],
    "agent_session_messages": ["agent_session_message_id"],
    "agent_feedbacks": ["agent_feedback_id"],
    "agent_schedules": ["agent_schedule_id"],
    "agent_schedule_runs": ["agent_schedule_run_id"],
    "agent_projects": ["agent_project_id"],
    "agent_tasks": ["agent_task_id"],
    "agent_memory_sections": ["agent_memory_section_id"],
    "agent_memory_section_embeddings": ["agent_memory_section_embedding_id"],
    "mcp_dev_tokens": ["mcp_dev_token_id"],
    "mcp_audit_logs": ["mcp_audit_log_id"],
    "slack_agents": ["slack_agent_id"],
    "slack_oauth_tokens": ["token_id"],
    "yuppaste_comment_threads": ["thread_id"],
    "yuppaste_comments": ["comment_id"],
}


def load_table(conn, table: str, csv_path: str, batch_size: int = 1000) -> int:
    """Load a CSV file into a table. Returns rows inserted."""
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            print(f"  {table}: SKIPPED (empty CSV)")
            return 0

        columns = list(reader.fieldnames)
        rows = list(reader)

    if not rows:
        print(f"  {table}: 0 rows (empty)")
        return 0

    # Build INSERT ... ON CONFLICT DO NOTHING
    col_list = ", ".join(columns)
    placeholders = ", ".join([f"%({c})s" for c in columns])
    pk_cols = TABLE_PKS.get(table, [])
    if pk_cols:
        conflict_clause = f"ON CONFLICT ({', '.join(pk_cols)}) DO NOTHING"
    else:
        conflict_clause = ""

    query = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_clause}"

    # Replace empty strings with None for nullable columns
    for row in rows:
        for k, v in row.items():
            if v == "":
                row[k] = None

    cur = conn.cursor()
    inserted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            psycopg2.extras.execute_batch(cur, query, batch, page_size=batch_size)
            inserted += len(batch)
        except Exception as e:
            conn.rollback()
            print(f"  {table}: ERROR at batch {i // batch_size} ({e})")
            return inserted

    conn.commit()
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Load CSV dump into yadb")
    parser.add_argument("--host", required=True, help="Database host (pgbouncer IP or Cloud SQL IP)")
    parser.add_argument("--port", type=int, default=6432, help="Database port (default: 6432 for pgbouncer)")
    parser.add_argument("--db", default="yadb", help="Target database name")
    parser.add_argument("--user", default="schema_manager", help="Database user (needs write access)")
    parser.add_argument("--password", required=True, help="Database password")
    parser.add_argument("--input-dir", required=True, help="Directory containing CSV files from dump script")
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per INSERT batch")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be loaded without executing")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — no data will be written.\n")
        for csv_name, table in LOAD_ORDER:
            path = os.path.join(args.input_dir, f"{csv_name}.csv")
            if os.path.exists(path):
                with open(path) as f:
                    count = sum(1 for _ in f) - 1
                print(f"  {csv_name}.csv -> {table}: {max(count, 0)} rows")
            else:
                print(f"  {csv_name}.csv: NOT FOUND")
        return

    print(f"Connecting to {args.host}:{args.port}/{args.db} as {args.user} ...")
    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.db,
        user=args.user,
        password=args.password,
        sslmode="require",
    )

    # Temporarily disable FK checks for bulk load
    cur = conn.cursor()
    cur.execute("SET session_replication_role = 'replica';")
    conn.commit()

    total = 0
    for csv_name, table in LOAD_ORDER:
        path = os.path.join(args.input_dir, f"{csv_name}.csv")
        if not os.path.exists(path):
            print(f"  {csv_name}.csv: NOT FOUND, skipping")
            continue

        count = load_table(conn, table, path, batch_size=args.batch_size)
        print(f"  {table}: {count} rows loaded")
        total += count

    # Re-enable FK checks
    cur = conn.cursor()
    cur.execute("SET session_replication_role = 'origin';")
    conn.commit()

    conn.close()
    print(f"\nDone. {total} total rows loaded into {args.db}.")


if __name__ == "__main__":
    main()

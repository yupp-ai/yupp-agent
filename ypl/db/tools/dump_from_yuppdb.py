#!/usr/bin/env python3
"""Dump relevant tables from the old yuppdb (yupp-mind) to CSV files.

Usage:
    python -m ypl.db.tools.dump_from_yuppdb \
        --host 34.48.17.132 --port 6432 --db yuppdb \
        --user developer --password '<PASSWORD>' \
        --output-dir ./dump

Connects to the old prod yuppdb (via pgbouncer) and exports only the tables
and columns needed by yupp-agent into a directory of CSV files.

Tables with name changes (soul_* → new names) are exported under their NEW names.
The users table is exported with only the subset of columns needed by yupp-agent.
"""

import argparse
import os
import sys

import psycopg2

# Tables that map 1:1 (same name, same columns) — dump all columns.
DIRECT_TABLES = [
    "agents",
    "agent_sessions",
    "agent_session_messages",
    "agent_feedbacks",
    "agent_schedules",
    "agent_schedule_runs",
    "agent_projects",
    "agent_tasks",
    "agent_memory_sections",
    "agent_memory_section_embeddings",
    "mcp_dev_tokens",
    "mcp_audit_logs",
    "slack_agents",
    "slack_oauth_tokens",
    "yuppaste_comment_threads",
    "yuppaste_comments",
]

# Tables that need renaming: (old_table, new_table, column_select)
# column_select is None for "all columns" or a list of columns to select.
RENAMED_TABLES = [
    ("soul_roles", "roles", ["role_id", "name", "description", "created_at", "modified_at", "deleted_at"]),
    ("soul_role_permissions", "role_permissions", ["role_id", "permission"]),
    ("soul_user_roles", "user_roles", ["user_id", "role_id"]),
]

# Users table: only export columns that exist in yupp-agent's User model.
USERS_COLUMNS = ["user_id", "name", "email", "image", "status", "created_at", "modified_at", "deleted_at"]


def dump_table(cursor: psycopg2.extensions.cursor, table: str, output_dir: str, columns: str = "*", output_name: str | None = None) -> int:
    """Dump a table to CSV. Returns row count."""
    output_name = output_name or table
    path = os.path.join(output_dir, f"{output_name}.csv")

    query = f"COPY (SELECT {columns} FROM {table}) TO STDOUT WITH CSV HEADER"
    with open(path, "w") as f:
        cursor.copy_expert(query, f)

    # Count rows (header excluded)
    with open(path) as f:
        count = sum(1 for _ in f) - 1
    return max(count, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump yupp-agent tables from old yuppdb")
    parser.add_argument("--host", required=True, help="Database host (pgbouncer IP or Cloud SQL IP)")
    parser.add_argument("--port", type=int, default=6432, help="Database port (default: 6432 for pgbouncer)")
    parser.add_argument("--db", default="yuppdb", help="Source database name")
    parser.add_argument("--user", default="developer", help="Database user")
    parser.add_argument("--password", required=True, help="Database password")
    parser.add_argument("--output-dir", required=True, help="Directory to write CSV files to")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Connecting to {args.host}:{args.port}/{args.db} as {args.user} ...")
    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.db,
        user=args.user,
        password=args.password,
        sslmode="require",
    )
    conn.set_session(readonly=True)
    cur = conn.cursor()

    total = 0

    # 1. Direct tables (same name, all columns)
    for table in DIRECT_TABLES:
        try:
            count = dump_table(cur, table, args.output_dir)
            print(f"  {table}: {count} rows")
            total += count
        except Exception as e:
            print(f"  {table}: SKIPPED ({e})")
            conn.rollback()

    # 2. Renamed tables
    for old_name, new_name, columns in RENAMED_TABLES:
        try:
            cols = ", ".join(columns) if columns else "*"
            count = dump_table(cur, old_name, args.output_dir, columns=cols, output_name=new_name)
            print(f"  {old_name} -> {new_name}: {count} rows")
            total += count
        except Exception as e:
            print(f"  {old_name} -> {new_name}: SKIPPED ({e})")
            conn.rollback()

    # 3. Users (subset of columns)
    try:
        cols = ", ".join(USERS_COLUMNS)
        count = dump_table(cur, "users", args.output_dir, columns=cols)
        print(f"  users (subset): {count} rows")
        total += count
    except Exception as e:
        print(f"  users: SKIPPED ({e})")
        conn.rollback()

    cur.close()
    conn.close()
    print(f"\nDone. {total} total rows dumped to {args.output_dir}/")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Dump relevant tables from the old yuppdb (yupp-mind) to CSV files.

Usage:
    export SOURCE_DB='postgresql://user:pass@host:port/yuppdb'
    python -m ypl.db.tools.dump_from_yuppdb --output-dir ./dump

    # Or pass directly:
    python -m ypl.db.tools.dump_from_yuppdb --source "$SOURCE_DB" --output-dir ./dump

Tables with name changes (soul_* → new names) are exported under their NEW names.
The users table is exported with only the subset of columns needed by yupp-agent.
"""

import argparse
import os

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
    "agent_security_incidents",
    "agent_artifacts",
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
RENAMED_TABLES = [
    ("soul_roles", "roles", ["role_id", "name", "description", "created_at", "modified_at", "deleted_at"]),
    ("soul_role_permissions", "role_permissions", ["role_id", "permission"]),
    ("soul_user_roles", "user_roles", ["user_id", "role_id"]),
]

# Users table: only export columns that exist in yupp-agent's User model.
USERS_COLUMNS = ["user_id", "name", "email", "image", "status", "created_at", "modified_at", "deleted_at"]


def dump_table(
    cursor: psycopg2.extensions.cursor,
    table: str,
    output_dir: str,
    columns: str = "*",
    output_name: str | None = None,
) -> int:
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
    parser.add_argument(
        "--source",
        default=os.environ.get("SOURCE_DB"),
        help="Source connection string (or set SOURCE_DB env var)",
    )
    parser.add_argument("--output-dir", required=True, help="Directory to write CSV files to")
    args = parser.parse_args()

    if not args.source:
        parser.error("--source is required (or set SOURCE_DB env var)")

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Connecting to source database ...")
    conn = psycopg2.connect(args.source, sslmode="require")
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

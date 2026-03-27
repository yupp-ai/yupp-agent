#!/usr/bin/env python3
"""Load CSV dump files into the new yadb database.

Usage:
    export DEST_DB='postgresql://user:pass@host:port/yadb'

    # Create tables first (only needed once):
    python -m ypl.db.tools.load_to_yadb --dest "$DEST_DB" --create-tables

    # Then load data:
    python -m ypl.db.tools.load_to_yadb --dest "$DEST_DB" --input-dir ./dump

    # Dry-run preview:
    python -m ypl.db.tools.load_to_yadb --input-dir ./dump --dry-run
"""

import argparse
import csv
import os
import sys

import psycopg2
import psycopg2.extras

# Bump CSV field size limit for large JSONB fields (raw_events, etc.)
csv.field_size_limit(sys.maxsize)

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
    ("agent_security_incidents", "agent_security_incidents"),
    # Depends on agent_schedules
    ("agent_schedule_runs", "agent_schedule_runs"),
    # Depends on agent_projects
    ("agent_tasks", "agent_tasks"),
    # Depends on agents + sessions + tasks
    ("agent_artifacts", "agent_artifacts"),
    # Depends on agent_memory_sections
    ("agent_memory_section_embeddings", "agent_memory_section_embeddings"),
    # mcp_audit_logs skipped — too large, not needed for migration
    # Yuppaste
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
    "agent_security_incidents": ["incident_id"],
    "agent_artifacts": ["agent_artifact_id"],
    "agent_memory_sections": ["agent_memory_section_id"],
    "agent_memory_section_embeddings": ["agent_memory_section_embedding_id"],
    "mcp_dev_tokens": ["mcp_dev_token_id"],
    "mcp_audit_logs": ["mcp_audit_log_id"],
    "slack_agents": ["slack_agent_id"],
    "slack_oauth_tokens": ["token_id"],
    "yuppaste_comment_threads": ["thread_id"],
    "yuppaste_comments": ["comment_id"],
}


def create_tables(dest: str) -> None:
    """Create all tables from SQLModel metadata using SQLAlchemy."""
    from sqlalchemy import create_engine

    import sqlmodel  # noqa: F401 — registers SQLModel metadata

    from ypl.db.all_models import all_models  # noqa: F841 — populates metadata

    engine = create_engine(dest)
    sqlmodel.SQLModel.metadata.create_all(engine)
    engine.dispose()
    print("Tables created successfully.")


def load_table(conn, table: str, csv_path: str, batch_size: int = 500) -> tuple[int, int]:
    """Load a CSV file into a table row-by-row, skipping FK failures. Returns (inserted, skipped)."""
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return 0, 0

        columns = list(reader.fieldnames)
        rows = list(reader)

    if not rows:
        return 0, 0

    col_list = ", ".join(columns)
    placeholders = ", ".join([f"%({c})s" for c in columns])
    pk_cols = TABLE_PKS.get(table, [])
    conflict_clause = f"ON CONFLICT ({', '.join(pk_cols)}) DO NOTHING" if pk_cols else ""
    query = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_clause}"

    # Replace empty strings with None
    for row in rows:
        for k, v in row.items():
            if v == "":
                row[k] = None

    inserted = 0
    skipped = 0
    cur = conn.cursor()

    # Try batch first (fast path)
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            psycopg2.extras.execute_batch(cur, query, batch, page_size=batch_size)
            conn.commit()
            inserted += len(batch)
        except Exception:
            conn.rollback()
            # Fall back to row-by-row for this batch
            for row in batch:
                try:
                    cur.execute(query, row)
                    conn.commit()
                    inserted += 1
                except Exception:
                    conn.rollback()
                    skipped += 1

    return inserted, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Load CSV dump into yadb")
    parser.add_argument(
        "--dest",
        default=os.environ.get("DEST_DB"),
        help="Destination connection string (or set DEST_DB env var)",
    )
    parser.add_argument("--input-dir", help="Directory containing CSV files from dump script")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be loaded without executing")
    parser.add_argument("--create-tables", action="store_true", help="Create tables from SQLModel metadata then exit")
    args = parser.parse_args()

    if args.dry_run:
        if not args.input_dir:
            parser.error("--input-dir is required for --dry-run")
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

    if not args.dest:
        parser.error("--dest is required (or set DEST_DB env var)")

    if args.create_tables:
        create_tables(args.dest)
        return

    if not args.input_dir:
        parser.error("--input-dir is required when loading data")

    print("Connecting to destination database ...")
    conn = psycopg2.connect(args.dest, sslmode="require")

    total = 0
    total_skipped = 0
    for csv_name, table in LOAD_ORDER:
        path = os.path.join(args.input_dir, f"{csv_name}.csv")
        if not os.path.exists(path):
            print(f"  {csv_name}.csv: NOT FOUND, skipping")
            continue

        inserted, skipped = load_table(conn, table, path)
        skip_msg = f" ({skipped} skipped)" if skipped else ""
        print(f"  {table}: {inserted} rows loaded{skip_msg}")
        total += inserted
        total_skipped += skipped

    conn.close()
    print(f"\nDone. {total} rows loaded, {total_skipped} skipped.")


if __name__ == "__main__":
    main()

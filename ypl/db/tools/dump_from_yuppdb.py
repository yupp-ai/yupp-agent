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
    # mcp_audit_logs skipped — too large, not needed for migration
    "slack_agents",
    "slack_oauth_tokens",
    "yuppaste_comment_threads",
    "yuppaste_comments",
]

# Roles that exist in yupp-agent's RoleName enum.
VALID_ROLES = ("ADMIN", "READONLY", "ADMIN_AGENT", "ENGINEER", "YUPPASTE_USER", "MCP_USER")
_ROLE_IN = "(" + ", ".join(f"'{r}'" for r in VALID_ROLES) + ")"

# Permissions that exist in yupp-agent's Permission enum.
VALID_PERMISSIONS = (
    "read_users",
    "write_users",
    "manage_rbac",
    "READ_YUPPASTE",
    "WRITE_YUPPASTE",
    "USE_MCP",
    "MANAGE_AGENTS",
    "MANAGE_AGENT_SCHEDULES",
    "MANAGE_AGENT_PROJECTS",
    "MANAGE_AGENT_SESSIONS",
    "CREATE_AGENT",
)
_PERM_IN = "(" + ", ".join(f"'{p}'" for p in VALID_PERMISSIONS) + ")"

# Tables that need renaming: (old_table, new_table, column_select, where)
RENAMED_TABLES = [
    (
        "soul_roles",
        "roles",
        ["role_id", "name", "description", "created_at", "modified_at", "deleted_at"],
        f"name IN {_ROLE_IN}",
    ),
    (
        "soul_role_permissions",
        "role_permissions",
        ["role_id", "permission"],
        f"role_id IN (SELECT role_id FROM soul_roles WHERE name IN {_ROLE_IN}) AND permission::text IN {_PERM_IN}",
    ),
    (
        "soul_user_roles",
        "user_roles",
        ["user_id", "role_id"],
        f"role_id IN (SELECT role_id FROM soul_roles WHERE name IN {_ROLE_IN})",
    ),
]

# Users table: only export columns that exist in yupp-agent's User model.
USERS_COLUMNS = ["user_id", "name", "email", "image", "status", "created_at", "modified_at", "deleted_at"]


def dump_table(
    cursor: psycopg2.extensions.cursor,
    table: str,
    output_dir: str,
    columns: str = "*",
    output_name: str | None = None,
    where: str | None = None,
) -> int:
    """Dump a table to CSV. Returns row count."""
    output_name = output_name or table
    path = os.path.join(output_dir, f"{output_name}.csv")

    where_clause = f" WHERE {where}" if where else ""
    query = f"COPY (SELECT {columns} FROM {table}{where_clause}) TO STDOUT WITH CSV HEADER"
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
    parser.add_argument(
        "--user-email-domain",
        default=os.environ.get("USER_EMAIL_DOMAIN", ""),
        help=(
            "Restrict the users dump to emails ending in this domain (e.g. 'example.com'). "
            "Empty = dump all users. Also reads USER_EMAIL_DOMAIN env var."
        ),
    )
    args = parser.parse_args()

    if not args.source:
        parser.error("--source is required (or set SOURCE_DB env var)")

    os.makedirs(args.output_dir, exist_ok=True)

    print("Connecting to source database ...")
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

    # 2. Renamed tables (with optional WHERE filter)
    for old_name, new_name, columns, where in RENAMED_TABLES:
        try:
            cols = ", ".join(columns) if columns else "*"
            count = dump_table(cur, old_name, args.output_dir, columns=cols, output_name=new_name, where=where)
            print(f"  {old_name} -> {new_name}: {count} rows")
            total += count
        except Exception as e:
            print(f"  {old_name} -> {new_name}: SKIPPED ({e})")
            conn.rollback()

    # 3. Users (subset of columns; optionally filtered by email domain)
    try:
        cols = ", ".join(USERS_COLUMNS)
        users_where: str | None
        if args.user_email_domain:
            # Parameterized via argparse; single-quote-escape any ' that sneaks in.
            safe_domain = args.user_email_domain.replace("'", "''")
            users_where = f"email LIKE '%@{safe_domain}'"
            label = f"users (@{args.user_email_domain} only)"
        else:
            users_where = None
            label = "users (all)"
        count = dump_table(cur, "users", args.output_dir, columns=cols, where=users_where)
        print(f"  {label}: {count} rows")
        total += count
    except Exception as e:
        print(f"  users: SKIPPED ({e})")
        conn.rollback()

    cur.close()
    conn.close()
    print(f"\nDone. {total} total rows dumped to {args.output_dir}/")


if __name__ == "__main__":
    main()

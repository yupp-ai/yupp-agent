#!/usr/bin/env python3
"""Dump the staging agentdb to your local Postgres.

Fetches credentials automatically from GCP Secret Manager, or prompts
interactively if gcloud is not available. Dumps are saved to ~/tmp/yadb-dumps/
with timestamps for easy browsing and re-restoration.

Usage:
    # Dump staging to local (saves to ~/tmp/yadb-dumps/ and restores):
    python -m ypl.db.tools.dump_staging_to_local

    # Dump only, don't restore:
    python -m ypl.db.tools.dump_staging_to_local --no-restore

    # List saved dumps and pick one to restore:
    python -m ypl.db.tools.dump_staging_to_local --list

    # Restore a specific dump by number (from --list):
    python -m ypl.db.tools.dump_staging_to_local --restore 3

    # Restore from an arbitrary file:
    python -m ypl.db.tools.dump_staging_to_local --restore-file ./my_dump.sql

    # Override the local destination:
    python -m ypl.db.tools.dump_staging_to_local --dest postgresql://user:pass@localhost:5432/mydb

    # Dump only specific tables:
    python -m ypl.db.tools.dump_staging_to_local --tables agents,agent_sessions

    # Schema only / data only:
    python -m ypl.db.tools.dump_staging_to_local --schema-only
    python -m ypl.db.tools.dump_staging_to_local --data-only
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime

GCP_PROJECT = "yupp-llms"
# Use the replica by default to avoid load on the primary.
SECRET_NAME_PRIMARY = "ym-postgres-connection-agentdb-staging"
SECRET_NAME_REPLICA = "ym-postgres-connection-agentdb-replica-staging"

LOCAL_DEFAULT_HOST = "127.0.0.1"
LOCAL_DEFAULT_PORT = "5432"
LOCAL_DEFAULT_USER = "postgres"
LOCAL_DEFAULT_PASSWORD = "local"
LOCAL_DEFAULT_DB = "yadb"

DUMPS_DIR = os.path.expanduser("~/tmp/yadb-dumps")

# Tables to skip during dump (not real data, or permission issues).
EXCLUDE_TABLES = [
    "alembic_version",
]

BANNER = """
╔══════════════════════════════════════════════════════════╗
║           yadb  ~  staging database dump tool            ║
╚══════════════════════════════════════════════════════════╝

  Pulls the staging agentdb into your local Postgres so you
  can develop and debug against real data.

  Source:  staging agentdb (read replica by default)
  Dest:    local Postgres (postgres:local@127.0.0.1:5432/yadb)
  Dumps:   ~/tmp/yadb-dumps/
""".rstrip()


def _print_step(step: int, total: int, message: str) -> None:
    """Print a numbered step with visual emphasis."""
    print(f"\n{'─' * 60}")
    print(f"  Step {step}/{total}: {message}")
    print(f"{'─' * 60}\n")


def _print_success(message: str) -> None:
    print(f"\n  >> {message}\n")


def _print_info(message: str) -> None:
    print(f"  {message}")


# ---------------------------------------------------------------------------
# Dump directory management
# ---------------------------------------------------------------------------


def _ensure_dumps_dir() -> str:
    os.makedirs(DUMPS_DIR, exist_ok=True)
    return DUMPS_DIR


def _list_dumps() -> list[dict]:
    """Return saved dumps sorted newest-first with metadata."""
    if not os.path.isdir(DUMPS_DIR):
        return []
    entries = []
    for name in sorted(os.listdir(DUMPS_DIR), reverse=True):
        if not name.endswith(".sql"):
            continue
        path = os.path.join(DUMPS_DIR, name)
        size_mb = os.path.getsize(path) / (1024 * 1024)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
        entries.append({"name": name, "path": path, "size_mb": size_mb, "mtime": mtime})
    return entries


def _print_dumps_table(dumps: list[dict]) -> None:
    if not dumps:
        print(f"  No dumps found in {DUMPS_DIR}/")
        return
    print(f"  Saved dumps in {DUMPS_DIR}/\n")
    print(f"    {'#':<4} {'Date':<22} {'Size':>8}  {'File'}")
    print(f"    {'─' * 4} {'─' * 22} {'─' * 8}  {'─' * 40}")
    for i, d in enumerate(dumps, 1):
        date_str = d["mtime"].strftime("%Y-%m-%d %H:%M UTC")
        size_str = f"{d['size_mb']:.1f} MB"
        print(f"    {i:<4} {date_str:<22} {size_str:>8}  {d['name']}")
    print()


def _generate_dump_filename(
    *, schema_only: bool = False, data_only: bool = False, tables: list[str] | None = None
) -> str:
    """Generate a timestamped dump filename."""
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = ""
    if schema_only:
        suffix = "_schema"
    elif data_only:
        suffix = "_data"
    if tables:
        suffix += "_" + "_".join(tables[:3])
        if len(tables) > 3:
            suffix += f"_and{len(tables) - 3}more"
    return f"yadb_{ts}{suffix}.sql"


# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------


def _check_pg_tools() -> None:
    """Ensure pg_dump and psql are on PATH."""
    missing = [tool for tool in ("pg_dump", "psql") if not shutil.which(tool)]
    if missing:
        print(f"  Missing required tools: {', '.join(missing)}")
        print()
        print("  Install PostgreSQL client tools first:")
        print("    macOS:  brew install libpq && brew link --force libpq")
        print("    Ubuntu: sudo apt-get install postgresql-client")
        sys.exit(1)
    _print_info("pg_dump and psql found on PATH.")


def _fetch_secret_from_gcp(secret_name: str) -> str | None:
    """Try to fetch a secret value from GCP Secret Manager. Returns None on failure."""
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{GCP_PROJECT}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(name=name)
        return response.payload.data.decode("utf-8")
    except Exception as e:
        _print_info(f"GCP Secret Manager unavailable: {e}")
        return None


def _parse_connection_json(raw: str) -> dict[str, str]:
    """Parse the JSON connection blob into {host, port, user, password, database}."""
    data = json.loads(raw)
    host_str = data.get("host", "localhost:5432")
    if ":" in host_str:
        host, port = host_str.rsplit(":", 1)
    else:
        host, port = host_str, "5432"
    return {
        "host": host,
        "port": port,
        "user": data.get("user", "postgres"),
        "password": data.get("password", ""),
        "database": data.get("database", "postgres"),
    }


def _get_source_credentials(use_primary: bool) -> dict[str, str]:
    """Get staging DB credentials from GCP or interactive prompt."""
    secret_name = SECRET_NAME_PRIMARY if use_primary else SECRET_NAME_REPLICA
    label = "primary" if use_primary else "read replica"

    _print_info(f"Trying to fetch staging agentdb credentials ({label})")
    _print_info(f"from GCP Secret Manager (project: {GCP_PROJECT})...")
    print()

    raw = _fetch_secret_from_gcp(secret_name)
    if raw:
        creds = _parse_connection_json(raw)
        _print_info(f"Got credentials for {creds['user']}@{creds['host']}:{creds['port']}/{creds['database']}")
        return creds

    print()
    print("  No GCP access. Let's enter the staging DB details manually.")
    print("  (Ask a teammate or check the GCP console for these values.)")
    print()
    host = input("    Host: ").strip()
    if not host:
        print("    Error: host is required.")
        sys.exit(1)
    port = input("    Port [5432]: ").strip() or "5432"
    database = input("    Database [agentdb]: ").strip() or "agentdb"
    user = input("    User: ").strip()
    if not user:
        print("    Error: user is required.")
        sys.exit(1)
    password = getpass.getpass("    Password: ")

    print()
    _print_info(f"Using manually entered credentials for {user}@{host}:{port}/{database}")
    return {"host": host, "port": port, "user": user, "password": password, "database": database}


def _parse_dest_url(url: str) -> dict[str, str]:
    """Parse a postgresql:// URL into components."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or LOCAL_DEFAULT_HOST,
        "port": str(parsed.port or LOCAL_DEFAULT_PORT),
        "user": parsed.username or LOCAL_DEFAULT_USER,
        "password": parsed.password or LOCAL_DEFAULT_PASSWORD,
        "database": (parsed.path or "").lstrip("/") or LOCAL_DEFAULT_DB,
    }


def _build_default_dest_url() -> str:
    """Build a default local dest URL from hardcoded defaults. Override with --dest or DEST_DB."""
    from urllib.parse import quote_plus

    return (
        f"postgresql://{quote_plus(LOCAL_DEFAULT_USER)}:{quote_plus(LOCAL_DEFAULT_PASSWORD)}"
        f"@{LOCAL_DEFAULT_HOST}:{LOCAL_DEFAULT_PORT}/{LOCAL_DEFAULT_DB}"
    )


def _dest_label(dest: dict[str, str]) -> str:
    return f"{dest['user']}@{dest['host']}:{dest['port']}/{dest['database']}"


def _build_pg_env(creds: dict[str, str], *, sslmode: str = "disable") -> dict[str, str]:
    """Build env dict with PGPASSWORD and PGSSLMODE set."""
    env = os.environ.copy()
    env["PGPASSWORD"] = creds["password"]
    env["PGSSLMODE"] = sslmode
    return env


def _ensure_local_db_exists(dest: dict[str, str]) -> None:
    """Create the local database if it doesn't exist yet."""
    env = _build_pg_env(dest)
    # Connect to the default 'postgres' database to check/create
    check_cmd = [
        "psql",
        "-h",
        dest["host"],
        "-p",
        dest["port"],
        "-U",
        dest["user"],
        "-d",
        "postgres",
        "--quiet",
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{dest['database']}'",
    ]
    result = subprocess.run(check_cmd, env=env, capture_output=True, text=True)
    if result.stdout.strip() == "1":
        return

    _print_info(f"Database '{dest['database']}' does not exist. Creating it...")
    create_cmd = [
        "psql",
        "-h",
        dest["host"],
        "-p",
        dest["port"],
        "-U",
        dest["user"],
        "-d",
        "postgres",
        "--quiet",
        "-c",
        f"CREATE DATABASE {dest['database']};",
    ]
    result = subprocess.run(create_cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        _print_info(f"Failed to create database: {result.stderr.strip()}")
        sys.exit(1)
    _print_info(f"Database '{dest['database']}' created.")


def _drop_and_recreate_schema(dest: dict[str, str]) -> None:
    """Drop all tables in the destination database by dropping and recreating the public schema."""
    env = _build_pg_env(dest)
    cmd = [
        "psql",
        "-h",
        dest["host"],
        "-p",
        dest["port"],
        "-U",
        dest["user"],
        "-d",
        dest["database"],
        "--quiet",
        "-c",
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
    ]
    _print_info("Running: DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        _print_info(f"Warning: schema drop returned exit {result.returncode}")
        if result.stderr.strip():
            _print_info(f"  {result.stderr.strip()}")
    else:
        _print_info("Local database cleared successfully.")


def _run_pg_dump(
    source: dict[str, str],
    dump_file: str,
    *,
    schema_only: bool = False,
    data_only: bool = False,
    tables: list[str] | None = None,
) -> None:
    """Run pg_dump against the source database."""
    cmd = [
        "pg_dump",
        "-h",
        source["host"],
        "-p",
        source["port"],
        "-U",
        source["user"],
        "-d",
        source["database"],
        "--no-owner",
        "--no-acl",
        "-f",
        dump_file,
    ]
    if schema_only:
        cmd.append("--schema-only")
    if data_only:
        cmd.append("--data-only")
    if tables:
        for t in tables:
            cmd.extend(["-t", t])
    # Always exclude non-data tables (e.g. alembic_version — app user lacks access).
    for t in EXCLUDE_TABLES:
        cmd.extend(["-T", t])

    env = _build_pg_env(source, sslmode="require")

    mode = "schema only" if schema_only else ("data only" if data_only else "full (schema + data)")
    _print_info(f"Dump mode:  {mode}")
    if tables:
        _print_info(f"Tables:     {', '.join(tables)}")
    _print_info(f"Output:     {dump_file}")
    print()
    _print_info("Running pg_dump (this may take a minute for large databases)...")

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"\n  pg_dump failed (exit {result.returncode}):\n")
        for line in result.stderr.strip().split("\n"):
            print(f"    {line}")
        sys.exit(1)

    size_mb = os.path.getsize(dump_file) / (1024 * 1024)
    _print_info(f"Dump complete! Size: {size_mb:.1f} MB")


def _confirm_and_clear_local(dest: dict[str, str]) -> None:
    """Ensure the DB exists, ask for confirmation, then drop+recreate the public schema."""
    _ensure_local_db_exists(dest)
    label = _dest_label(dest)
    print()
    print("  About to DROP ALL TABLES in local database:")
    print(f"    {label}")
    print()
    answer = input("  Continue? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("\n  Aborted. Your local database was not modified.")
        sys.exit(0)
    print()
    _drop_and_recreate_schema(dest)


def _run_psql_restore(dest: dict[str, str], dump_file: str) -> None:
    """Load a SQL dump file into the local database via psql."""
    cmd = [
        "psql",
        "-h",
        dest["host"],
        "-p",
        dest["port"],
        "-U",
        dest["user"],
        "-d",
        dest["database"],
        "-f",
        dump_file,
        "--quiet",
        "--set",
        "ON_ERROR_STOP=off",
    ]

    env = _build_pg_env(dest)

    _print_info(f"Loading dump into {_dest_label(dest)}...")
    _print_info("Running psql (this may take a moment)...")

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0 and result.stderr.strip():
        stderr_lines = [line for line in result.stderr.strip().split("\n") if "ERROR" in line]
        if stderr_lines:
            print()
            _print_info(f"Encountered {len(stderr_lines)} non-fatal errors during restore:")
            for line in stderr_lines[:10]:
                _print_info(f"  {line}")
            if len(stderr_lines) > 10:
                _print_info(f"  ... and {len(stderr_lines) - 10} more")

    print()
    _print_info("Restore complete!")


def _stamp_alembic(dest: dict[str, str]) -> None:
    """Run alembic stamp head so the local DB is marked as up-to-date."""
    env = _build_pg_env(dest)
    # ALEMBIC_DB_URL tells env.py which DB to connect to
    db_url = f"postgresql://{dest['user']}:{dest['password']}@{dest['host']}:{dest['port']}/{dest['database']}"
    env["ALEMBIC_DB_URL"] = db_url
    env["ENVIRONMENT"] = "local"

    cmd = ["poetry", "run", "alembic", "-c", "alembic.ini", "stamp", "head"]
    _print_info("Stamping alembic version (alembic stamp head)...")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        _print_info(f"Warning: alembic stamp failed (exit {result.returncode})")
        if result.stderr.strip():
            _print_info(f"  {result.stderr.strip()}")
    else:
        _print_info("Alembic version stamped to head.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump staging agentdb to local Postgres",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dest",
        default=os.environ.get("DEST_DB"),
        help="Local destination DB URL (default: built from POSTGRES_* env vars or .env, "
        "falling back to postgres:local@127.0.0.1:5432/yadb)",
    )
    parser.add_argument("--list", action="store_true", dest="list_dumps", help="List saved dumps and exit")
    parser.add_argument(
        "--restore",
        type=int,
        metavar="N",
        help="Restore dump #N from the saved list (use --list to see numbers)",
    )
    parser.add_argument("--restore-file", help="Restore from an arbitrary SQL file")
    parser.add_argument("--no-restore", action="store_true", help="Dump only, don't restore to local")
    parser.add_argument("--schema-only", action="store_true", help="Dump schema only (no data)")
    parser.add_argument("--data-only", action="store_true", help="Dump data only (assumes schema exists)")
    parser.add_argument("--use-primary", action="store_true", help="Use primary instead of replica (default: replica)")
    parser.add_argument("--tables", help="Comma-separated list of tables to dump (default: all)")

    args = parser.parse_args()

    # Resolve dest URL: --dest flag > DEST_DB env var > POSTGRES_* from .env > hardcoded defaults
    if not args.dest:
        args.dest = _build_default_dest_url()

    # --- List mode ---
    if args.list_dumps:
        print(BANNER)
        print()
        dumps = _list_dumps()
        _print_dumps_table(dumps)
        if dumps:
            print("  Tip: use --restore N to restore a dump (e.g. --restore 1)")
            print()
        return

    # --- Restore from saved dump by number ---
    if args.restore is not None:
        print(BANNER)
        dumps = _list_dumps()
        if not dumps:
            print(f"\n  No dumps found in {DUMPS_DIR}/")
            sys.exit(1)
        idx = args.restore - 1
        if idx < 0 or idx >= len(dumps):
            print(f"\n  Invalid dump number {args.restore}. Valid range: 1-{len(dumps)}")
            sys.exit(1)

        selected = dumps[idx]
        dest = _parse_dest_url(args.dest)

        _print_step(1, 3, "Checking tools")
        _check_pg_tools()

        _print_step(2, 3, "Clear local database")
        _print_info(f"Selected dump: {selected['name']} ({selected['size_mb']:.1f} MB)")
        _confirm_and_clear_local(dest)

        _print_step(3, 3, "Restore dump to local")
        _run_psql_restore(dest, selected["path"])
        _stamp_alembic(dest)

        _print_success(f"Done! Restored {selected['name']} into {dest['database']}.")
        return

    # --- Restore from arbitrary file ---
    if args.restore_file:
        print(BANNER)
        if not os.path.exists(args.restore_file):
            print(f"\n  Error: file not found: {args.restore_file}")
            sys.exit(1)

        dest = _parse_dest_url(args.dest)

        _print_step(1, 3, "Checking tools")
        _check_pg_tools()

        _print_step(2, 3, "Clear local database")
        _print_info(f"Restore file: {args.restore_file}")
        _confirm_and_clear_local(dest)

        _print_step(3, 3, "Restore dump to local")
        _run_psql_restore(dest, args.restore_file)
        _stamp_alembic(dest)

        _print_success(f"Done! Restored {args.restore_file} into {dest['database']}.")
        return

    # --- Dump (and optionally restore) ---
    print(BANNER)

    dest = _parse_dest_url(args.dest)
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    replica_label = "primary" if args.use_primary else "read replica"

    # Show the plan and confirm with the user
    print("  Here's what we'll do:\n")
    print(f"    Source:      staging agentdb ({replica_label})")
    print(f"    Destination: {_dest_label(dest)}")
    print(f"    Dumps dir:   {DUMPS_DIR}/")
    if tables:
        print(f"    Tables:      {', '.join(tables)}")
    if args.schema_only:
        print("    Mode:        schema only")
    elif args.data_only:
        print("    Mode:        data only")
    if args.no_restore:
        print("    Restore:     no (dump only)")
    else:
        print("    Restore:     yes (will clear local DB first)")
    print()
    answer = input("  Look good? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        print("\n  Aborted. Use --dest to change the destination.")
        sys.exit(0)

    total_steps = 3 if args.no_restore else 5
    step = 0

    step += 1
    _print_step(step, total_steps, "Checking tools")
    _check_pg_tools()

    step += 1
    _print_step(step, total_steps, "Connect to staging")
    source = _get_source_credentials(use_primary=args.use_primary)

    _ensure_dumps_dir()
    filename = _generate_dump_filename(schema_only=args.schema_only, data_only=args.data_only, tables=tables)
    dump_file = os.path.join(DUMPS_DIR, filename)

    step += 1
    _print_step(step, total_steps, "Dump staging database")
    _run_pg_dump(source, dump_file, schema_only=args.schema_only, data_only=args.data_only, tables=tables)

    if not args.no_restore:
        step += 1
        _print_step(step, total_steps, "Clear local database")
        _confirm_and_clear_local(dest)

        step += 1
        _print_step(step, total_steps, "Restore dump to local")
        _run_psql_restore(dest, dump_file)
        _stamp_alembic(dest)

    # --- Summary ---
    print()
    print(f"{'═' * 60}")
    if not args.no_restore:
        print(f"  All done! Staging agentdb is now in your local {dest['database']}.")
    else:
        print("  Dump saved! Use --restore or --restore-file to load it later.")
    print(f"{'═' * 60}")

    print()
    dumps = _list_dumps()
    _print_dumps_table(dumps)


if __name__ == "__main__":
    main()

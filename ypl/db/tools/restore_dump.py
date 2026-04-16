#!/usr/bin/env python3
"""Restore a yadb dump file into a destination Postgres.

Env-driven only. Intended for loading a production yadb dump into a
monolith VM's local Postgres, possibly under a different GCP account.

Usage:
    export PG_DEST_URL='postgresql://postgres:postgres@127.0.0.1:5432/yadb'
    python -m ypl.db.tools.restore_dump --file ~/tmp/yadb-dumps/yadb_prod_20260416_120000.dump

    # Skip the drop-schema confirmation prompt:
    python -m ypl.db.tools.restore_dump --file ... --no-confirm

    # Parallel restore (default 4 jobs):
    python -m ypl.db.tools.restore_dump --file ... --jobs 8

    # After restore, stamp alembic to the current head so `alembic upgrade head`
    # doesn't try to re-apply existing migrations:
    python -m ypl.db.tools.restore_dump --file ... --stamp-alembic

The script will:
  1. Verify pg_restore and psql are on PATH.
  2. Ensure the destination database exists (creates it if not).
  3. DROP SCHEMA public CASCADE; CREATE SCHEMA public; (with confirmation).
  4. pg_restore --no-owner --no-acl --clean-if-exists --jobs=N.
  5. Optionally: alembic stamp head.
"""

from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qsl, unquote, urlparse


def _check_pg_tools() -> None:
    missing = [tool for tool in ("pg_restore", "psql") if not shutil.which(tool)]
    if missing:
        print(f"Error: missing required tools: {', '.join(missing)}", file=sys.stderr)
        print("  macOS:  brew install libpq && brew link --force libpq", file=sys.stderr)
        print("  Ubuntu: sudo apt-get install postgresql-client", file=sys.stderr)
        sys.exit(1)


def _parse_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("postgresql", "postgres"):
        print(f"Error: expected postgresql:// URL, got scheme={parsed.scheme!r}", file=sys.stderr)
        sys.exit(1)
    if not parsed.hostname or not parsed.path:
        print("Error: URL must include host and /database", file=sys.stderr)
        sys.exit(1)
    # urlparse does not URL-decode userinfo/path; do it explicitly so passwords
    # with /, @, :, +, =, space etc. can be percent-encoded in the URL.
    return {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username) if parsed.username else "postgres",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": unquote(parsed.path.lstrip("/")),
        "sslmode": dict(parse_qsl(parsed.query)).get("sslmode", "prefer"),
    }


def _build_env(creds: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = creds["password"]
    env["PGSSLMODE"] = creds["sslmode"]
    return env


def _run_psql(creds: dict[str, str], *, database: str | None = None, sql: str) -> subprocess.CompletedProcess:
    db = database or creds["database"]
    cmd = [
        "psql",
        "-h",
        creds["host"],
        "-p",
        creds["port"],
        "-U",
        creds["user"],
        "-d",
        db,
        "--quiet",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    ]
    return subprocess.run(cmd, env=_build_env(creds), capture_output=True, text=True)


def _ensure_database_exists(creds: dict[str, str]) -> None:
    """Connect to the 'postgres' DB and create the target database if missing."""
    check = _run_psql(
        creds,
        database="postgres",
        sql=f"SELECT 1 FROM pg_database WHERE datname = '{creds['database']}'",
    )
    if "1" in (check.stdout or ""):
        return
    print(f"Database {creds['database']!r} does not exist. Creating it...")
    create = _run_psql(creds, database="postgres", sql=f'CREATE DATABASE "{creds["database"]}"')
    if create.returncode != 0:
        print(f"Failed to create database: {create.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def _drop_schema(creds: dict[str, str]) -> None:
    print("Dropping and recreating public schema...")
    result = _run_psql(creds, sql="DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    if result.returncode != 0:
        print(f"Failed to drop schema: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def _stamp_alembic(creds: dict[str, str]) -> None:
    url = f"postgresql://{creds['user']}:{creds['password']}@{creds['host']}:{creds['port']}/{creds['database']}"
    env = os.environ.copy()
    env["ALEMBIC_DB_URL"] = url
    env.setdefault("ENVIRONMENT", "local")
    cmd = ["poetry", "run", "alembic", "-c", "alembic.ini", "stamp", "head"]
    print("Running alembic stamp head...")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"alembic stamp failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)
    print("Alembic stamped to head.")


def _human_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024  # type: ignore[assignment]
    return f"{bytes_:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore a yadb Postgres dump (custom format) into a destination DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--file", required=True, help="Path to the .dump file produced by dump_prod_yadb.py.")
    parser.add_argument(
        "--dest",
        default=os.environ.get("PG_DEST_URL"),
        help="Destination postgresql:// URL (or set PG_DEST_URL env var).",
    )
    parser.add_argument("--jobs", type=int, default=4, help="pg_restore parallel jobs (default 4).")
    parser.add_argument("--no-confirm", action="store_true", help="Skip drop-schema confirmation prompt.")
    parser.add_argument("--stamp-alembic", action="store_true", help="Run alembic stamp head after restore.")
    args = parser.parse_args()

    if not args.dest:
        parser.error("--dest is required (or set PG_DEST_URL env var)")
    if not os.path.isfile(args.file):
        parser.error(f"file not found: {args.file}")

    _check_pg_tools()
    creds = _parse_url(args.dest)
    size = os.path.getsize(args.file)

    dest_label = f"{creds['user']}@{creds['host']}:{creds['port']}/{creds['database']}"
    print("pg_restore plan:")
    print(f"  Dump file: {args.file} ({_human_size(size)})")
    print(f"  Dest:      {dest_label} (sslmode={creds['sslmode']})")
    print(f"  Jobs:      {args.jobs}")
    print("  Will DROP + recreate the public schema before loading.")
    print()

    if not args.no_confirm:
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted. Destination was not modified.")
            sys.exit(0)

    _ensure_database_exists(creds)
    _drop_schema(creds)

    cmd = [
        "pg_restore",
        "-h",
        creds["host"],
        "-p",
        creds["port"],
        "-U",
        creds["user"],
        "-d",
        creds["database"],
        "--no-owner",
        "--no-acl",
        "--jobs",
        str(args.jobs),
        "--verbose",
        args.file,
    ]
    print("Running pg_restore...")
    started = time.time()
    result = subprocess.run(cmd, env=_build_env(creds))
    elapsed = time.time() - started

    if result.returncode != 0:
        print(f"\npg_restore exited with {result.returncode} after {elapsed:.1f}s", file=sys.stderr)
        print(
            "Note: a non-zero exit can still produce a usable DB — inspect pg_restore's "
            "stderr above for the actual failures and decide whether to retry.",
            file=sys.stderr,
        )
        sys.exit(result.returncode)

    print(f"\nRestore complete in {elapsed:.1f}s.")

    if args.stamp_alembic:
        _stamp_alembic(creds)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Dump a yadb Postgres database to a local compressed file.

Env-driven only — no GCP Secret Manager lookups. Works across GCP accounts.
Intended for pulling production yadb for a monolith / cross-account migration.

Usage:
    export PG_SOURCE_URL='postgresql://user:pass@host:5432/yadb?sslmode=require'
    python -m ypl.db.tools.dump_prod_yadb

    # Override output directory or filename:
    python -m ypl.db.tools.dump_prod_yadb --output-dir ./dumps
    python -m ypl.db.tools.dump_prod_yadb --filename yadb_snapshot.dump

    # Dump a subset:
    python -m ypl.db.tools.dump_prod_yadb --tables agents,agent_sessions
    python -m ypl.db.tools.dump_prod_yadb --schema-only
    python -m ypl.db.tools.dump_prod_yadb --data-only

Prefer connecting direct to Cloud SQL's private IP (port 5432) rather than
through PgBouncer — pg_dump needs a stable single session, which breaks if
the bouncer is in transaction pool mode.
"""

from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from urllib.parse import parse_qsl, unquote, urlparse

DEFAULT_OUTPUT_DIR = os.path.expanduser("~/tmp/yadb-dumps")


def _check_pg_dump() -> None:
    if not shutil.which("pg_dump"):
        print("Error: pg_dump not on PATH.", file=sys.stderr)
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


def _generate_filename(schema_only: bool, data_only: bool, tables: list[str] | None) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = ""
    if schema_only:
        suffix = "_schema"
    elif data_only:
        suffix = "_data"
    if tables:
        joined = "_".join(tables[:3])
        suffix += f"_{joined}"
        if len(tables) > 3:
            suffix += f"_and{len(tables) - 3}more"
    return f"yadb_prod_{ts}{suffix}.dump"


def _human_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024  # type: ignore[assignment]
    return f"{bytes_:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump a yadb Postgres database to a compressed custom-format file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("PG_SOURCE_URL"),
        help="Source postgresql:// URL (or set PG_SOURCE_URL env var).",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--filename", help="Override auto-generated filename.")
    parser.add_argument("--tables", help="Comma-separated tables to dump (default: all).")
    parser.add_argument("--schema-only", action="store_true", help="Dump schema only, no data.")
    parser.add_argument("--data-only", action="store_true", help="Dump data only, assume schema exists.")
    args = parser.parse_args()

    if args.schema_only and args.data_only:
        parser.error("--schema-only and --data-only are mutually exclusive")
    if not args.source:
        parser.error("--source is required (or set PG_SOURCE_URL env var)")

    _check_pg_dump()
    creds = _parse_url(args.source)
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None

    os.makedirs(args.output_dir, exist_ok=True)
    filename = args.filename or _generate_filename(args.schema_only, args.data_only, tables)
    out_path = os.path.join(args.output_dir, filename)

    if os.path.exists(out_path):
        print(f"Error: refusing to overwrite existing file {out_path}", file=sys.stderr)
        sys.exit(1)

    src_label = f"{creds['user']}@{creds['host']}:{creds['port']}/{creds['database']}"
    mode = "schema only" if args.schema_only else "data only" if args.data_only else "full (schema + data)"
    print("pg_dump plan:")
    print(f"  Source:   {src_label} (sslmode={creds['sslmode']})")
    print(f"  Mode:     {mode}")
    if tables:
        print(f"  Tables:   {', '.join(tables)}")
    print(f"  Output:   {out_path}")
    print("  Format:   custom (compressed)")
    print()

    cmd = [
        "pg_dump",
        "-h",
        creds["host"],
        "-p",
        creds["port"],
        "-U",
        creds["user"],
        "-d",
        creds["database"],
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--verbose",
        "-f",
        out_path,
    ]
    if args.schema_only:
        cmd.append("--schema-only")
    if args.data_only:
        cmd.append("--data-only")
    for t in tables or []:
        cmd.extend(["-t", t])

    env = os.environ.copy()
    env["PGPASSWORD"] = creds["password"]
    env["PGSSLMODE"] = creds["sslmode"]

    print("Running pg_dump... (this may take a while for large databases)")
    started = time.time()
    # Stream pg_dump's --verbose output to stderr in real time so operators see progress.
    result = subprocess.run(cmd, env=env)
    elapsed = time.time() - started

    if result.returncode != 0:
        print(f"\npg_dump failed (exit {result.returncode}) after {elapsed:.1f}s", file=sys.stderr)
        if os.path.exists(out_path):
            os.remove(out_path)
            print(f"Removed partial file {out_path}", file=sys.stderr)
        sys.exit(result.returncode)

    size = os.path.getsize(out_path)
    print()
    print(f"Done in {elapsed:.1f}s — {_human_size(size)} written to {out_path}")


if __name__ == "__main__":
    main()

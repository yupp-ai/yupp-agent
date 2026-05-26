"""``ahs-memory`` argparse front-end.

Three subcommands:

  ``walk``  — dry-run: walk a workspace, print the slug table.
  ``push``  — for each ``*.md`` file, POST to AHS as a MEMORY artifact.
  ``diff``  — 3-way diff of the workspace against the user's MEMORY rows.

Output policy: tables and one-line progress notes go to stderr so the
process exit code is the only signal a calling script needs; the final
summary line goes to stdout so a pipe like
``ahs-memory push ... | tee migrate.log`` captures the durable record.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from ahs_memory import __version__
from ahs_memory.client import AHSAPIError, AHSMemoryClient
from ahs_memory.config import ConfigError, require_api_key, require_user_id, resolve_config
from ahs_memory.pusher import diff_workspace, push_workspace
from ahs_memory.render import human_bytes, render_table
from ahs_memory.walker import DEFAULT_MAX_BYTES, walk_workspace


def _print_err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# walk
# ---------------------------------------------------------------------------


def cmd_walk(args: argparse.Namespace) -> int:
    """Dry-run table of (slug, size, skip reason). No network."""
    candidates = walk_workspace(
        Path(args.path),
        include=args.include or (),
        exclude=args.exclude or (),
        prefix=args.prefix,
        max_bytes=args.max_bytes,
    )
    if not candidates:
        _print_err(f"(no .md files under {args.path})")
        return 0
    rows = [
        (
            c.rel_path,
            c.slug or "(empty)",
            human_bytes(c.size_bytes),
            c.skip_reason or "ok",
        )
        for c in candidates
    ]
    _print_err(render_table(["path", "slug", "size", "status"], rows))
    skipped = sum(1 for c in candidates if c.skip_reason)
    _print_err()
    print(f"walked {len(candidates)} files ({skipped} would skip).")
    return 0


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def cmd_push(args: argparse.Namespace) -> int:
    """Upload each pushable ``*.md`` as a MEMORY artifact."""
    cfg = resolve_config(override_user_id=args.user_id)
    api_key = require_api_key(cfg)
    user_id = require_user_id(cfg)

    candidates = walk_workspace(
        Path(args.path),
        include=args.include or (),
        exclude=args.exclude or (),
        prefix=args.prefix,
        max_bytes=args.max_bytes,
    )
    _print_err(f"pushing {len(candidates)} candidate(s) to {cfg.api_url} as user {user_id}")

    with AHSMemoryClient(cfg.api_url, api_key, user_id=user_id) as client:
        outcomes = push_workspace(
            client,
            candidates,
            user_id=user_id,
            skip_unchanged=args.skip_unchanged,
        )

    rows = [(o.rel_path, o.slug or "(empty)", o.status, human_bytes(o.size_bytes), o.detail) for o in outcomes]
    _print_err(render_table(["path", "slug", "status", "size", "detail"], rows))

    # Plain dict — keeps the mypy-friendly indexing-with-fallback pattern below
    # without dragging in ``Counter[Literal[...]]``'s narrowed get() overloads.
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    summary_parts = [
        f"{counts.get(s, 0)} {s}"
        for s in (
            "created",
            "new-version",
            "skipped-unchanged",
            "skipped-oversize",
            "skipped-unsafe",
            "error",
        )
    ]
    _print_err()
    print(f"pushed {len(outcomes)} file(s) — " + ", ".join(summary_parts) + ".")
    # Exit non-zero if anything errored so CI loops can detect partial failures.
    return 1 if counts.get("error", 0) else 0


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def cmd_diff(args: argparse.Namespace) -> int:
    """3-way diff of the workspace vs. user-scope MEMORY rows."""
    cfg = resolve_config(override_user_id=args.user_id)
    api_key = require_api_key(cfg)
    user_id = require_user_id(cfg)

    candidates = walk_workspace(
        Path(args.path),
        include=args.include or (),
        exclude=args.exclude or (),
        prefix=args.prefix,
        max_bytes=args.max_bytes,
    )
    with AHSMemoryClient(cfg.api_url, api_key, user_id=user_id) as client:
        rows = diff_workspace(client, candidates, user_id=user_id)

    table_rows = [(r.status, r.slug, r.rel_path, human_bytes(r.size_bytes) if r.size_bytes else "") for r in rows]
    _print_err(render_table(["status", "slug", "path", "size"], table_rows))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary_parts = [
        f"{counts.get('local-only', 0)} local-only",
        f"{counts.get('changed', 0)} changed",
        f"{counts.get('remote-only', 0)} remote-only",
        f"{counts.get('unchanged', 0)} unchanged",
    ]
    _print_err()
    print(f"diff: {', '.join(summary_parts)}.")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _add_walk_args(p: argparse.ArgumentParser) -> None:
    """Args shared by walk/push/diff (every subcommand walks first)."""
    p.add_argument("path", help="Workspace root to walk.")
    p.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="Only consider files matching this glob (relative POSIX path). Repeatable.",
    )
    p.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="Exclude files matching this glob (relative POSIX path). Repeatable.",
    )
    p.add_argument(
        "--prefix",
        metavar="STR",
        help="Prefix prepended to every slug (e.g. 'notes/').",
    )
    p.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        metavar="N",
        help=f"Files larger than this are skipped (default {DEFAULT_MAX_BYTES} = 10 MiB).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ahs-memory",
        description="Bulk-import a Markdown workspace into AHS as MEMORY artifacts.",
    )
    parser.add_argument("--version", action="version", version=f"ahs-memory {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    p_walk = subs.add_parser("walk", help="Dry-run table of (slug, size, status). No network.")
    _add_walk_args(p_walk)
    p_walk.set_defaults(func=cmd_walk)

    p_push = subs.add_parser("push", help="Upload each .md as a MEMORY artifact.")
    _add_walk_args(p_push)
    p_push.add_argument(
        "--user-id",
        help="user_id to write to (overrides AHS_USER_ID / config.toml).",
    )
    p_push.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="Compare hashes against the latest server version; skip if identical.",
    )
    p_push.set_defaults(func=cmd_push)

    p_diff = subs.add_parser("diff", help="3-way diff of workspace vs. user-scope MEMORY rows.")
    _add_walk_args(p_diff)
    p_diff.add_argument(
        "--user-id",
        help="user_id whose memories to compare against.",
    )
    p_diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except AHSAPIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

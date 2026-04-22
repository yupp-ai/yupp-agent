"""``ahs-artifact`` — command-line client for the AHS artifact REST API.

Talks to ``AHS_BASE_URL`` (default ``https://ahs.agcouch.com``) with the
``AGENT_HARNESS_SERVICE_API_KEY`` shared secret.

Subcommands::

    ahs-artifact add [FILE] [--slug NAME] [--title T] [--type TYPE] [--new-slug]
    ahs-artifact get (UUID | SLUG) [-v N] [-q | -m]
    ahs-artifact rm  (UUID | SLUG)
    ahs-artifact ls  [--slug NAME] [--limit N] [--offset N]
    ahs-artifact versions SLUG
    ahs-artifact url (UUID | SLUG)
    ahs-artifact search QUERY [--limit N] [--offset N]

Content type is sniffed from the file extension (``.md`` → markdown,
``.html`` → html, else plain). Override with ``--type``.

If the file/stdin is given without ``--title``, the CLI prompts
interactively — use ``--title`` in scripts to avoid the prompt.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

import httpx

DEFAULT_BASE_URL = "https://ahs.agcouch.com"
_EXT_CONTENT_TYPE: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
}
_TYPE_ALIASES: dict[str, str] = {
    "md": "text/markdown",
    "markdown": "text/markdown",
    "html": "text/html",
    "plain": "text/plain",
    "txt": "text/plain",
    "text": "text/plain",
}
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ---------------------------------------------------------------------------
# Config + HTTP client
# ---------------------------------------------------------------------------


class CLIError(Exception):
    """User-facing CLI error. Message is printed; process exits non-zero."""


def _config() -> tuple[str, str]:
    base_url = os.environ.get("AHS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("AGENT_HARNESS_SERVICE_API_KEY", "")
    if not api_key:
        raise CLIError("AGENT_HARNESS_SERVICE_API_KEY is not set in the environment.")
    return base_url, api_key


def _client() -> httpx.Client:
    base_url, api_key = _config()
    return httpx.Client(
        base_url=base_url,
        headers={"X-API-Key": api_key},
        timeout=30.0,
    )


def _handle(resp: httpx.Response, *, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
    if resp.status_code in ok:
        if resp.status_code == 204 or not resp.content:
            return None
        ct = resp.headers.get("content-type", "")
        if ct.startswith("application/json"):
            return resp.json()
        return resp.content
    # Error path — try to pull detail out of JSON.
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    raise CLIError(f"HTTP {resp.status_code}: {detail}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _resolve_content_type(file_path: Path | None, override: str | None) -> str:
    if override is not None:
        resolved = _TYPE_ALIASES.get(override.lower(), override.lower())
        if resolved not in {"text/plain", "text/markdown", "text/html"}:
            raise CLIError(f"--type must be one of: md, markdown, html, plain (got {override!r}).")
        return resolved
    if file_path is not None:
        return _EXT_CONTENT_TYPE.get(file_path.suffix.lower(), "text/plain")
    # stdin default: markdown is the most common "paste" format.
    return "text/markdown"


def _prompt_title(default: str) -> str:
    if not sys.stdin.isatty():
        return default
    try:
        raw = input(f"Title [{default}]: ").strip()
    except EOFError:
        raw = ""
    return raw or default


def _format_artifact_row(a: dict[str, Any]) -> str:
    slug = a.get("named_slug") or "-"
    version = a.get("version")
    ver = f"v{version}" if version is not None else "-"
    title = a.get("title") or ""
    created = (a.get("created_at") or "").replace("T", " ")[:19]
    archived = (a.get("metadata") or {}).get("is_archived")
    flag = " [archived]" if archived else ""
    return f"{created}  {a['artifact_id'][:8]}  {slug:<24}  {ver:<4}  {title}{flag}"


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no results)", file=sys.stderr)
        return
    print(f"{'created (UTC)':<19}  {'id':<8}  {'slug':<24}  {'ver':<4}  title", file=sys.stderr)
    print(f"{'-' * 19}  {'-' * 8}  {'-' * 24}  {'-' * 4}  -----", file=sys.stderr)
    for r in rows:
        print(_format_artifact_row(r))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    file_arg: str | None = args.file
    if file_arg and file_arg != "-":
        path = Path(file_arg)
        if not path.exists():
            raise CLIError(f"File not found: {file_arg}")
        content_bytes = path.read_bytes()
        default_title = path.stem
    else:
        content_bytes = sys.stdin.buffer.read()
        default_title = "stdin"
    if not content_bytes:
        raise CLIError("Empty content — nothing to upload.")

    content_type = _resolve_content_type(
        Path(file_arg) if file_arg and file_arg != "-" else None,
        args.type,
    )

    title = args.title or _prompt_title(default_title)
    if not title:
        raise CLIError("Title is required.")

    body: dict[str, Any] = {
        "content": content_bytes.decode("utf-8", errors="replace"),
        "content_type": content_type,
        "title": title,
    }
    if args.slug:
        body["named_slug"] = args.slug
        if args.new_slug:
            body["create_new_slug"] = True
        else:
            # Auto-detect: try append first, fall back to create-new if slug is unknown.
            body["create_new_slug"] = False

    with _client() as http:
        resp = http.post("/ahs/artifacts", json=body)
        if args.slug and not args.new_slug and resp.status_code == 400:
            # Append failed because slug doesn't exist yet — retry as new.
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text
            if "does not exist yet" in detail:
                body["create_new_slug"] = True
                resp = http.post("/ahs/artifacts", json=body)
        data = _handle(resp, ok=(201,))

    print(f"artifact_id: {data['artifact_id']}", file=sys.stderr)
    if data.get("named_slug"):
        print(f"slug:        {data['named_slug']} (v{data['version']})", file=sys.stderr)
    base_url, _ = _config()
    url = data["url"]
    print(f"url:         {url if url.startswith(('http://', 'https://')) else base_url + url}")
    return 0


def _get_by_id_or_slug(http: httpx.Client, ident: str, version: int | None) -> dict[str, Any]:
    if _is_uuid(ident):
        return cast(dict[str, Any], _handle(http.get(f"/ahs/artifacts/{ident}/meta")))
    params: dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    return cast(dict[str, Any], _handle(http.get(f"/ahs/artifacts/by-slug/{ident}", params=params)))


def cmd_get(args: argparse.Namespace) -> int:
    with _client() as http:
        meta = _get_by_id_or_slug(http, args.ident, args.version)
        if not args.content_only:
            # Metadata on stderr so stdout stays pipe-clean.
            slug = meta.get("named_slug") or "-"
            version = meta.get("version")
            ver = f"v{version}" if version is not None else "-"
            print(f"artifact_id: {meta['artifact_id']}", file=sys.stderr)
            print(f"title:       {meta.get('title') or ''}", file=sys.stderr)
            print(f"slug/ver:    {slug} ({ver})", file=sys.stderr)
            print(f"created:     {meta.get('created_at')}", file=sys.stderr)
            print(f"type:        {meta.get('content_type')}", file=sys.stderr)
            if meta.get("description"):
                print(f"description: {meta['description']}", file=sys.stderr)
            attachments = (meta.get("metadata") or {}).get("attachments") or []
            if attachments:
                print(f"attachments: {len(attachments)}", file=sys.stderr)
                for a in attachments:
                    print(f"  - {a.get('filename')} ({a.get('content_type')})", file=sys.stderr)
            print("---", file=sys.stderr)
        if args.meta_only:
            return 0
        resp = http.get(f"/ahs/artifacts/{meta['artifact_id']}")
        content = _handle(resp)
    if isinstance(content, bytes):
        sys.stdout.buffer.write(content)
        if not content.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
    else:
        # JSON content (unusual for artifacts — fall back to a pretty print).
        print(json.dumps(content, indent=2))
    return 0


def _confirm_yes_no(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise CLIError("Refusing to destructively act without a TTY — re-run interactively.")
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _confirm_typed(expected: str, prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise CLIError("Refusing to destructively act without a TTY — re-run interactively.")
    try:
        answer = input(prompt).strip()
    except EOFError:
        return False
    return answer == expected


def cmd_rm(args: argparse.Namespace) -> int:
    ident = args.ident
    with _client() as http:
        if _is_uuid(ident):
            # Fetch meta for a human-friendly confirm prompt.
            meta = _handle(http.get(f"/ahs/artifacts/{ident}/meta"))
            title = meta.get("title") or "(untitled)"
            if not _confirm_yes_no(f"Archive artifact {ident[:8]}… {title!r}?"):
                print("aborted", file=sys.stderr)
                return 1
            _handle(http.delete(f"/ahs/artifacts/{ident}"), ok=(204,))
            print(f"archived {ident}", file=sys.stderr)
            return 0
        # Slug path — archives every version.
        versions = _handle(http.get(f"/ahs/artifacts/by-slug/{ident}/versions"))
        version_list = versions.get("versions", [])
        if not version_list:
            raise CLIError(f"No artifact found for slug {ident!r}.")
        print(
            f"This will archive {len(version_list)} version(s) under slug {ident!r}:",
            file=sys.stderr,
        )
        for v in version_list:
            print(f"  - v{v.get('version')}  {v.get('title')}", file=sys.stderr)
        if not _confirm_typed("delete", "Type 'delete' to confirm: "):
            print("aborted", file=sys.stderr)
            return 1
        resp = _handle(http.delete(f"/ahs/artifacts/by-slug/{ident}"))
        print(f"archived {resp['archived_count']} version(s) of {ident!r}", file=sys.stderr)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.include_archived:
        params["include_archived"] = "true"
    with _client() as http:
        data = _handle(http.get("/ahs/artifacts", params=params))
    _print_table(data.get("artifacts", []))
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    with _client() as http:
        data = _handle(http.get(f"/ahs/artifacts/by-slug/{args.slug}/versions"))
    _print_table(data.get("versions", []))
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    base_url, _ = _config()
    ident = args.ident
    if _is_uuid(ident):
        print(f"{base_url}/ahs/artifacts/{ident}")
        return 0
    with _client() as http:
        meta = _handle(http.get(f"/ahs/artifacts/by-slug/{ident}"))
    url = meta["url"]
    print(url if url.startswith(("http://", "https://")) else f"{base_url}{url}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"q": args.query, "limit": args.limit, "offset": args.offset}
    if args.include_archived:
        params["include_archived"] = "true"
    with _client() as http:
        data = _handle(http.get("/ahs/artifacts/search", params=params))
    _print_table(data.get("artifacts", []))
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ahs-artifact",
        description="CLI for the AHS textual-artifact API.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = subs.add_parser("add", help="Create a new artifact from a file or stdin.")
    p_add.add_argument("file", nargs="?", help="File to upload (omit or '-' for stdin).")
    p_add.add_argument("--slug", help="Stable URL-safe slug. Appends a version if slug exists.")
    p_add.add_argument(
        "--new-slug",
        action="store_true",
        help="Fail if --slug is already in use (force a fresh slug).",
    )
    p_add.add_argument("--title", help="Artifact title (prompts interactively if omitted).")
    p_add.add_argument(
        "--type",
        dest="type",
        help="Content type override: md | markdown | html | plain (default: inferred).",
    )
    p_add.set_defaults(func=cmd_add)

    # get
    p_get = subs.add_parser("get", help="Fetch an artifact by UUID or slug.")
    p_get.add_argument("ident", help="UUID or named slug.")
    p_get.add_argument("-v", "--version", type=int, help="Specific version (slug only).")
    p_get.add_argument(
        "-q",
        "--content-only",
        action="store_true",
        help="Suppress metadata header; print only content (pipe-friendly).",
    )
    p_get.add_argument(
        "-m",
        "--meta-only",
        action="store_true",
        help="Print only metadata; skip content.",
    )
    p_get.set_defaults(func=cmd_get)

    # rm
    p_rm = subs.add_parser("rm", help="Archive an artifact (UUID) or slug (all versions).")
    p_rm.add_argument("ident", help="UUID (single) or slug (all versions).")
    p_rm.set_defaults(func=cmd_rm)

    # ls
    p_ls = subs.add_parser("ls", help="List artifacts (reverse-chronological).")
    p_ls.add_argument("--limit", type=int, default=50)
    p_ls.add_argument("--offset", type=int, default=0)
    p_ls.add_argument("--include-archived", action="store_true")
    p_ls.set_defaults(func=cmd_ls)

    # versions
    p_versions = subs.add_parser("versions", help="List all versions under a slug.")
    p_versions.add_argument("slug")
    p_versions.set_defaults(func=cmd_versions)

    # url
    p_url = subs.add_parser("url", help="Print the public URL for an artifact.")
    p_url.add_argument("ident", help="UUID or slug (latest version).")
    p_url.set_defaults(func=cmd_url)

    # search
    p_search = subs.add_parser(
        "search",
        help="Substring search over title, description, slug, and attachment filenames.",
    )
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--offset", type=int, default=0)
    p_search.add_argument("--include-archived", action="store_true")
    p_search.set_defaults(func=cmd_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

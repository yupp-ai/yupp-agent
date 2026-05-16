"""Workspace tool implementations for raw executors.

Pure functions (no MCP dependency) that provide filesystem and shell access
within session workspaces. Registered as MCP tools in local_mcp_server.py.

BCH integration:
  When a ``CommandHandlerManager`` is registered for a session via
  ``set_command_handler_manager(session_id, manager)``, async wrappers
  forward calls to the warm bwrapped proxy instead of spawning a new bwrap
  process per call.  The sync ``run_command()`` is kept for backward
  compatibility; the MCP tool layer uses the async path when a manager is set.
"""

from __future__ import annotations
import fnmatch
import ipaddress
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ypl.agent_harness_service.common.constants import (
    AHS_REPOS_DIR,
    AHS_SESSIONS_DIR,
    SESSION_INFRA_DIRS,
    _validate_session_id,
)
from ypl.agent_harness_service.common.constants import (
    get_session_dir as get_session_dir,  # re-export for backward compat
)
from ypl.agent_harness_service.common.types import ToolDispatcher
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# BCH manager registry
# ---------------------------------------------------------------------------
# Maps session_id → CommandHandlerManager.  Populated by service.py when a
# session starts (A5), cleared on session teardown.  When a manager is
# registered for a session, the async MCP tool wrappers (mcp_bash etc. in
# local_mcp_server.py) forward calls to the warm proxy instead of spawning
# a new bwrap process per call.
#
# Pattern mirrors register_orchestration_callbacks() in local_mcp_server.py:
# module-level dict + registration helpers wired by the service layer.
_session_managers: dict[str, ToolDispatcher] = {}

# Output limits
_MAX_OUTPUT_BYTES = 100_000  # 100 KB for command output
_MAX_READ_LINES = 2000
_MAX_LINE_LENGTH = 2000  # Truncate individual lines longer than this
_DEFAULT_COMMAND_TIMEOUT = 120  # seconds
_DEFAULT_REPO = "yupp-agent"

# Image extensions → returned as ImageContent (models see natively via vision).
# Only formats supported by Claude's vision API: PNG, JPEG, GIF, WebP.
_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    }
)

# Document/file extensions → returned as EmbeddedResource (client-dependent processing)
_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    }
)

# All extensions that get returned as binary content blocks
_MULTIMODAL_EXTENSIONS = _IMAGE_EXTENSIONS | _DOCUMENT_EXTENSIONS

# Binary file extensions that cannot be meaningfully processed by AI models
_BINARY_EXTENSIONS = frozenset(
    {
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".class",
        ".jar",
        ".war",
        ".ear",
        ".pyc",
        ".pyo",
        ".wasm",
        ".o",
        ".a",
        ".lib",
        ".obj",
        ".bmp",
        ".ico",
        ".tiff",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".flac",
        ".wav",
        ".ogg",
        ".m4a",
        ".sqlite",
        ".db",
    }
)

# Minimal environment for sandboxed commands — excludes secrets, API keys, etc.
_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "SHELL",
        "TMPDIR",
        "TZ",
    }
)

# Private/reserved IP ranges for SSRF protection
_BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Blocked hostnames for SSRF protection (cloud metadata endpoints, etc.)
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.google.internal."})
_BLOCKED_HOSTNAME_SUFFIXES = (".localhost", ".local", ".internal")

# Directories to skip during file traversal
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    "vendor",
    ".idea",
    ".vscode",
    ".cache",
    "coverage",
    ".coverage",
}


def resolve_workspace(session_id: str, repo: str | None = None, require_write: bool = False) -> str:
    """Resolve workspace root for a session.

    The session workspace at ``AHS_SESSIONS_DIR/{session_id}/`` contains:
    - A single ``repos/`` symlink → ``AHS_REPOS_DIR`` (read-only, shared
      across all sessions)
    - Real directories for writable worktrees (e.g., ``yupp-agent-fix-auth-bug/``)
    - ``history/``, ``.claude/``, ``.mcp.json`` (infrastructure, skipped)

    Resolution order for write access:
    1. Real directories (non-symlink) whose name starts with ``{repo}-`` (worktrees)
    2. Error if no worktree found

    Resolution order for read access:
    1. Worktree (same as write, preferred if it exists)
    2. Shared read-only repo via the ``repos/`` symlink (``{session_dir}/repos/{repo}/``)
    3. Shared read-only repo at ``AHS_REPOS_DIR/{repo}/`` (when no session dir)

    Raises ValueError if no workspace found or write required without worktree.
    """
    _validate_session_id(session_id)

    # Sanitize repo to prevent path traversal (e.g., "../../etc")
    if repo and (".." in repo.split(os.sep) or os.path.isabs(repo)):
        raise ValueError(f"Invalid repo name: {repo!r}")

    session_dir = os.path.join(AHS_SESSIONS_DIR, session_id)
    target_repo = repo or _DEFAULT_REPO

    if os.path.isdir(session_dir):
        # First, look for writable worktrees (real dirs, not symlinks, not infrastructure)
        worktree_dirs: list[str] = []
        for entry in os.listdir(session_dir):
            if entry.startswith(".") or entry in SESSION_INFRA_DIRS:
                continue
            full_path = os.path.join(session_dir, entry)
            if os.path.islink(full_path) or not os.path.isdir(full_path):
                continue
            worktree_dirs.append(entry)

        if repo:
            # Look for worktrees matching this repo (e.g., "yupp-agent-fix-auth-*")
            matching = [d for d in worktree_dirs if d.startswith(f"{repo}-")]
            if len(matching) == 1:
                return os.path.join(session_dir, matching[0])
            if len(matching) > 1:
                raise ValueError(f"Multiple worktrees found for {repo!r}: {matching}. Specify exact name.")
        else:
            # No repo specified — if exactly one worktree, use it
            if len(worktree_dirs) == 1:
                return os.path.join(session_dir, worktree_dirs[0])
            if len(worktree_dirs) > 1:
                raise ValueError(f"Multiple worktrees found: {worktree_dirs}. Specify 'repo' parameter.")

    # Fall back to read-only access
    if require_write:
        raise ValueError("Write access requires a worktree. Call request_write_access first.")

    # No worktree found — return the session dir itself as the workspace root.
    # The agent can browse all repo symlinks from here.
    if os.path.isdir(session_dir):
        return session_dir

    # Last resort: shared read-only repo at AHS_REPOS_DIR (no session dir at all)
    shared_path = os.path.join(AHS_REPOS_DIR, target_repo)
    if os.path.isdir(shared_path):
        return shared_path

    raise ValueError(f"No workspace found for session {session_id!r}, repo {target_repo!r}")


def safe_path(workspace_root: str, relative_path: str) -> str:
    """Resolve path within workspace, preventing escape via ../ or symlinks.

    Also allows paths that resolve to repos under ``AHS_REPOS_DIR`` — this
    handles session workspaces where the shared repos dir is symlinked in
    as ``repos/`` (i.e. ``{session_dir}/repos/ → ${AHS_REPOS_DIR}``).
    Without this, ``read_file("repos/yupp-agent/README.md")`` would be
    rejected because the real path lands outside the session directory.

    Raises ValueError if resolved path is outside workspace_root and AHS_REPOS_DIR.
    """
    real_root = os.path.realpath(workspace_root)
    joined = os.path.join(real_root, relative_path)
    real_path = os.path.realpath(joined)

    if real_path.startswith(real_root + os.sep) or real_path == real_root:
        return real_path

    # Allow paths that resolve into the shared repos directory (via symlinks)
    real_repos = os.path.realpath(AHS_REPOS_DIR)
    if real_path.startswith(real_repos + os.sep) or real_path == real_repos:
        return real_path

    raise ValueError(f"Path escapes workspace: {relative_path!r}")


_MAX_BINARY_READ_BYTES = 20 * 1024 * 1024  # 20 MB limit for binary/multimodal files


@dataclass(frozen=True)
class BinaryFileResult:
    """Result for binary files that should be returned as multimodal MCP content.

    The caller (MCP tool layer) inspects `category` to choose the right
    FastMCP wrapper: Image, Audio, or File.
    """

    data: bytes
    extension: str  # e.g. "png", "pdf" — without leading dot
    category: str  # "image" or "document"


def read_file(session_id: str, path: str, offset: int = 0, limit: int = _MAX_READ_LINES) -> str | BinaryFileResult:
    """Read file contents with line numbers, or binary data for multimodal files.

    For text files, returns cat -n style output. For image, audio, and document
    files, returns a BinaryFileResult so the MCP layer can wrap them in the
    appropriate content block (ImageContent, AudioContent, or EmbeddedResource).

    Args:
        session_id: Harness session UUID.
        path: File path relative to workspace root.
        offset: Line number to start from (0-based). Ignored for binary files.
        limit: Maximum number of lines to read. Ignored for binary files.

    Returns:
        Text file contents with line numbers (str), or BinaryFileResult for multimodal files.
    """
    workspace = resolve_workspace(session_id, require_write=False)
    full_path = safe_path(workspace, path)

    if not os.path.isfile(full_path):
        raise ValueError(f"File not found: {path}")

    ext = os.path.splitext(full_path)[1].lower()

    # Return binary data for multimodal files (images, audio, documents)
    if ext in _MULTIMODAL_EXTENSIONS:
        file_size = os.path.getsize(full_path)
        if file_size > _MAX_BINARY_READ_BYTES:
            raise ValueError(f"File too large ({file_size} bytes, max {_MAX_BINARY_READ_BYTES}): {path}")
        with open(full_path, "rb") as f:
            data = f.read()

        ext_no_dot = ext.lstrip(".")
        category = "image" if ext in _IMAGE_EXTENSIONS else "document"

        return BinaryFileResult(data=data, extension=ext_no_dot, category=category)

    # Block other binary files that AI models cannot process
    if ext in _BINARY_EXTENSIONS:
        raise ValueError(f"Cannot read binary file ({ext}): {path}")

    lines: list[str] = []
    with open(full_path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < offset:
                continue
            if len(lines) >= limit:
                break
            line_num = i + 1
            text = line.rstrip()
            if len(text) > _MAX_LINE_LENGTH:
                text = text[:_MAX_LINE_LENGTH] + f"... (truncated to {_MAX_LINE_LENGTH} chars)"
            lines.append(f"  {line_num}\t{text}")

    return "\n".join(lines)


def write_file(session_id: str, path: str, content: str) -> str:
    """Write/create file in workspace. Creates parent directories as needed.

    Args:
        session_id: Harness session UUID.
        path: File path relative to workspace root.
        content: File content to write.

    Returns:
        Confirmation message.
    """
    workspace = resolve_workspace(session_id, require_write=True)
    full_path = safe_path(workspace, path)

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)

    return f"Wrote {len(content)} bytes to {path}"


def edit_file(
    session_id: str,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Find-and-replace edit in a file.

    Args:
        session_id: Harness session UUID.
        path: File path relative to workspace root.
        old_string: Text to find.
        new_string: Replacement text.
        replace_all: If True, replace all occurrences. Otherwise, error if not unique.

    Returns:
        Confirmation with number of replacements.
    """
    workspace = resolve_workspace(session_id, require_write=True)
    full_path = safe_path(workspace, path)

    if not os.path.isfile(full_path):
        raise ValueError(f"File not found: {path}")

    with open(full_path, encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in {path}")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string found {count} times in {path}. Use replace_all=True to replace all occurrences.")

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    replaced = count if replace_all else 1
    return f"Replaced {replaced} occurrence(s) in {path}"


_MAX_LIST_FILES = 1_000


def list_files(session_id: str, pattern: str, path: str | None = None) -> str:
    """Glob-based file listing using standard glob semantics.

    Uses pathlib.Path.glob() so that ``*`` matches within a single directory
    and ``**`` matches recursively (consistent with shell glob behaviour).

    Args:
        session_id: Harness session UUID.
        pattern: Glob pattern (e.g., '**/*.py' for recursive, '*.py' for current dir only).
        path: Optional subdirectory to search in.

    Returns:
        Newline-separated relative file paths (max 1000 results).
    """
    from pathlib import Path

    # Reject patterns containing ".." to prevent workspace escape via Path.glob()
    if ".." in Path(pattern).parts:
        raise ValueError("Pattern must not contain '..' segments")

    workspace = resolve_workspace(session_id, require_write=False)
    search_root = safe_path(workspace, path) if path else workspace
    real_workspace = os.path.realpath(workspace)

    if not os.path.isdir(search_root):
        raise ValueError(f"Directory not found: {path}")

    root_path = Path(search_root)
    workspace_path = Path(workspace)

    matches: list[str] = []
    truncated = False
    for hit in root_path.glob(pattern):
        if not hit.is_file():
            continue
        # Validate each hit stays within workspace or repos dir (catches symlinks pointing outside)
        real_hit = os.path.realpath(str(hit))
        real_repos = os.path.realpath(AHS_REPOS_DIR)
        in_workspace = real_hit.startswith(real_workspace + os.sep) or real_hit == real_workspace
        in_repos = real_hit.startswith(real_repos + os.sep) or real_hit == real_repos
        if not in_workspace and not in_repos:
            continue
        # Skip entries inside directories we want to ignore
        if _SKIP_DIRS.intersection(hit.relative_to(root_path).parts):
            continue
        rel = str(hit.relative_to(workspace_path))
        matches.append(rel)
        if len(matches) >= _MAX_LIST_FILES:
            truncated = True
            break

    matches.sort()
    result = "\n".join(matches) if matches else "(no matches)"
    if truncated:
        result += f"\n\n(truncated: showing first {_MAX_LIST_FILES} matches; narrow your pattern for complete results)"
    return result


def search_files(
    session_id: str,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
) -> str:
    """Regex content search (grep-like).

    Args:
        session_id: Harness session UUID.
        pattern: Regex pattern to search for.
        path: Optional subdirectory to search in.
        glob: Optional glob pattern to filter files (e.g., '*.py').

    Returns:
        Matching lines in file:line:content format.
    """
    workspace = resolve_workspace(session_id, require_write=False)
    real_root = os.path.realpath(workspace)
    search_root = safe_path(workspace, path) if path else workspace

    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}") from e

    results: list[str] = []
    max_results = 500

    # If path points to a single file, search just that file
    real_repos = os.path.realpath(AHS_REPOS_DIR)
    if os.path.isfile(search_root):
        real_file = os.path.realpath(search_root)
        in_workspace = real_file.startswith(real_root + os.sep) or real_file == real_root
        in_repos = real_file.startswith(real_repos + os.sep) or real_file == real_repos
        if not in_workspace and not in_repos:
            raise ValueError(f"Path escapes workspace: {path}")
        rel = os.path.relpath(search_root, workspace)
        try:
            with open(search_root, encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if regex.search(line):
                        results.append(f"{rel}:{line_num}:{line.rstrip()}")
                        if len(results) >= max_results:
                            results.append(f"(truncated at {max_results} matches)")
                            return "\n".join(results)
        except (OSError, UnicodeDecodeError):
            pass
        return "\n".join(results) if results else "(no matches)"

    if not os.path.isdir(search_root):
        raise ValueError(f"Path not found: {path}")

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if glob and not fnmatch.fnmatch(filename, glob):
                continue
            full = os.path.join(dirpath, filename)
            # Validate each file against workspace root / repos dir to catch symlinks outside
            real_file = os.path.realpath(full)
            in_ws = real_file.startswith(real_root + os.sep) or real_file == real_root
            in_rp = real_file.startswith(real_repos + os.sep) or real_file == real_repos
            if not in_ws and not in_rp:
                continue
            rel = os.path.relpath(full, workspace)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append(f"{rel}:{line_num}:{line.rstrip()}")
                            if len(results) >= max_results:
                                results.append(f"(truncated at {max_results} matches)")
                                return "\n".join(results)
            except (OSError, UnicodeDecodeError):
                continue

    return "\n".join(results) if results else "(no matches)"


def _build_safe_env(workspace: str) -> dict[str, str]:
    """Build a minimal environment for sandboxed commands.

    Only includes safe variables (PATH, HOME, LANG, etc.) — excludes
    API keys, database credentials, and other secrets from the parent process.
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    # Ensure PATH is always set
    if "PATH" not in env:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    # Set HOME to workspace to prevent access to user home dir dotfiles
    env["HOME"] = workspace
    return env


def run_command(
    session_id: str,
    command: str,
    timeout: int = _DEFAULT_COMMAND_TIMEOUT,
    bwrap: bool = False,
) -> str:
    """Run a shell command in the workspace.

    When bwrap=True and bubblewrap is available, the command runs inside a
    minimal filesystem namespace (only system binaries + workspace visible).
    Falls back to plain bash when bwrap is unavailable or fails at runtime.

    Args:
        session_id: Harness session UUID.
        command: Shell command to execute.
        timeout: Timeout in seconds (max 600).
        bwrap: If True, wrap command in bubblewrap for OS-level sandboxing.

    Returns:
        Combined stdout+stderr, truncated at 100KB.
    """
    # Validate command doesn't contain null bytes (would be silently truncated by bash -c)
    if "\x00" in command:
        return "[ERROR] Command contains null bytes, which are not allowed."

    # Block `gh pr create` — PRs must go through the create_pr MCP tool for proper
    # user attribution via device-flow GitHub tokens.
    # Scan the full command (not just the first line) to catch multiline bypasses.
    if re.search(r"(^|\||&&|;|`|\$?\()\s*gh\s+pr\s+create\b", command, re.MULTILINE):
        return (
            "[ERROR] `gh pr create` is not allowed via bash. "
            "Use the `create_pr` MCP tool instead — it handles GitHub authentication "
            "so the PR is attributed to the requesting user, not the bot."
        )

    # require_write=False: allow read-only commands (e.g., ls, cat, grep) to run
    # against the symlinked repo even when no writable worktree exists yet.
    # Write attempts will fail at the filesystem level with a clear permission error.
    workspace = resolve_workspace(session_id, require_write=False)
    timeout = max(1, min(timeout, 600))
    env = _build_safe_env(workspace)

    from ypl.agent_harness_service.executors.sandbox import build_bwrap_command, bwrap_available, log_bwrap_details

    use_bwrap = bwrap and bwrap_available()
    if bwrap and not use_bwrap:
        logger.warning(
            "bwrap requested but not available, falling back to plain bash",
            session_id=session_id,
        )
    if use_bwrap:
        cmd = build_bwrap_command(command, workspace)
        log_bwrap_details(cmd, session_id, "Running command in bwrap sandbox")
    else:
        cmd = ["bash", "-c", command]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # own process group so we can kill children too
        )
    except OSError as e:
        if use_bwrap:
            # bwrap Popen failed at runtime (e.g., kernel blocks unprivileged user namespaces).
            # Fall back to plain bash and log a warning.
            # TODO: also handle bwrap starting but exiting non-zero (e.g. EPERM from
            # restricted user namespaces) — requires post-execution fallback (see PR #10579).
            logger.warning(
                "bwrap Popen failed, falling back to plain bash",
                error=str(e),
                session_id=session_id,
                workspace=workspace,
            )
            try:
                proc = subprocess.Popen(
                    ["bash", "-c", command],
                    cwd=workspace,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except OSError as e2:
                return f"[ERROR] Failed to start command: {e2}"
        else:
            return f"[ERROR] Failed to start command: {e}"

    # Read output in a background thread to avoid blocking on stdout.read()
    # when the process produces no output (e.g., `sleep`). The main thread
    # joins with a timeout to enforce the command deadline.
    chunks: list[str] = []
    truncated = False

    def _drain_stdout() -> None:
        nonlocal truncated
        assert proc.stdout is not None
        total_read = 0
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read <= _MAX_OUTPUT_BYTES:
                chunks.append(chunk)
            elif not truncated:
                overshoot = total_read - _MAX_OUTPUT_BYTES
                chunks.append(chunk[: len(chunk) - overshoot])
                truncated = True
                # Continue draining to avoid blocking the process on a full pipe

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()
    reader.join(timeout=timeout)

    if reader.is_alive():
        # Timed out — kill the entire process group (bash + any child processes)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()  # fallback if process group kill fails
        reader.join()
        proc.wait()
        return "".join(chunks) + f"\n[ERROR] Command timed out after {timeout}s"

    proc.wait()

    output = "".join(chunks)
    if truncated:
        output += f"\n[truncated at {_MAX_OUTPUT_BYTES} bytes]"
    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"

    return output


def _validate_url(url: str) -> None:
    """Validate URL for SSRF protection.

    Blocks non-HTTP schemes, private/reserved IPs, and cloud metadata endpoints.
    Raises ValueError if the URL is not allowed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed, got: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # Block known dangerous hostnames (cloud metadata, local services)
    hostname_lower = hostname.lower()
    if hostname_lower in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Access to {hostname!r} is not allowed")
    if hostname_lower.endswith(_BLOCKED_HOSTNAME_SUFFIXES):
        raise ValueError(f"Access to {hostname!r} is not allowed")

    # Resolve hostname to IP and check against blocked ranges
    import socket

    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname!r}") from None

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        # Unwrap IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1) so they
        # match against IPv4 blocked networks.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        for network in _BLOCKED_IP_NETWORKS:
            if ip in network:
                raise ValueError(f"Access to private/reserved IP ranges is not allowed: {hostname}")


async def fetch_url(url: str, prompt: str | None = None) -> str:
    """Fetch URL content and return as text.

    Validates URL scheme (http/https only) and blocks private/reserved IPs
    to prevent SSRF attacks. Streams response to avoid OOM on large content.

    Args:
        url: URL to fetch.
        prompt: Unused (kept for API compatibility with Claude Code's WebFetch).

    Returns:
        Page content as plain text (HTML tags stripped).
    """
    _validate_url(url)

    async def _check_redirect(response: httpx.Response) -> None:
        """Re-validate redirect targets to prevent SSRF via open redirects."""
        if response.is_redirect and response.next_request:
            _validate_url(str(response.next_request.url))

    client = httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=30.0,
        event_hooks={"response": [_check_redirect]},
    )
    async with client, client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        chunks: list[str] = []
        total_read = 0
        async for chunk in response.aiter_text():
            total_read += len(chunk)
            if total_read <= _MAX_OUTPUT_BYTES:
                chunks.append(chunk)
            else:
                overshoot = total_read - _MAX_OUTPUT_BYTES
                chunks.append(chunk[: len(chunk) - overshoot])
                chunks.append(f"\n[truncated at {_MAX_OUTPUT_BYTES} bytes]")
                break
        text = "".join(chunks)

    # Basic HTML-to-text: strip tags if HTML content
    if "html" in content_type:
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

    return text


async def search_web(query: str, num_results: int = 10) -> str:
    """Search the web using SearchAPI.io (Google engine).

    Args:
        query: Search query string.
        num_results: Maximum number of results to return (default 10, clamped to 1-20).

    Returns:
        Formatted text results with title, URL, and snippet per result.
    """
    num_results = max(1, min(num_results, 20))

    api_key = os.environ.get("SEARCHAPI_API_KEY")
    if not api_key:
        raise ValueError("SEARCHAPI_API_KEY environment variable is not set")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://www.searchapi.io/api/v1/search",
            params={"engine": "google", "q": query, "num": num_results},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("organic_results", [])
    if not results:
        return "No results found."

    lines: list[str] = []
    for r in results:
        lines.append(f"Title: {r.get('title', '')}")
        lines.append(f"URL: {r.get('link', '')}")
        lines.append(f"Snippet: {r.get('snippet', '')}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# BCH manager registration helpers
# ---------------------------------------------------------------------------


def set_command_handler_manager(session_id: str, manager: ToolDispatcher | None) -> None:
    """Register or clear the BCH CommandHandlerManager for a session.

    Called by service.py (A5) when a session starts or ends.  When a manager
    is registered, the async MCP tool wrappers in local_mcp_server.py forward
    calls to the warm bwrapped proxy instead of spawning a new bwrap process.

    Args:
        session_id: The AHS session UUID.
        manager: The ``CommandHandlerManager`` instance to register, or
            ``None`` to deregister (called during session teardown).
    """
    if manager is None:
        _session_managers.pop(session_id, None)
    else:
        _session_managers[session_id] = manager


def get_command_handler_manager(session_id: str) -> ToolDispatcher | None:
    """Return the BCH manager for a session, or None if not registered."""
    return _session_managers.get(session_id)

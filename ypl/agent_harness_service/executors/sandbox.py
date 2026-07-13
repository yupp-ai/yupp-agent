"""Sandboxing and security isolation for agent execution.

Provides bubblewrap (bwrap) integration for both raw executors (individual bash
commands) and harnessed executors (Claude Code CLI / Codex CLI). Bwrap creates
a minimal Linux namespace where only explicitly mounted paths are visible.

See also: ~/projects/ahs/bubblewrap-sandboxing.md for deployment docs.
"""

import os
import subprocess
from functools import lru_cache

from ypl.agent_harness_service.common.constants import AHS_REPOS_DIR
from ypl.structured_logger import get_logger

logger = get_logger()

# Shared skills live under <repo>/.agents/skills/ and are symlinked from
# ~/.claude/skills. Only symlink targets under these prefixes are mounted into
# the bwrap sandbox — this prevents a sandboxed agent from planting symlinks
# to exfiltrate arbitrary host paths on subsequent runs.
_SERVICE_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ALLOWED_SYMLINK_TARGET_PREFIXES: tuple[str, ...] = (
    os.path.realpath(os.path.join(_SERVICE_REPO_DIR, ".agents", "skills")),
)

# Real path to skills directory — used for direct --ro-bind mounts so agents
# can read SKILL.md files and run scripts. Codex reads from ~/.codex/skills/
# (symlinks resolved outside bwrap), Claude Code reads from ~/.claude/skills/
# (inner symlinks resolved by _resolve_inner_symlinks), and raw executors
# access via the load_skill() MCP tool (which reads from this path directly).
_REAL_SKILLS_DIR = os.path.realpath(os.path.join(_SERVICE_REPO_DIR, ".agents", "skills"))

# ---------------------------------------------------------------------------
# System mounts (shared by raw and CLI executors)
# ---------------------------------------------------------------------------

# System paths to mount read-only inside bwrap sandbox.
# Whitelist approach: only these paths are visible to the sandboxed command.
#
# /opt is mounted because the service repo (/opt/yupp-agent) ships the
# code, skills under .agents/, and the poetry venv at .venv/ — all of
# which the CLI and agent-authored bash commands legitimately need.
#
# Secrets must NOT live under /opt. The ``.env`` file belongs at
# /data/ahs/.env (outside the sandbox mount set). Having it at
# /opt/yupp-agent/.env was the root cause of the 2026-04-22 prod-DB
# orphan-revision incident — an agent read the file and then ran alembic
# against prod Postgres. See
# docs/plans/2026-04-22-single-box-agent-sandbox-hardening.md.
_BWRAP_RO_BINDS: list[str] = [
    "/usr",
    "/opt",  # service repo + dependency artifacts; no secrets allowed under /opt
    "/etc/ssl",  # TLS certificates
    "/etc/ca-certificates",
    "/etc/alternatives",
    "/etc/passwd",  # needed by git, python (getpwuid)
    "/etc/group",  # needed by some tools
    "/etc/gitconfig",  # system git config
    "/etc/resolv.conf",  # DNS resolution (only useful if network not unshared)
]

# Merged-usr compatibility: On modern distros (Ubuntu 20.04+, Debian 12+, Fedora 17+),
# /bin, /lib, /lib64, /sbin are symlinks into /usr/* ("merged-usr" layout).
# bwrap does not follow host symlinks, so we must recreate them with --symlink
# inside the namespace. On older distros where these are real directories, we
# fall back to --ro-bind. _bwrap_system_mounts() detects which layout the host uses.
_MERGED_USR_PATHS: list[tuple[str, str]] = [
    ("/bin", "usr/bin"),
    ("/sbin", "usr/sbin"),
    ("/lib", "usr/lib"),
    ("/lib64", "usr/lib64"),  # may not exist on all systems
]

# ---------------------------------------------------------------------------
# CLI executor mounts (Claude Code / Codex)
# ---------------------------------------------------------------------------
# All paths are relative to $HOME. Review and update when the CLI installation
# method changes or when deploying on a new VM image.

# .git/ subdirectories that git must write to during commit/push/fetch.
# Mounting the full .git/ as --bind would expose repo config, hooks, and
# alternates to the sandboxed process. Instead we overlay only these subdirs.
#
# Rationale per subdir:
#   objects/   — new commit/blob/tree objects (git commit, git fetch)
#   refs/      — branch and remote ref pointers (git commit, git fetch)
#   worktrees/ — per-worktree metadata: HEAD, COMMIT_EDITMSG, logs/HEAD
#   logs/      — reflog for branches/HEAD (git commit writes here)
#
# Not included: config, hooks, info, packed-refs (file not dir), description.
# packed-refs is only written by git pack-refs / git gc, not during normal
# commit/push — and those maintenance commands should not run in the sandbox.
_GIT_RW_SUBDIRS: list[str] = ["objects", "refs", "worktrees", "logs"]

# Read-write: directories the CLI writes to at runtime.
# RW is required because the CLI persists session state, conversation history,
# and settings here. A per-session overlay would be ideal for isolation but the
# CLI expects a persistent ~/.claude directory across session resumes.
_CLI_HOME_RW_BINDS: list[str] = [
    ".claude",  # Claude CLI session state, settings, CLAUDE.md
    ".codex",  # Codex CLI session state, settings, skills
]

# Read-only: directories and files the CLI reads from but never writes to.
_CLI_HOME_RO_BINDS: list[str] = [
    ".local/bin",  # claude binary (symlink to .local/share/claude/versions/<ver>)
    ".local/share",  # Claude CLI versions (symlink target for .local/bin/claude)
    ".nvm",  # Node.js version manager (nvm-managed Node.js installs)
    ".npm",  # npm global cache / config
    ".node",  # Alternative Node.js installation directory
    ".gitconfig",  # user git config (credential helper pointing to gh)
    ".config/gh",  # gh CLI stored token for GitHub auth (git push, gh pr create)
    ".claude.json",  # Claude CLI user config (theme, telemetry, etc.) — suppresses startup warning
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bwrap_system_mounts() -> list[str]:
    """Build bwrap args for system directories.

    Detects merged-usr vs traditional layout and uses --symlink or --ro-bind
    accordingly. This is needed because bwrap doesn't follow host symlinks —
    on Ubuntu 20.04+ where /bin -> /usr/bin, a --ro-bind /bin /bin would
    make /bin appear empty (the dynamic linker can't find libraries).
    """
    args: list[str] = []
    for path in _BWRAP_RO_BINDS:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    for real_path, usr_target in _MERGED_USR_PATHS:
        if os.path.islink(real_path):
            # Merged-usr: recreate the symlink inside the namespace
            args += ["--symlink", usr_target, real_path]
        elif os.path.isdir(real_path):
            # Traditional layout: mount as read-only
            args += ["--ro-bind", real_path, real_path]
    return args


@lru_cache(maxsize=1)
def bwrap_available() -> bool:
    """Check if bwrap is installed AND a minimal sandbox actually works.

    Previously this only ran ``bwrap --version``. That's insufficient on
    Ubuntu 24.04+ where ``kernel.apparmor_restrict_unprivileged_userns=1``
    lets ``bwrap --version`` succeed (no namespace needed) while any real
    sandbox invocation fails with ``setting up uid map: Permission denied``.

    The symptom was that runner.py saw ``bwrap_available() == True``, built a
    bwrap wrapper, and then the CLI subprocess died at spawn time — or, worse,
    the ``use_bwrap`` logic silently skipped the wrap and ran the agent
    completely unsandboxed. Both paths have been observed on your-vm-host.

    This tightened check exec's ``/usr/bin/true`` inside a minimal namespace.
    If that fails, bwrap is effectively unusable and callers must treat this
    as "no sandbox available."
    """
    try:
        # Smallest possible sandbox that still needs the dynamic linker.
        # ``/lib`` and ``/lib64`` are symlinks into ``/usr`` on merged-usr
        # distros, so we recreate them with ``--symlink``.
        result = subprocess.run(
            [
                "bwrap",
                "--ro-bind",
                "/usr",
                "/usr",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--",
                "/usr/bin/true",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "bwrap binary present but minimal sandbox invocation failed — "
            "check kernel.apparmor_restrict_unprivileged_userns (Ubuntu 24+).",
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _resolve_inner_symlinks(
    directory: str,
    allowed_prefixes: tuple[str, ...] = (),
) -> list[str]:
    """Build --ro-bind args for symlink targets inside a mounted directory.

    bwrap mounts don't follow symlinks — if a directory contains symlinks
    whose targets live outside the mount namespace (e.g. ~/.claude/skills ->
    /opt/yupp-agent/.agents/skills/), the symlink will dangle inside the
    sandbox. This function scans for such symlinks and returns --ro-bind
    args so their real targets are visible.

    Security: Only symlink targets under ``allowed_prefixes`` are mounted.
    This prevents a sandboxed process from planting symlinks (e.g.
    ~/.claude/x -> /root/.ssh/) that would be mounted on the next invocation.
    """
    args: list[str] = []
    try:
        for entry in os.listdir(directory):
            entry_path = os.path.join(directory, entry)
            if os.path.islink(entry_path):
                real = os.path.realpath(entry_path)
                if not os.path.exists(real) or real == entry_path:
                    continue
                if not any(real.startswith(p + os.sep) or real == p for p in allowed_prefixes):
                    logger.warning(
                        "Skipping symlink target outside allowed prefixes",
                        symlink=entry_path,
                        target=real,
                        allowed_prefixes=allowed_prefixes,
                    )
                    continue
                args += ["--ro-bind", real, real]
    except OSError as e:
        logger.warning("Failed to resolve inner symlinks", directory=directory, error=str(e))
    return args


def _resolve_workspace_symlinks(workspace: str) -> list[tuple[str, str]]:
    """Resolve the workspace ``repos/`` symlink to per-repo bind tuples.

    Every session workspace contains a single ``repos/`` symlink pointing at
    ``AHS_REPOS_DIR`` (set up in ``session_lifecycle.py``). To preserve the
    existing per-repo bwrap mount contract — system mounts each repo
    read-only and overlays specific ``.git/`` subdirs as read-write — we
    enumerate the children of ``AHS_REPOS_DIR`` and return one entry per
    repo.

    The symlink target is validated against ``AHS_REPOS_DIR`` to prevent a
    writable workspace from injecting arbitrary host paths into the bwrap
    mount list (symlink escape attack). ``AHS_REPOS_DIR`` itself is
    operator-controlled, so its children are trusted (in local dev, repos
    inside ``AHS_REPOS_DIR`` are commonly symlinks to a checkout elsewhere).

    The materialized ``agent_memories/`` directory is a regular subdirectory
    of the workspace (no symlink), so it is mounted via the workspace bind
    and needs no special handling here.

    Returns:
        A list of ``(real_path, symlink_path)`` tuples for each repo under
        the workspace's ``repos/`` symlink (mounted read-only).
    """
    repo_binds: list[tuple[str, str]] = []
    if not os.path.isdir(workspace):
        return repo_binds

    repos_link = os.path.join(workspace, "repos")
    if not (os.path.islink(repos_link) and os.path.isdir(repos_link)):
        return repo_binds

    real_repos_dir = os.path.realpath(AHS_REPOS_DIR)
    real_link_target = os.path.realpath(repos_link)
    if real_link_target != real_repos_dir:
        logger.warning(
            "Skipping workspace ``repos/`` symlink whose target is not AHS_REPOS_DIR",
            symlink=repos_link,
            target=real_link_target,
            repos_dir=real_repos_dir,
        )
        return repo_binds

    for entry in sorted(os.listdir(real_repos_dir)):
        child = os.path.join(real_repos_dir, entry)
        if not os.path.isdir(child):
            continue
        real_child = os.path.realpath(child)
        # ``virtual`` is the path the agent sees inside the workspace — the
        # caller uses it for diagnostic logging only; bwrap binds the real
        # path on both sides of the mount.
        virtual = os.path.join(repos_link, entry)
        repo_binds.append((real_child, virtual))
    return repo_binds


def log_bwrap_details(bwrap_args: list[str], session_id: str, label: str) -> None:
    """Log bwrap sandbox parameters for observability."""
    ro_binds: list[str] = []
    rw_binds: list[str] = []
    symlinks: list[str] = []
    namespaces: list[str] = []
    i = 0
    while i < len(bwrap_args):
        arg = bwrap_args[i]
        if arg == "--ro-bind" and i + 2 < len(bwrap_args):
            ro_binds.append(bwrap_args[i + 1])
            i += 3
        elif arg == "--bind" and i + 2 < len(bwrap_args):
            rw_binds.append(bwrap_args[i + 1])
            i += 3
        elif arg == "--symlink" and i + 2 < len(bwrap_args):
            symlinks.append(f"{bwrap_args[i + 2]}->{bwrap_args[i + 1]}")
            i += 3
        elif arg.startswith("--unshare-"):
            namespaces.append(arg.removeprefix("--unshare-"))
            i += 1
        else:
            i += 1
    # Extract just the command name (first arg after "--" separator)
    try:
        sep_index = bwrap_args.index("--")
        command_name = bwrap_args[sep_index + 1] if sep_index + 1 < len(bwrap_args) else "(empty)"
    except ValueError:
        command_name = bwrap_args[0] if bwrap_args else "(empty)"
    logger.info(
        f"{label}: {command_name}",
        session_id=session_id,
        command_name=command_name,
        ro_binds=ro_binds,
        rw_binds=rw_binds,
        symlinks=symlinks,
        namespaces=namespaces,
    )


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def build_bwrap_command(command: str, workspace: str) -> list[str]:
    """Build a bwrap-wrapped command line for raw executor bash commands.

    Creates a minimal filesystem namespace:
    - System binaries/libraries (read-only)
    - Workspace directory (read-write)
    - Each shared repo under ``AHS_REPOS_DIR`` (read-only — the workspace's
      ``repos/`` symlink resolves there)
    - Ephemeral /tmp
    - Minimal /dev and /proc
    - Isolated PID namespace (--unshare-pid)
    - Isolated IPC namespace (--unshare-ipc)
    - Isolated UTS namespace (--unshare-uts)

    Note: git credential files (~/.gitconfig, ~/.config/gh) are intentionally NOT
    mounted here — raw bash commands should not have access to GitHub credentials.
    Git operations (push, fetch) are performed by the CLI executor, not raw bash.

    Network access is preserved (no --unshare-net) so raw bash commands can make
    outbound connections (e.g., API calls, package downloads).
    """
    args = ["bwrap"]

    # System mounts (handles merged-usr symlinks on modern distros)
    args += _bwrap_system_mounts()

    # Resolve workspace ``repos/`` symlink — bwrap doesn't follow host
    # symlinks, so we mount each shared repo explicitly.
    repo_binds = _resolve_workspace_symlinks(workspace)
    for real_path, _symlink_path in repo_binds:
        args += ["--ro-bind", real_path, real_path]

    # Skills directory (read-only — SKILL.md files and any scripts).
    # Mounted directly so raw executor bash commands and load_skill() MCP tool
    # can access skill content without per-session symlinks.
    if os.path.isdir(_REAL_SKILLS_DIR):
        args += ["--ro-bind", _REAL_SKILLS_DIR, _REAL_SKILLS_DIR]

    # Read-write workspace (includes agent_memories/ as a real subdir).
    args += ["--bind", workspace, workspace]

    # Ephemeral /tmp, minimal /dev, /proc
    args += ["--tmpfs", "/tmp"]
    args += ["--dev", "/dev"]
    args += ["--proc", "/proc"]

    # Namespace isolation
    # Note: no --unshare-net — network access is preserved for raw bash commands
    args += ["--unshare-pid"]  # PID isolation — prevents /proc/<host-pid>/environ leak
    args += ["--unshare-ipc"]  # IPC isolation — prevents shared memory enumeration
    args += ["--unshare-uts"]  # UTS isolation — prevents hostname changes

    # Kill sandbox if parent (harness) dies
    args += ["--die-with-parent"]

    # Set working directory
    args += ["--chdir", workspace]

    # The actual command
    args += ["bash", "-c", command]

    return args


def build_bwrap_cli_command(args: list[str], workspace: str) -> list[str]:
    """Wrap a Claude Code CLI command in bubblewrap for filesystem isolation.

    Unlike the raw executor bwrap (``build_bwrap_command``), this does NOT
    unshare the network — the CLI needs outbound HTTPS for the Anthropic API
    and MCP servers.

    The CLI only needs access to:
    - System binaries/libraries (read-only)
    - CLI home dirs (~/.claude, ~/.local/bin, ~/.nvm, etc.)
    - Git credential files (~/.gitconfig, ~/.config/gh — read-only)
    - Repo source trees (read-only) with specific .git/ subdirs overlaid read-write
      (objects/, refs/, worktrees/, logs/ — enough for git commit/push/fetch)
    - Session workspace (read-write — worktrees, attachments, history)

    Identity files (ROLE.md, WORKSPACE.md) are NOT mounted — they
    are read by the harness and injected via --system-prompt.

    Args:
        args: The original CLI command as a list of strings (e.g., ["claude", "-p", ...]).
        workspace: Path to the session workspace directory.

    Returns:
        New command list with bwrap prefix.
    """
    bwrap_args = ["bwrap"]

    # System mounts (handles merged-usr symlinks on modern distros)
    bwrap_args += _bwrap_system_mounts()

    # CLI home directory mounts
    home_dir = os.path.expanduser("~")
    for subdir in _CLI_HOME_RW_BINDS:
        path = os.path.join(home_dir, subdir)
        if os.path.isdir(path):
            # RW needed: CLI writes session state, settings, and conversation
            # history here. A per-session overlay would be ideal but the CLI
            # expects a persistent ~/.claude across resumes.
            bwrap_args += ["--bind", path, path]
            # Resolve symlinks inside mounted dirs (e.g. ~/.claude/skills ->
            # /opt/yupp-agent/.agents/skills/) so their targets are reachable.
            # Without this, symlinks pointing outside the mount namespace dangle.
            bwrap_args += _resolve_inner_symlinks(path, _ALLOWED_SYMLINK_TARGET_PREFIXES)
    for subdir in _CLI_HOME_RO_BINDS:
        path = os.path.join(home_dir, subdir)
        if os.path.exists(path):
            bwrap_args += ["--ro-bind", path, path]

    # Repos: source tree + most of .git/ read-only; only specific .git/ subdirs
    # are overlaid as --bind (read-write). This protects repo config, hooks,
    # and alternates while still allowing git commit/push/fetch to work.
    repo_binds = _resolve_workspace_symlinks(workspace)
    for real_path, _symlink_path in repo_binds:
        bwrap_args += ["--ro-bind", real_path, real_path]
        git_dir = os.path.join(real_path, ".git")
        if os.path.isdir(git_dir):
            for subdir in _GIT_RW_SUBDIRS:
                subdir_path = os.path.join(git_dir, subdir)
                # Pre-create subdirs that git creates lazily (e.g. worktrees/
                # only appears after the first `git worktree add`). Without
                # this, repos that have never had worktrees get a read-only
                # .git/worktrees/ inside bwrap, blocking git add in worktrees
                # created later by the harness.
                os.makedirs(subdir_path, exist_ok=True)
                bwrap_args += ["--bind", subdir_path, subdir_path]

    # Read-write session workspace (worktrees, attachments, history,
    # agent_memories/).
    bwrap_args += ["--bind", workspace, workspace]

    # Ephemeral /tmp, minimal /dev, /proc
    bwrap_args += ["--tmpfs", "/tmp"]
    bwrap_args += ["--dev", "/dev"]
    bwrap_args += ["--proc", "/proc"]

    # Namespace isolation (NO --unshare-net: CLI needs HTTPS for API + MCP)
    bwrap_args += ["--unshare-pid"]
    bwrap_args += ["--unshare-ipc"]
    bwrap_args += ["--unshare-uts"]

    # Kill sandbox if parent (harness) dies
    bwrap_args += ["--die-with-parent"]

    # Set working directory
    bwrap_args += ["--chdir", workspace]

    # The actual CLI command
    bwrap_args += args

    return bwrap_args

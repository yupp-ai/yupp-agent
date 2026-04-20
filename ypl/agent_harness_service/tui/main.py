#!/usr/bin/env python3
"""ahstui — interactive TUI for the Agent Harness Service.

Usage:
  ahstui                              Resume last session
  ahstui --agent sre                  New session with agent
  ahstui --agent sre "first message"  New session + initial message
  ahstui --session-id <uuid>          Resume specific session
  ahstui --host localhost:8090        Point to a different AHS host
  ahstui --logout                     Clear cached Google credentials

Commands (inside TUI):
  /help        Show help
  /stop        Stop the current agent turn
  /clear       Clear the chat log
  /history     Reload message history
  /agents      List available agents
  /new [agent] Start new session (default: same agent)
  /sessions    List your sessions (grouped by parent)
  /allsessions List all sessions
  /attach <id> Attach to another session
  /projects    Browse projects and tasks (Ctrl+O)
  /exit        Quit the TUI

Environment:
  AGENT_HARNESS_SERVICE_API_KEY / AHS_API_KEY   API key
"""

from __future__ import annotations
import argparse
import json
import os
import sys

try:
    from ypl.agent_harness_service.tui.chat import AHSTui
    from ypl.agent_harness_service.tui.config import _init_connection, _load_session_id
except ImportError:
    print(
        "ERROR: textual is required. Install with: poetry install -E ahstui",
        file=sys.stderr,
    )
    sys.exit(1)


def _resolve_user_id(email: str) -> str:
    """Call the AHS /resolve_user endpoint to map email → user_id."""
    import urllib.error
    import urllib.request

    from ypl.agent_harness_service.tui.config import get_api_key, get_http_base

    url = f"{get_http_base()}/resolve_user"
    data = json.dumps({"email": email}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-API-Key": get_api_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            user_id = body.get("user_id", "")
            if not user_id:
                print(f"ERROR: /resolve_user returned no user_id for {email}", file=sys.stderr)
                sys.exit(1)
            return str(user_id)
    except (urllib.error.URLError, json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Failed to resolve user ID for {email}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ahstui", description="Interactive TUI for Agent Harness Service")
    parser.add_argument("message", nargs="?", default=None, help="Optional first message (creates new session)")
    parser.add_argument("--agent", default=None, help="Agent name for new session (e.g., sre)")
    parser.add_argument("--session-id", default=None, help="Resume a specific session by ID")
    parser.add_argument(
        "--host",
        default=None,
        help="AHS host (or set AHS_HOST env var). Examples: localhost:8090, https://ahs.example.com",
    )
    parser.add_argument("--logout", action="store_true", help="Clear cached Google credentials and exit")
    parser.add_argument(
        "-u",
        "--user-id",
        default=None,
        help=(
            "Skip Google OAuth + /resolve_user lookup and use this user_id directly. "
            "Useful for local monolith testing when you already know a user_id from the users table "
            "or want to bypass OAuth entirely."
        ),
    )
    args = parser.parse_args()

    # Handle --logout before anything else
    if args.logout:
        from ypl.agent_harness_service.tui.auth import logout

        logout()
        return

    _init_connection(args.host)

    if args.user_id:
        # Explicit override path: skip OAuth + skip /resolve_user entirely.
        # We don't know the email in this mode, which is fine — nothing downstream
        # in the TUI needs it once user_id is known.
        print(f"Using user_id override: {args.user_id} (skipping OAuth + /resolve_user)")
        user_id = args.user_id
    else:
        # Google OAuth login (opens browser if needed, uses cached token otherwise)
        from ypl.agent_harness_service.tui.auth import login

        email = login()
        print(f"Logged in as {email}")

        # Resolve email → user_id via AHS API
        user_id = _resolve_user_id(email)

    app = AHSTui(
        agent=args.agent,
        session_id=args.session_id,
        initial_message=args.message,
        user_id=user_id,
    )
    app.run()

    # After exit, print resume command with all connection params
    sid = app._session_id or _load_session_id()
    if sid:
        script = os.path.relpath(__file__)
        parts = [f"python {script}", f"--session-id {sid}"]
        if args.host:
            parts.append(f"--host {args.host}")
        print(f"\nTo resume this session:\n  {' '.join(parts)}\n")


if __name__ == "__main__":
    main()

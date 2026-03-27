#!/usr/bin/env python3
"""ahscli — lightweight CLI for the Agent Harness Service.

Usage:
  ahscli create [--agent sre] ['message']         Create a session (optionally send first message)
  ahscli message 'text'                            Send a message to the current session
  ahscli message 'text' --session-id <id>          Send a message to a specific session
  ahscli history                                   Show message history (newest first)
  ahscli history --session-id <id>                 Show history for a specific session
  ahscli feedback 1                                Positive feedback on last session
  ahscli feedback 0 --message-id <id>              Negative feedback on a specific message

Environment:
  AHS_HOST       Server host (default: localhost)
  AHS_PORT       Server port (default: 8090)
  AGENT_HARNESS_SERVICE_API_KEY   API key (reads from .env, same as server; AHS_API_KEY accepted as fallback)
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SESSION_FILE = Path("/tmp/LAST_AHS_SESSION_ID")
USER_ID_FILE = Path("/tmp/LAST_AHS_USER_ID")


def _resolve_api_key() -> str:
    key = os.environ.get("AGENT_HARNESS_SERVICE_API_KEY") or os.environ.get("AHS_API_KEY", "")
    if not key:
        print("ERROR: AGENT_HARNESS_SERVICE_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    return key


def _base_url() -> str:
    host = os.environ.get("AHS_HOST", "localhost")
    port = os.environ.get("AHS_PORT", "8090")
    return f"http://{host}:{port}/ahs"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{_base_url()}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", _resolve_api_key())
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode())  # type: ignore[no-any-return]
    except HTTPError as e:
        detail = e.read().decode() if e.fp else str(e)
        print(f"ERROR {e.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Could not connect — {e.reason}", file=sys.stderr)
        sys.exit(1)


def _save_session(sid: str) -> None:
    SESSION_FILE.write_text(sid)
    print(f"  [/tmp] Wrote session_id to {SESSION_FILE}")


def _load_session(args: argparse.Namespace) -> str:
    sid: str | None = getattr(args, "session_id", None)
    if sid:
        return sid
    if SESSION_FILE.exists():
        sid = SESSION_FILE.read_text().strip()
        print(f"  [/tmp] Read session_id from {SESSION_FILE}: {sid}")
        return sid
    print("ERROR: No session ID. Use --session-id or run 'ahscli create' first.", file=sys.stderr)
    sys.exit(1)


def _save_user_id(user_id: str) -> None:
    USER_ID_FILE.write_text(user_id)
    print(f"  [/tmp] Wrote user_id to {USER_ID_FILE}")


def _load_user_id(args: argparse.Namespace) -> str | None:
    """Load user_id from args or /tmp file. Returns None if not available."""
    uid: str | None = getattr(args, "user_id", None)
    if uid:
        return uid
    if USER_ID_FILE.exists():
        uid = USER_ID_FILE.read_text().strip()
        if uid:
            print(f"  [/tmp] Read user_id from {USER_ID_FILE}: {uid}")
            return uid
    return None


# --- Commands ---


def cmd_create(args: argparse.Namespace) -> None:
    user_id = _load_user_id(args)
    body: dict = {"agent_id": args.agent, "trigger": "api", "source": "cli"}
    if args.message:
        body["message"] = args.message
    if args.session_id:
        body["session_id"] = args.session_id
    if user_id:
        body["user_id"] = user_id
        _save_user_id(user_id)

    resp = _request("POST", "/session/create", body)
    sid = resp.get("session_id", "")
    if sid:
        _save_session(sid)
        print(f"Session: {resp['session_id']}")
        print(f"Status:  {resp['status']}")
    else:
        print(f"ERROR: {json.dumps(resp)}", file=sys.stderr)
        sys.exit(1)


def cmd_message(args: argparse.Namespace) -> None:
    sid = _load_session(args)
    user_id = _load_user_id(args)
    body: dict = {"session_id": sid, "message": args.message, "source": "cli"}
    if user_id:
        body["user_id"] = user_id
    resp = _request("POST", "/session/message", body)
    print(f"Session: {resp.get('session_id', '?')}")
    print(f"Turn:    {resp.get('turn_number', '?')}")
    print(f"Status:  {resp.get('status', '?')}")


def cmd_history(args: argparse.Namespace) -> None:
    sid = _load_session(args)
    params = f"?limit={args.limit}&offset={args.offset}"
    resp = _request("GET", f"/session/{sid}/history{params}")
    total = resp.get("total_messages", "?")
    print(
        f"Session: {resp['session_id']}  Agent: {resp['agent_id']}  Status: {resp['status']}"
        f"  ({total} total messages, showing {args.offset}–{args.offset + len(resp.get('messages', []))})"
    )
    divider = "=" * 80
    for msg in resp.get("messages", []):
        print(divider)
        role = msg["role"].upper()
        mid = msg["message_id"]
        content = msg.get("content") or "(empty)"
        extras = ""
        if msg.get("cost_usd"):
            extras += f"  est_cost=${msg['cost_usd']}"
        if msg.get("duration_ms"):
            extras += f"  {msg['duration_ms']}ms"
        print(f"[{role}] {mid}{extras}")
        print()
        print(content)
    print(divider)


def cmd_feedback(args: argparse.Namespace) -> None:
    sid = _load_session(args)
    body: dict = {"session_id": sid}
    if args.rating is not None:
        body["rating"] = "POSITIVE" if args.rating == 1 else "NEGATIVE"
    if args.message_id:
        body["message_id"] = args.message_id

    resp = _request("POST", "/session/feedback", body)
    print(json.dumps(resp, indent=2))


def cmd_agents(args: argparse.Namespace) -> None:
    resp = _request("GET", "/agents")
    agents = resp.get("agents", [])
    if not agents:
        print("No agents found.")
        return
    # Table header
    fmt = "{:<20} {:<15} {:<12} {:<18} {:<6} {:<8} {:<8}"
    print(fmt.format("NAME", "DISPLAY", "EXECUTOR", "MODEL", "TURNS", "BUDGET", "TIMEOUT"))
    print("-" * 90)
    for a in agents:
        model = a.get("executor_model") or a.get("llm_model") or "-"
        print(
            fmt.format(
                a["name"][:20],
                (a.get("display_name") or "-")[:15],
                a.get("executor_type", "-")[:12],
                model[:18],
                str(a.get("max_turns", "-")),
                f"${a.get('max_budget_usd', 0):.1f}",
                f"{a.get('timeout_s', 0)}s",
            )
        )
        if args.descriptions and a.get("description"):
            print(f"  {a['description']}")


def cmd_agent(args: argparse.Namespace) -> None:
    params = "include_system_prompts=true" if args.prompts else ""
    resp = _request("GET", f"/agent/{args.name}?{params}")
    a = resp.get("agent", {})
    model = a.get("executor_model") or a.get("llm_model") or "-"
    print(f"Name:        {a.get('name')}")
    print(f"Display:     {a.get('display_name') or '-'}")
    print(f"Description: {a.get('description') or '-'}")
    print(f"Executor:    {a.get('executor_type')}  Model: {model}")
    print(f"Turns: {a.get('max_turns')}  Budget: ${a.get('max_budget_usd', 0):.1f}  Timeout: {a.get('timeout_s')}s")
    print(f"Sandbox:     {a.get('sandbox_enabled')}")
    print(f"Repo:        {a.get('default_repo')}")
    print(f"Tools:       {json.dumps(a.get('tool_permissions', {}))}")
    print(f"Subagents:   {a.get('allowed_subagents', [])}")
    print(f"Gateways:    {a.get('allowed_gateways', [])}")
    prompts = resp.get("system_prompts")
    if prompts:
        print(f"\nSystem Prompts ({len(prompts)} files):")
        for key, content in prompts.items():
            lines = content.count("\n") + 1
            print(f"\n--- {key} ({lines} lines) ---")
            print(content)


def _print_sessions_table(sessions: list[dict]) -> None:
    fmt = "{:<38} {:<14} {:<10} {:<8} {:<20} {:<5}"
    print(fmt.format("SESSION_ID", "AGENT", "STATUS", "TRIGGER", "CREATED", "MSGS"))
    print("-" * 100)
    for s in sessions:
        created = (s.get("created_at") or "-")[:19]
        print(
            fmt.format(
                s["session_id"][:38],
                s.get("agent_name", "-")[:14],
                s.get("status", "-")[:10],
                s.get("trigger", "-")[:8],
                created,
                str(s.get("message_count", 0)),
            )
        )


def cmd_sessions(args: argparse.Namespace) -> None:
    params: list[str] = [f"limit={args.limit}", f"offset={args.offset}"]
    if args.status:
        params.append(f"status={args.status}")
    if args.agent:
        params.append(f"agent_name={args.agent}")
    if args.trigger:
        params.append(f"trigger={args.trigger}")
    if args.since:
        params.append(f"since={args.since}")
    if args.until:
        params.append(f"until={args.until}")
    if args.include_subsessions:
        params.append("root_sessions_only=false")

    resp = _request("GET", f"/sessions?{'&'.join(params)}")
    total = resp.get("total", 0)
    sessions = resp.get("sessions", [])
    print(f"Total: {total}  Showing: {resp.get('offset', 0)}–{resp.get('offset', 0) + len(sessions)}")
    print()
    _print_sessions_table(sessions)


def cmd_session(args: argparse.Namespace) -> None:
    sid = _load_session(args)
    resp = _request("GET", f"/session/{sid}")
    s = resp.get("session", {})
    print(f"Session: {s.get('session_id')}  Agent: {s.get('agent_name')}  Status: {s.get('status')}")
    print(f"Trigger: {s.get('trigger')}  Model: {s.get('model') or '-'}  Messages: {s.get('message_count', 0)}")
    if s.get("parent_session_id"):
        print(f"Parent:  {s['parent_session_id']}")
    subs = resp.get("subsessions", [])
    if subs:
        print(f"\nSubsessions ({len(subs)}):")
        _print_sessions_table(subs)


def cmd_create_agent(args: argparse.Namespace) -> None:
    body: dict = {
        "name": args.name,
        "display_name": args.display_name or "",
        "description": args.description,
        "default_repo": args.default_repo,
        "max_turns": args.max_turns,
        "max_budget_usd": args.max_budget_usd,
        "timeout_s": args.timeout_s,
        "executor_config": {"type": args.executor_type, "model": args.executor_model},
        "sandbox": {"enabled": not args.no_sandbox},
    }
    if args.tool_permissions:
        body["tool_permissions"] = json.loads(args.tool_permissions)
    if args.allowed_subagents:
        body["allowed_subagents"] = args.allowed_subagents.split(",")
    if args.allowed_gateways:
        body["allowed_gateways"] = args.allowed_gateways.split(",")
    if args.role_md:
        body["role_md"] = Path(args.role_md).read_text()
    if args.soul_md:
        body["soul_md"] = Path(args.soul_md).read_text()

    resp = _request("POST", "/agent/create", body)
    print(f"Agent: {resp.get('name', '?')}")
    print(f"Status: {resp.get('status', '?')}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ahscli", description="CLI for Agent Harness Service")
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a session")
    p_create.add_argument("message", nargs="?", default=None, help="Optional first message")
    p_create.add_argument("--agent", default="sre", help="Agent name (default: sre)")
    p_create.add_argument("--session-id", default=None, help="External session ID to resume")
    p_create.add_argument("--user-id", default=None, help="Yupp user ID (saved to /tmp for reuse)")

    # message
    p_msg = sub.add_parser("message", help="Send a message")
    p_msg.add_argument("message", help="Message text")
    p_msg.add_argument("--session-id", default=None, help="Session ID (default: last created)")
    p_msg.add_argument("--user-id", default=None, help="Yupp user ID (default: last used)")

    # history
    p_hist = sub.add_parser("history", help="Show message history")
    p_hist.add_argument("--session-id", default=None, help="Session ID (default: last created)")
    p_hist.add_argument("--limit", type=int, default=50, help="Max messages to return (default: 50)")
    p_hist.add_argument("--offset", type=int, default=0, help="Offset for pagination (default: 0)")

    # feedback
    p_fb = sub.add_parser("feedback", help="Send feedback")
    p_fb.add_argument("rating", type=int, choices=[0, 1], help="1=positive, 0=negative")
    p_fb.add_argument("--session-id", default=None, help="Session ID (default: last created)")
    p_fb.add_argument("--message-id", default=None, help="Specific message ID for feedback")

    # agents
    p_agents = sub.add_parser("agents", help="List all configured agents")
    p_agents.add_argument("--descriptions", action="store_true", help="Show agent descriptions")

    # agent (single agent detail)
    p_agent = sub.add_parser("agent", help="Show single agent details")
    p_agent.add_argument("name", help="Agent name")
    p_agent.add_argument("--prompts", action="store_true", help="Include system prompt contents")

    # create-agent
    p_ca = sub.add_parser("create-agent", help="Create a new agent")
    p_ca.add_argument("name", help="Agent name (lowercase, alphanumeric + hyphens)")
    p_ca.add_argument("--display-name", default=None, help="Human-readable display name")
    p_ca.add_argument("--description", default=None, help="Agent description")
    p_ca.add_argument("--executor-type", default="harnessed", choices=["harnessed", "raw"], help="Executor type")
    p_ca.add_argument("--executor-model", default=None, help="Model (CLI name or provider/model_id)")
    p_ca.add_argument("--tool-permissions", default=None, help='JSON string, e.g. \'{"*": "allow"}\'')
    p_ca.add_argument("--allowed-subagents", default=None, help="Comma-separated agent names")
    p_ca.add_argument("--default-repo", default="yupp-agent", help="Default repo (default: yupp-agent)")
    p_ca.add_argument("--max-turns", type=int, default=20, help="Max turns (default: 20)")
    p_ca.add_argument("--max-budget-usd", type=float, default=2.0, help="Max budget USD (default: 2.0)")
    p_ca.add_argument("--timeout-s", type=int, default=300, help="Timeout seconds (default: 300)")
    p_ca.add_argument("--no-sandbox", action="store_true", help="Disable sandbox")
    p_ca.add_argument("--allowed-gateways", default=None, help="Comma-separated gateways (default: *)")
    p_ca.add_argument("--role-md", default=None, help="Path to ROLE.md file (role, expertise, and personality)")
    p_ca.add_argument("--soul-md", default=None, help="Deprecated — personality is now merged into ROLE.md")

    # sessions
    p_sess = sub.add_parser("sessions", help="List sessions with filters")
    p_sess.add_argument("--status", default=None, help="Filter: active, completed, stale")
    p_sess.add_argument("--agent", default=None, help="Filter by agent name")
    p_sess.add_argument("--trigger", default=None, help="Filter: slack, webhook, cron, api")
    p_sess.add_argument("--since", default=None, help="Created after (ISO-8601 datetime)")
    p_sess.add_argument("--until", default=None, help="Created before (ISO-8601 datetime)")
    p_sess.add_argument(
        "--include-subsessions", action="store_true", help="Include subagent sessions (default: root only)"
    )
    p_sess.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    p_sess.add_argument("--offset", type=int, default=0, help="Offset for pagination (default: 0)")

    # session (single session detail)
    p_sd = sub.add_parser("session", help="Show single session with subsessions")
    p_sd.add_argument("--session-id", default=None, help="Session ID (default: last created)")

    args = parser.parse_args()

    commands = {
        "create": cmd_create,
        "message": cmd_message,
        "history": cmd_history,
        "feedback": cmd_feedback,
        "agents": cmd_agents,
        "agent": cmd_agent,
        "create-agent": cmd_create_agent,
        "sessions": cmd_sessions,
        "session": cmd_session,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()

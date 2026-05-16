"use client";

// SessionChat — client component that:
//   1. Shows the initial server-rendered history (passed as a prop).
//   2. Connects to /api/ahs/session/{id}/ws (proxied via next.config rewrite)
//      and appends new turns, tool events, and stop_ack frames as they arrive.
//   3. Sends user messages over POST /api/web/sessions/{id}/message.
//   4. Sends stop over POST /api/web/sessions/{id}/stop.
//
// Frame shape comes from cmd/ahs/turn.go + ws.go. We treat unknown types as
// no-ops rather than guessing — this matches the "drop UI rather than invent"
// policy.

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { MessageHistoryItem, RecordedToolCall } from "@/lib/types";
import { truncate } from "@/lib/format";
import { ArtifactPill, parseArtifactResult } from "./ArtifactPill";
import type { ArtifactRef } from "./ArtifactPill";
import { ToolBlock } from "./ToolBlock";

interface ChatTurn {
  id: string;          // unique key
  role: "user" | "assistant" | "system";
  content: string;
  toolCalls: {
    name: string;
    args?: string;
    meta?: string;
    preview?: string;
    full?: string;
    artifact?: ArtifactRef;
  }[];
}

function fromHistory(items: MessageHistoryItem[]): ChatTurn[] {
  return items.map((m) => {
    const role = (m.role || "system") as ChatTurn["role"];
    const tools: ChatTurn["toolCalls"] = (m.tool_uses ?? []).map((ev: RecordedToolCall) => ({
      name: ev.name,
      args: ev.input ? truncate(JSON.stringify(ev.input), 80) : undefined,
      preview: ev.output ? truncate(ev.output, 120) : undefined,
      full: ev.output || undefined,
      meta: ev.is_error ? "ERROR" : undefined,
      artifact: parseArtifactResult(ev.name, ev.input, ev.output),
    }));
    return {
      id: m.message_id,
      role,
      content: m.content || "",
      toolCalls: tools,
    };
  });
}

export function SessionChat({
  sessionId,
  initialHistory,
  agentName,
  initialStatus,
}: {
  sessionId: string;
  initialHistory: MessageHistoryItem[];
  agentName: string;
  initialStatus: string;
}) {
  const [turns, setTurns] = useState<ChatTurn[]>(() => fromHistory(initialHistory));
  const [streaming, setStreaming] = useState(false);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [status, setStatus] = useState(initialStatus);
  const wsRef = useRef<WebSocket | null>(null);
  const liveAssistantRef = useRef<ChatTurn | null>(null);

  useEffect(() => {
    // WS upgrade is forwarded to AHS by the beforeFiles rewrite in
    // next.config.mjs, which appends api_key from server-side env.
    // Browser doesn't carry the key.
    const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/api/ahs/session/${sessionId}/ws`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => setErr(null);
    ws.onerror = () => setErr("WebSocket error — live updates paused.");
    ws.onclose = () => {
      wsRef.current = null;
    };
    ws.onmessage = (ev) => {
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      handleFrame(msg);
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Frame shapes verified against cmd/ahs/turn.go + ws.go:
  //   ws.go:82                         {type:"thread/started", session_id}
  //   turn.go:48-50                    {type:"turn/started"}
  //   turn.go:52-55                    {type:"item/started", item:{type:"agent_message"}}
  //   turn.go:135-138 / 238-241        {type:"item/agentMessage/delta", delta}
  //   turn.go:156-162                  {type:"item/started", item:{type:"mcp_tool_call", name, input}}
  //   turn.go:179-187                  {type:"item/completed", item:{type:"mcp_tool_call", name, output, is_error}}
  //   turn.go:243-247                  {type:"item/completed", item:{type:"agent_message"}}
  //   turn.go:198-201 / 250-253        {type:"turn/completed", status}
  //   ws.go:124-129                    {type:"stop_ack", status, cancelled}
  //   ws.go:103 / 117 / 135            {type:"error", message}
  function handleFrame(msg: Record<string, unknown>) {
    const t = String(msg.type ?? "");
    const item = (msg.item as Record<string, unknown> | undefined) ?? undefined;
    const itemType = item ? String(item.type ?? "") : "";

    switch (t) {
      case "thread/started":
        return;

      case "turn/started":
        setStreaming(true);
        return;

      case "item/started": {
        if (itemType === "agent_message") {
          // New assistant turn begins — open an empty bubble we'll append deltas to.
          const id = `live-${Date.now()}`;
          const turn: ChatTurn = { id, role: "assistant", content: "", toolCalls: [] };
          liveAssistantRef.current = turn;
          setTurns((prev) => [...prev, turn]);
          return;
        }
        if (itemType === "mcp_tool_call") {
          const live = liveAssistantRef.current;
          if (!live) return;
          const name = String(item?.name ?? "tool");
          const input = item?.input;
          live.toolCalls.push({
            name,
            args: input !== undefined ? truncate(JSON.stringify(input), 80) : undefined,
          });
          setTurns((prev) => prev.map((p) => (p.id === live.id ? { ...live } : p)));
          return;
        }
        return;
      }

      case "item/agentMessage/delta": {
        const delta = String(msg.delta ?? "");
        if (!delta) return;
        const live = liveAssistantRef.current;
        if (!live) {
          // Defensive: if a delta arrives before item/started:agent_message, open a bubble.
          const id = `live-${Date.now()}`;
          const turn: ChatTurn = { id, role: "assistant", content: delta, toolCalls: [] };
          liveAssistantRef.current = turn;
          setTurns((prev) => [...prev, turn]);
          return;
        }
        live.content += delta;
        setTurns((prev) => prev.map((p) => (p.id === live.id ? { ...live } : p)));
        return;
      }

      case "item/completed": {
        if (itemType === "mcp_tool_call") {
          const live = liveAssistantRef.current;
          if (!live) return;
          const name = String(item?.name ?? "");
          // Match by tool name; tools complete in order, so the most recent
          // unfilled call with this name is the right one.
          const target = [...live.toolCalls].reverse().find((tc) => tc.name === name && !tc.preview);
          const output = item?.output;
          if (target && output !== undefined) {
            const outStr = typeof output === "string" ? output : JSON.stringify(output, null, 2);
            target.preview = truncate(outStr, 120);
            target.full = outStr;
            if (item?.is_error) target.meta = "ERROR";
            // Heuristic: if this is an artifact-mutating tool, parse the
            // JSON output for {artifact_id, slug, title} so we can render
            // an inline link card.
            target.artifact = parseArtifactResult(name, undefined, outStr);
          }
          setTurns((prev) => prev.map((p) => (p.id === live.id ? { ...live } : p)));
          return;
        }
        if (itemType === "agent_message") {
          // Assistant message finished. Keep the bubble; turn/completed comes next.
          return;
        }
        return;
      }

      case "turn/completed":
      case "stop_ack": {
        setStreaming(false);
        liveAssistantRef.current = null;
        const newStatus = (msg.status as string) ?? "";
        if (newStatus) setStatus(newStatus);
        return;
      }

      case "error":
        setErr(String(msg.message ?? "unknown error"));
        setStreaming(false);
        liveAssistantRef.current = null;
        return;

      default:
        return;
    }
  }

  async function send() {
    const message = text.trim();
    if (!message) return;
    setErr(null);
    const optimistic: ChatTurn = {
      id: `user-${Date.now()}`,
      role: "user",
      content: message,
      toolCalls: [],
    };
    setTurns((prev) => [...prev, optimistic]);
    setText("");
    try {
      const res = await fetch(`/api/web/sessions/${sessionId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { error?: string };
        setErr(j.error || `HTTP ${res.status}`);
      }
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function stop() {
    try {
      await fetch(`/api/web/sessions/${sessionId}/stop`, { method: "POST" });
    } catch {
      /* ignore — UI will reflect stop_ack via WS */
    }
  }

  const showSpinner = streaming;
  const swatch = useMemo(() => (agentName.charAt(0) || "?").toUpperCase(), [agentName]);

  // Auto-scroll. Own dedicated scroll container (chat-scroll) so the
  // composer can sit underneath as a separate flex row; we drive
  // .scrollTo on the container directly because sentinel scrollIntoView
  // through nested overflow is flaky.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const initialPaint = useRef(true);
  const turnsFingerprint = JSON.stringify(
    turns.map((t) => [t.id, t.content.length, t.toolCalls.length]),
  );
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const behavior: ScrollBehavior = initialPaint.current ? "instant" : "smooth";
    initialPaint.current = false;
    requestAnimationFrame(() => {
      el.scrollTo({ top: el.scrollHeight, behavior });
    });
  }, [turnsFingerprint, streaming]);

  return (
    <div className="main-body chat-shell" style={{ padding: 0, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div ref={scrollRef} className="chat-scroll" style={{ flex: 1, overflowY: "auto", padding: "14px 28px 0" }}>
        <div className="chat-controls">
          <span className="hint-xs">
            session <code>{sessionId.slice(0, 8)}…</code> · {turns.length} turns · status <strong>{status}</strong>
          </span>
        </div>

        <div className="chat">
          {turns.length === 0 && (
            <div className="bubble system">No history yet — start the conversation below.</div>
          )}
          {(() => {
            // Each user turn gets a "Turn N" label rendered on the right.
            // Assistant bubbles need no role label — the layout already
            // distinguishes left assistant / right user.
            let userIdx = 0;
            return turns.flatMap((t) => {
              const out: React.ReactNode[] = [];
              if (t.role === "user") {
                userIdx += 1;
                out.push(
                  <div key={`${t.id}-label`} className="turn-num turn-num-right">
                    Turn {userIdx}
                  </div>,
                );
              }
              out.push(
                <div key={`${t.id}-body`} className={`bubble ${t.role}`}>
                  {t.role === "assistant" ? (
                    t.content ? (
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{t.content}</ReactMarkdown>
                    ) : (
                      "…"
                    )
                  ) : (
                    t.content
                  )}
                </div>,
              );
              t.toolCalls.forEach((tc, i) => {
                out.push(<ToolBlock key={`${t.id}-tool-${i}`} event={tc} />);
                if (tc.artifact) {
                  out.push(<ArtifactPill key={`${t.id}-art-${i}`} a={tc.artifact} />);
                }
              });
              return out;
            });
          })()}
          {showSpinner && (
            <div className="typing-bubble">
              <span className="spinner"></span>
              <span>{agentName} is thinking…</span>
            </div>
          )}
        </div>
      </div>

      <div className="composer-dock" style={{ padding: "12px 28px 18px", marginTop: 0 }}>
          <div className="composer">
            <textarea
              placeholder="Reply…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                // Enter sends; Shift+Enter / Cmd+Enter / Ctrl+Enter inserts a newline.
                if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <div className="composer-foot">
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--ink-soft)", fontSize: 12 }}>
                <span style={{ background: "var(--accent)", color: "#fff", width: 14, height: 14, borderRadius: 3, display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 10 }}>
                  {swatch}
                </span>
                {agentName}
              </span>
              <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
                {err && <span className="error-inline">{err}</span>}
                {streaming && (
                  <button type="button" className="stop" onClick={() => void stop()}>
                    ■ Stop
                  </button>
                )}
                <button type="button" className="send" onClick={() => void send()} disabled={!text.trim()}>
                  Send →
                </button>
              </span>
            </div>
          </div>
        </div>
    </div>
  );
}

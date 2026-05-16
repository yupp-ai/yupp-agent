"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

export function Composer({
  agentName,
  agentChoices,
}: {
  agentName: string;
  agentChoices: { name: string; display_name: string }[];
}) {
  const [text, setText] = useState("");
  const [agent, setAgent] = useState(agentName);
  const [pending, start] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();
  const swatch = agent.charAt(0).toUpperCase();

  function submit() {
    const message = text.trim();
    if (!message) return;
    setErr(null);
    start(async () => {
      try {
        const res = await fetch("/api/web/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_name: agent, message, trigger: "WEB" }),
        });
        const j = (await res.json()) as { session_id?: string; error?: string };
        if (!res.ok || !j.session_id) {
          setErr(j.error || `HTTP ${res.status}`);
          return;
        }
        router.push(`/sessions/${j.session_id}`);
        router.refresh();
      } catch (e) {
        setErr((e as Error).message);
      }
    });
  }

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        placeholder="Ask the agent anything…"
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={pending}
        onKeyDown={(e) => {
          // Enter sends; Shift+Enter / Cmd+Enter / Ctrl+Enter inserts a newline.
          if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="composer-foot">
        <label className="agent-pick" style={{ paddingLeft: 6 }}>
          <span className="swatch">{swatch}</span>
          <select
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            disabled={pending}
            style={{ border: "none", background: "transparent", outline: "none", font: "inherit" }}
          >
            {agentChoices.map((a) => (
              <option key={a.name} value={a.name}>
                {a.display_name || a.name}
              </option>
            ))}
          </select>
        </label>
        <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          {err && <span className="error-inline">{err}</span>}
          <button type="submit" className="send" disabled={pending || !text.trim()}>
            {pending ? "Starting…" : "Send →"}
          </button>
        </span>
      </div>
    </form>
  );
}

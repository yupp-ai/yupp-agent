"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

export function NewTaskForm({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("NORMAL");
  const [agent, setAgent] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!title.trim()) {
      setErr("title required");
      return;
    }
    start(async () => {
      const res = await fetch("/api/web/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          title: title.trim(),
          description: description.trim() || undefined,
          priority,
          agent_name: agent.trim() || undefined,
        }),
      });
      const j = (await res.json().catch(() => ({}))) as { error?: string };
      if (!res.ok) {
        setErr(j.error || `HTTP ${res.status}`);
        return;
      }
      setTitle("");
      setDescription("");
      onClose();
      router.refresh();
    });
  }

  return (
    <form
      onSubmit={submit}
      style={{
        border: "1px solid var(--rule)",
        background: "#fff",
        padding: 14,
        borderRadius: 6,
        margin: "10px 0 18px",
        maxWidth: 720,
      }}
    >
      <h3 style={{ marginTop: 0 }}>New task</h3>
      <table className="t">
        <tbody>
          <tr>
            <th style={{ width: 120 }}>Title</th>
            <td>
              <input className="form-input" value={title} onChange={(e) => setTitle(e.target.value)} disabled={pending} />
            </td>
          </tr>
          <tr>
            <th>Description</th>
            <td>
              <textarea className="form-input" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} disabled={pending} />
            </td>
          </tr>
          <tr>
            <th>Priority</th>
            <td>
              <select className="form-input" value={priority} onChange={(e) => setPriority(e.target.value)} disabled={pending}>
                <option>URGENT</option>
                <option>HIGH</option>
                <option>NORMAL</option>
                <option>LOW</option>
              </select>
            </td>
          </tr>
          <tr>
            <th>Agent (optional)</th>
            <td>
              <input className="form-input" placeholder="agent name, e.g. code-reviewer" value={agent} onChange={(e) => setAgent(e.target.value)} disabled={pending} />
            </td>
          </tr>
        </tbody>
      </table>
      <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
        <button type="submit" className="top-act primary" disabled={pending}>
          {pending ? "Creating…" : "Create task"}
        </button>
        <button type="button" className="top-act" onClick={onClose} disabled={pending}>
          Cancel
        </button>
        {err && <span className="error-inline" style={{ alignSelf: "center" }}>{err}</span>}
      </div>
    </form>
  );
}

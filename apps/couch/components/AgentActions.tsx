"use client";

import { useTransition, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import type { AgentDetailResponse } from "@/lib/types";

export function AgentActions({ detail }: { detail: AgentDetailResponse }) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const name = detail.agent.name;

  function downloadJSON() {
    const blob = new Blob([JSON.stringify(detail, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function archive() {
    if (!window.confirm(`Archive agent "${name}"? This is a soft-delete; the row is hidden from the UI.`)) return;
    setErr(null);
    start(async () => {
      const res = await fetch("/api/web/agents", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const j = (await res.json().catch(() => ({}))) as { error?: string };
      if (!res.ok) {
        setErr(j.error || `HTTP ${res.status}`);
        return;
      }
      router.push("/agents");
      router.refresh();
    });
  }

  return (
    <div className="title-actions">
      <Link href={`/agents/${encodeURIComponent(name)}/edit`} className="btn primary">
        ✎ Edit agent
      </Link>
      <Link href={`/agents/new?from=${encodeURIComponent(name)}`} className="btn">
        ⎘ Duplicate
      </Link>
      <button type="button" className="btn" onClick={downloadJSON}>
        ⤓ Export JSON
      </button>
      <button type="button" className="btn danger" onClick={archive} disabled={pending}>
        {pending ? "Archiving…" : "⊘ Archive"}
      </button>
      {err && <span className="error-inline" style={{ alignSelf: "center" }}>{err}</span>}
    </div>
  );
}

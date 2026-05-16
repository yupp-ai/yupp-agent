"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

const TRANSITIONS: { label: string; status: string; variant: string }[] = [
  { label: "✓ Complete", status: "COMPLETED", variant: "complete" },
  { label: "✕ Failed", status: "FAILED", variant: "fail" },
  { label: "↻ Restart", status: "__restart__", variant: "restart" },
  { label: "⏸ Pause", status: "BLOCKED", variant: "restart" },
];

export function TaskStatusButtons({ taskId }: { taskId: string }) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [err, setErr] = useState<string | null>(null);

  function action(status: string) {
    setErr(null);
    start(async () => {
      const url =
        status === "__restart__"
          ? `/api/web/tasks/${taskId}/restart`
          : `/api/web/tasks/${taskId}/status`;
      const body = status === "__restart__" ? null : JSON.stringify({ status });
      const res = await fetch(url, {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ?? undefined,
      });
      const j = (await res.json().catch(() => ({}))) as { error?: string };
      if (!res.ok) {
        setErr(j.error || `HTTP ${res.status}`);
        return;
      }
      router.refresh();
    });
  }

  return (
    <div className="task-action-row">
      {TRANSITIONS.map((t) => (
        <button
          key={t.label}
          className={`action-btn ${t.variant}`}
          onClick={() => action(t.status)}
          disabled={pending}
        >
          {t.label}
        </button>
      ))}
      {err && <span className="error-inline" style={{ alignSelf: "center" }}>{err}</span>}
    </div>
  );
}

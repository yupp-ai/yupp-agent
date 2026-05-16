"use client";

import { useTransition, useState } from "react";
import { useRouter } from "next/navigation";

export function ScheduleActions({ scheduleId }: { scheduleId: string }) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [err, setErr] = useState<string | null>(null);

  function trigger() {
    setErr(null);
    start(async () => {
      const res = await fetch(`/api/web/schedules/${scheduleId}/trigger`, { method: "POST" });
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { error?: string };
        setErr(j.error || `HTTP ${res.status}`);
      } else {
        router.refresh();
      }
    });
  }

  function del() {
    if (!window.confirm(`Delete schedule ${scheduleId}? This is a soft-delete.`)) return;
    setErr(null);
    start(async () => {
      const res = await fetch(`/api/web/schedules/${scheduleId}`, { method: "DELETE" });
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { error?: string };
        setErr(j.error || `HTTP ${res.status}`);
      } else {
        router.push("/schedules");
        router.refresh();
      }
    });
  }

  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      {err && <span style={{ color: "var(--error)", fontSize: 11 }}>{err}</span>}
      <button className="top-act" onClick={trigger} disabled={pending}>
        {pending ? "…" : "trigger now"}
      </button>
      <button
        className="top-act"
        onClick={del}
        disabled={pending}
        style={{ color: "var(--error)", borderColor: "var(--error)" }}
      >
        delete
      </button>
    </span>
  );
}

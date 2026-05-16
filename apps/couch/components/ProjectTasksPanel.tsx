"use client";

import { useState } from "react";
import Link from "next/link";
import { StatusPill } from "./StatusPill";
import { NewTaskForm } from "./NewTaskForm";
import type { TaskInfo } from "@/lib/types";
import { fmtRelative, fmtUsd } from "@/lib/format";

export function ProjectTasksPanel({
  projectId,
  initialTasks,
}: {
  projectId: string;
  initialTasks: TaskInfo[];
}) {
  const [creating, setCreating] = useState(false);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h2>Tasks <span className="hint" style={{ fontWeight: 400 }}>({initialTasks.length})</span></h2>
        {!creating && (
          <button className="top-act primary" onClick={() => setCreating(true)}>
            ＋ task
          </button>
        )}
      </div>

      {creating && <NewTaskForm projectId={projectId} onClose={() => setCreating(false)} />}

      {initialTasks.length === 0 ? (
        <p className="hint">No tasks yet — click + task to add one.</p>
      ) : (
        <table className="t task-table" style={{ tableLayout: "fixed", width: "100%" }}>
          <colgroup>
            <col style={{ width: 120 }} />
            <col style={{ width: 64 }} />
            <col />
            <col style={{ width: 80 }} />
            <col style={{ width: 130 }} />
            <col style={{ width: 100 }} />
            <col style={{ width: 80 }} />
          </colgroup>
          <thead>
            <tr>
              <th>Status</th>
              <th>Dep on</th>
              <th className="task-cell">Title</th>
              <th>Priority</th>
              <th>Agent</th>
              <th>Updated</th>
              <th>Spent</th>
            </tr>
          </thead>
          <tbody>
            {initialTasks.map((t) => (
              <tr key={t.task_id} className="row-clickable">
                <td><StatusPill status={t.status} /></td>
                <td>{depsLabel(t.depends_on)}</td>
                <td className="task-cell">
                  <Link href={`/projects/${projectId}/tasks/${t.task_id}`}>
                    <div className="task-title">{t.title}</div>
                    {t.description && <div className="task-desc">{t.description}</div>}
                  </Link>
                </td>
                <td>{t.priority}</td>
                <td>{t.agent_name ?? "—"}</td>
                <td>{fmtRelative(t.modified_at)}</td>
                <td>{fmtUsd(t.actual_spending_usd ?? undefined)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function depsLabel(deps: unknown): string {
  if (!deps) return "—";
  if (Array.isArray(deps)) {
    if (deps.length === 0) return "—";
    return deps.map((d) => (typeof d === "string" ? d.slice(0, 6) : String(d))).join(", ");
  }
  return "—";
}

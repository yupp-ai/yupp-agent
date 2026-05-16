import Link from "next/link";
import { notFound } from "next/navigation";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { StatusPill } from "@/components/StatusPill";
import { TaskStatusButtons } from "@/components/TaskStatusButtons";
import { TopBar } from "@/components/TopBar";
import { findProject, getTask } from "@/lib/ahs";
import { fmtRelative, fmtUsd } from "@/lib/format";

export default async function TaskDetailPage({
  params,
}: {
  params: Promise<{ id: string; taskId: string }>;
}) {
  const { id, taskId } = await params;
  const [projectRes, taskRes] = await Promise.all([findProject(id), getTask(taskId)]);
  if (!projectRes.ok) {
    if (projectRes.status === 404) notFound();
    return (
      <Shell active="/projects">
        <section className="main">
          <TopBar
            back={{ href: "/projects", label: "Projects" }}
            crumbs={[{ href: "/projects", label: "Projects" }, { label: id.slice(0, 8) }]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS find-project failed." body={projectRes.error} />
          </div>
        </section>
      </Shell>
    );
  }
  if (!taskRes.ok) {
    if (taskRes.status === 404) notFound();
    return (
      <Shell active="/projects">
        <section className="main">
          <TopBar
            back={{ href: `/projects/${id}`, label: projectRes.data.name }}
            crumbs={[
              { href: "/projects", label: "Projects" },
              { href: `/projects/${id}`, label: projectRes.data.name },
              { label: taskId.slice(0, 8) },
            ]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS get-task failed." body={taskRes.error} />
          </div>
        </section>
      </Shell>
    );
  }
  const p = projectRes.data;
  const t = taskRes.data;
  return (
    <Shell active="/projects">
      <section className="main">
        <TopBar
          back={{ href: `/projects/${p.project_id}`, label: p.name }}
          crumbs={[
            { href: "/projects", label: "Projects" },
            { href: `/projects/${p.project_id}`, label: p.name },
            { label: t.title },
          ]}
        />
        <div className="main-body wide">
          <h1>
            {t.title}{" "}
            <span className="pill-inline"><StatusPill status={t.status} /></span>
          </h1>

          <TaskStatusButtons taskId={t.task_id} />

          <div className="two-col" style={{ gridTemplateColumns: "460px 1fr" }}>
            <div>
              <h2 style={{ marginTop: 0 }}>Details</h2>
              <table className="t">
                <tbody>
                  <tr><th style={{ width: 160 }}>Status</th><td><StatusPill status={t.status} /></td></tr>
                  <tr><th>Priority</th><td>{t.priority}</td></tr>
                  <tr><th>Agent</th><td>{t.agent_name ? <Link href={`/agents/${encodeURIComponent(t.agent_name)}`}>{t.agent_name}</Link> : "—"}</td></tr>
                  <tr><th>Depends on</th><td>{depsValue(t.depends_on)}</td></tr>
                  <tr><th>Estimated effort</th><td>{t.estimated_effort ?? "—"}</td></tr>
                  <tr><th>Spent</th><td>{fmtUsd(t.actual_spending_usd ?? undefined)}</td></tr>
                  <tr><th>Created</th><td>{fmtRelative(t.created_at)}</td></tr>
                  <tr><th>Modified</th><td>{fmtRelative(t.modified_at)}</td></tr>
                  <tr><th>Completed</th><td>{fmtRelative(t.completed_at)}</td></tr>
                  <tr><th>Task ID</th><td><code style={{ fontSize: 11 }}>{t.task_id}</code></td></tr>
                </tbody>
              </table>
              {t.result !== undefined && t.result !== null && (
                <details style={{ marginTop: 18 }}>
                  <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 13 }}>
                    ▾ Result
                  </summary>
                  <pre style={{ background: "var(--bg-deep)", padding: "8px 10px", borderRadius: 4, fontSize: 12, marginTop: 6 }}>
                    {JSON.stringify(t.result, null, 2)}
                  </pre>
                </details>
              )}
            </div>

            <div>
              <h2 style={{ marginTop: 0 }}>Description</h2>
              <div style={{ fontFamily: "var(--font-serif)", fontSize: 15, lineHeight: 1.6 }}>
                {t.description ? <p>{t.description}</p> : <p className="hint">No description.</p>}
              </div>
            </div>
          </div>
        </div>
      </section>
    </Shell>
  );
}

function depsValue(deps: unknown): React.ReactNode {
  if (!deps) return "—";
  if (Array.isArray(deps)) {
    if (deps.length === 0) return "—";
    return deps.map((d, i) => <code key={i} style={{ marginRight: 6 }}>{String(d)}</code>);
  }
  return "—";
}

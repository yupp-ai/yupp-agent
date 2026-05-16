import { notFound } from "next/navigation";
import { Banner } from "@/components/Banner";
import { ProjectTasksPanel } from "@/components/ProjectTasksPanel";
import { Shell } from "@/components/Shell";
import { StatusPill } from "@/components/StatusPill";
import { TopBar } from "@/components/TopBar";
import { findProject, listProjectTasks } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

export default async function ProjectDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const r = await findProject(id);
  if (!r.ok) {
    if (r.status === 404) notFound();
    return (
      <Shell active="/projects">
        <section className="main">
          <TopBar
            back={{ href: "/projects", label: "Projects" }}
            crumbs={[{ href: "/projects", label: "Projects" }, { label: id.slice(0, 8) }]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS find-project failed." body={r.error} />
          </div>
        </section>
      </Shell>
    );
  }
  const p = r.data;
  const tasksRes = await listProjectTasks(p.project_id);
  const tasks = tasksRes.ok ? tasksRes.data.tasks : [];

  return (
    <Shell active="/projects">
      <section className="main">
        <TopBar
          back={{ href: "/projects", label: "Projects" }}
          crumbs={[{ href: "/projects", label: "Projects" }, { label: p.name }]}
          actions={<button className="top-act">edit</button>}
        />
        <div className="main-body wide">
          <h1>
            {p.name}{" "}
            <span className="pill-inline">
              <StatusPill status={p.status} />
            </span>
          </h1>
          {p.description && <p className="lede">{p.description}</p>}

          <table className="t" style={{ maxWidth: 720 }}>
            <tbody>
              <tr><th style={{ width: 160 }}>Project ID</th><td><code>{p.project_id}</code></td></tr>
              <tr><th>Created by</th><td><code>{p.creator_user_id ?? "—"}</code></td></tr>
              <tr><th>Created</th><td>{fmtRelative(p.created_at)}</td></tr>
            </tbody>
          </table>

          {!tasksRes.ok && (
            <Banner kind="warn" title="AHS list-tasks failed." body={tasksRes.error} />
          )}
          {tasksRes.ok && (
            <ProjectTasksPanel projectId={p.project_id} initialTasks={tasks} />
          )}
        </div>
      </section>
    </Shell>
  );
}

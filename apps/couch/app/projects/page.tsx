import Link from "next/link";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { StatusPill } from "@/components/StatusPill";
import { TopBar } from "@/components/TopBar";
import { listProjects } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

export default async function ProjectsPage() {
  const r = await listProjects({ limit: 100 });

  return (
    <Shell active="/projects">
      <section className="main">
        <TopBar
          crumbs={[{ label: "Projects" }]}
          actions={<button className="top-act primary">＋ project</button>}
        />
        <div className="main-body wide">
          <h1>Projects</h1>
          <p className="lede">
            Long-running, multi-task workstreams. Each project owns a default agent.
          </p>

          {!r.ok && <Banner kind="error" title="AHS list-projects failed." body={r.error} />}

          {r.ok && (
            <Banner
              kind="info"
              title="Tasks endpoint pending."
              body={
                <>
                  Project metadata is wired, but <code>cmd/ahs</code> does not yet expose
                  per-project task lists, the dependency graph, or shared state. The
                  &ldquo;at a glance&rdquo; section, dependency tab, and per-task pages will land
                  once those routes ship — they are intentionally absent here rather than
                  faked.
                </>
              }
            />
          )}

          {r.ok && r.data.projects.length === 0 && (
            <p className="hint">No projects yet.</p>
          )}

          {r.ok && r.data.projects.length > 0 && (
            <table
              className="t project-list-table"
              style={{ tableLayout: "fixed", width: "100%", marginTop: 14 }}
            >
              <colgroup>
                <col style={{ width: 110 }} />
                <col style={{ width: 280 }} />
                <col />
                <col style={{ width: 200 }} />
                <col style={{ width: 110 }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Project</th>
                  <th>Description</th>
                  <th>Created by</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {r.data.projects.map((p) => (
                  <tr key={p.project_id} className="row-clickable">
                    <td><StatusPill status={p.status} /></td>
                    <td>
                      <Link href={`/projects/${p.project_id}`} className="project-name">
                        {p.name}
                      </Link>
                    </td>
                    <td className="ellipsis">{p.description ?? "—"}</td>
                    <td>
                      <code style={{ fontSize: 11 }}>{p.creator_user_id ?? "—"}</code>
                    </td>
                    <td>{fmtRelative(p.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </Shell>
  );
}

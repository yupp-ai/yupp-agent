import Link from "next/link";
import { notFound } from "next/navigation";
import { Banner } from "@/components/Banner";
import { ScheduleActions } from "@/components/ScheduleActions";
import { Shell } from "@/components/Shell";
import { StatusPill } from "@/components/StatusPill";
import { TopBar } from "@/components/TopBar";
import { getSchedule, listScheduleRuns } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

export default async function ScheduleDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [detail, runs] = await Promise.all([getSchedule(id), listScheduleRuns(id, { limit: 50 })]);

  if (!detail.ok) {
    if (detail.status === 404) notFound();
    return (
      <Shell active="/schedules">
        <section className="main">
          <TopBar
            back={{ href: "/schedules", label: "Schedules" }}
            crumbs={[{ href: "/schedules", label: "Schedules" }, { label: id.slice(0, 8) }]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS get-schedule failed." body={detail.error} />
          </div>
        </section>
      </Shell>
    );
  }

  const s = detail.data;
  const runsList = runs.ok ? runs.data.runs : [];

  return (
    <Shell active="/schedules">
      <section className="main">
        <TopBar
          back={{ href: "/schedules", label: "Schedules" }}
          crumbs={[
            { href: "/schedules", label: "Schedules" },
            { label: s.name || `Schedule ${s.schedule_id.slice(0, 6)}…` },
          ]}
          actions={<ScheduleActions scheduleId={s.schedule_id} />}
        />
        <div className="main-body wide">
          <h1>
            {s.name || `Schedule ${s.schedule_id.slice(0, 6)}…`}{" "}
            <span className="pill-inline">
              <StatusPill status={s.status} />
            </span>
          </h1>
          {s.description && <p className="lede">{s.description}</p>}

          <div className="kpi-row">
            <div><b>{s.run_count}</b> total runs</div>
            <div><b>{runsList.filter((r) => r.status === "COMPLETED").length}</b> completed</div>
            <div><b>{runsList.filter((r) => r.status === "FAILED").length}</b> failed</div>
            {s.max_runs && <div>cap <b>{s.max_runs}</b></div>}
          </div>

          <h2>Configuration</h2>
          <table className="t" style={{ maxWidth: 720 }}>
            <tbody>
              <tr><th style={{ width: 200 }}>Schedule ID</th><td><code>{s.schedule_id}</code></td></tr>
              <tr><th>Type</th><td>{s.schedule_type}</td></tr>
              {s.cron_expression && <tr><th>Cron</th><td><code>{s.cron_expression}</code></td></tr>}
              <tr><th>Agent</th><td>{s.agent_name || "—"}</td></tr>
              {s.message && (
                <tr>
                  <th>Initial message</th>
                  <td style={{ fontFamily: "var(--font-serif)" }}>“{s.message}”</td>
                </tr>
              )}
              <tr><th>Next run</th><td>{fmtRelative(s.next_run_at)}</td></tr>
              <tr><th>Last run</th><td>{fmtRelative(s.last_run_at)}</td></tr>
            </tbody>
          </table>

          <h2>Recent runs</h2>
          {!runs.ok ? (
            <Banner kind="warn" title="AHS list-runs failed." body={runs.error} />
          ) : runsList.length === 0 ? (
            <p className="hint">No runs yet.</p>
          ) : (
            <table className="t">
              <thead>
                <tr>
                  <th>Run #</th>
                  <th>Started</th>
                  <th>Completed</th>
                  <th>Status</th>
                  <th>Session</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {runsList.map((r) => (
                  <tr key={r.run_id}>
                    <td>{r.run_number}</td>
                    <td>{fmtRelative(r.started_at)}</td>
                    <td>{fmtRelative(r.completed_at)}</td>
                    <td><StatusPill status={r.status} /></td>
                    <td>
                      {r.session_id ? (
                        <Link href={`/sessions/${r.session_id}`}>
                          {r.session_id.slice(0, 8)}…
                        </Link>
                      ) : "—"}
                    </td>
                    <td style={{ color: "var(--error)", fontSize: 11.5 }}>
                      {r.error ? r.error.slice(0, 80) : "—"}
                    </td>
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

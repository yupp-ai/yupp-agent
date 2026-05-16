import Link from "next/link";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { StatusPill } from "@/components/StatusPill";
import { TopBar } from "@/components/TopBar";
import { listSchedules } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

export default async function SchedulesPage() {
  const r = await listSchedules();

  return (
    <Shell active="/schedules">
      <section className="main">
        <TopBar
          crumbs={[{ label: "Schedules" }]}
          actions={
            <>
              <button className="top-act">＋ one-shot</button>
              <button className="top-act primary">＋ recurring</button>
            </>
          }
        />
        <div className="main-body wide">
          <h1>Schedules</h1>
          <p className="lede">
            Cron-driven and one-shot agent runs. The scheduler claim loop fires due rows.
          </p>

          {!r.ok && <Banner kind="error" title="AHS list-schedules failed." body={r.error} />}

          {r.ok && (() => {
            const all = r.data.schedules;
            const recurring = all.filter((s) => s.schedule_type === "RECURRING");
            const oneShot = all.filter((s) => s.schedule_type === "SCHEDULED");

            return (
              <>
                <div className="split-section">
                  <h2>Recurring</h2>
                  <span className="count">{recurring.length} schedules · cron-driven</span>
                </div>
                {recurring.length === 0 ? (
                  <p className="hint">No recurring schedules.</p>
                ) : (
                  <table className="t">
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Agent</th>
                        <th>Status</th>
                        <th>Cron</th>
                        <th>Next run</th>
                        <th>Last run</th>
                        <th>Runs</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recurring.map((s) => (
                        <tr key={s.schedule_id} className="row-clickable">
                          <td>
                            <Link href={`/schedules/${s.schedule_id}`}>
                              {s.name || `Schedule ${s.schedule_id.slice(0, 6)}…`}
                            </Link>
                            {s.message && (
                              <div className="hint-xs">{s.message}</div>
                            )}
                          </td>
                          <td>{s.agent_name || "—"}</td>
                          <td><StatusPill status={s.status} /></td>
                          <td><code>{s.cron_expression ?? "—"}</code></td>
                          <td>{fmtRelative(s.next_run_at)}</td>
                          <td>{fmtRelative(s.last_run_at)}</td>
                          <td>{s.run_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                <div className="split-section" style={{ marginTop: 32 }}>
                  <h2>Scheduled (one-shot)</h2>
                  <span className="count">
                    {oneShot.length} schedules · fire once at a fixed time
                  </span>
                </div>
                {oneShot.length === 0 ? (
                  <p className="hint">No one-shot schedules.</p>
                ) : (
                  <table className="t">
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Agent</th>
                        <th>Status</th>
                        <th>Fire at</th>
                        <th>Last run</th>
                      </tr>
                    </thead>
                    <tbody>
                      {oneShot.map((s) => (
                        <tr key={s.schedule_id} className="row-clickable">
                          <td>
                            <Link href={`/schedules/${s.schedule_id}`}>
                              {s.name || `Schedule ${s.schedule_id.slice(0, 6)}…`}
                            </Link>
                            {s.message && (
                              <div className="hint-xs">{s.message}</div>
                            )}
                          </td>
                          <td>{s.agent_name || "—"}</td>
                          <td><StatusPill status={s.status} /></td>
                          <td>{fmtRelative(s.next_run_at)}</td>
                          <td>{fmtRelative(s.last_run_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            );
          })()}
        </div>
      </section>
    </Shell>
  );
}

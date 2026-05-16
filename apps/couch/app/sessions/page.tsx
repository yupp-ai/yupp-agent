import Link from "next/link";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { StatusPill } from "@/components/StatusPill";
import { Tpill } from "@/components/Tpill";
import { TopBar } from "@/components/TopBar";
import { listAgents, listSessions } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

export default async function SessionsPage({
  searchParams,
}: {
  searchParams?: Promise<{ q?: string; status?: string; agent_name?: string }>;
}) {
  const params = (await searchParams) ?? {};
  const [r, agentsR] = await Promise.all([
    listSessions({
      q: params.q,
      status: params.status,
      agent_name: params.agent_name,
      limit: 100,
    }),
    listAgents(),
  ]);
  const agentChoices = agentsR.ok ? agentsR.data.agents : [];

  return (
    <Shell active="/sessions">
      <section className="main">
        <TopBar
          crumbs={[{ label: "Sessions" }]}
          meta={r.ok ? `${r.data.total} total` : undefined}
        />
        <div className="main-body wide">
          <h1>Sessions</h1>
          <p className="lede">
            Every conversation, regardless of trigger. Search any keyword to match in titles + message bodies.
          </p>

          {!r.ok && <Banner kind="error" title="AHS list-sessions failed." body={r.error} />}

          <form className="filters" id="session-filters">
            <input
              type="search"
              name="q"
              defaultValue={params.q ?? ""}
              placeholder="Search title or message content…"
              autoFocus={!params.q ? false : true}
              style={{ width: 340 }}
            />
            <select name="agent_name" defaultValue={params.agent_name ?? ""}>
              <option value="">any agent</option>
              {agentChoices.map((a) => (
                <option key={a.name} value={a.name}>{a.name}</option>
              ))}
            </select>
            <select name="status" defaultValue={params.status ?? ""}>
              <option value="">any status</option>
              <option value="ACTIVE">ACTIVE</option>
              <option value="IN_PROGRESS">IN_PROGRESS</option>
              <option value="COMPLETED">COMPLETED</option>
              <option value="FAILED">FAILED</option>
              <option value="CANCELLED">CANCELLED</option>
            </select>
            <button className="top-act primary" type="submit">Apply</button>
            {(params.q || params.status || params.agent_name) && (
              <Link href="/sessions" className="top-act">Clear</Link>
            )}
          </form>

          {r.ok && (
            r.data.sessions.length === 0 ? (
              <p className="hint">No sessions match.</p>
            ) : (
              <table className="t">
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Trigger</th>
                    <th>Agent</th>
                    <th>Started</th>
                    <th>Status</th>
                    <th>Turns</th>
                    <th>Model</th>
                  </tr>
                </thead>
                <tbody>
                  {r.data.sessions.map((s) => (
                    <tr key={s.session_id} className="row-clickable">
                      <td>
                        <Link href={`/sessions/${s.session_id}`}>
                          {s.title || `Session ${s.session_id.slice(0, 6)}…`}
                        </Link>
                      </td>
                      <td><Tpill trigger={s.trigger} variant="wide" /></td>
                      <td>{s.agent_name}</td>
                      <td>{fmtRelative(s.created_at)}</td>
                      <td><StatusPill status={s.status} /></td>
                      <td>{s.message_count}</td>
                      <td><code style={{ fontSize: 11 }}>{s.model ?? "—"}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </section>
    </Shell>
  );
}

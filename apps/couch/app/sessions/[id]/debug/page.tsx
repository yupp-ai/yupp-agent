import Link from "next/link";
import { notFound } from "next/navigation";
import { Banner } from "@/components/Banner";
import { DebugEventTable } from "@/components/DebugEventTable";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { getSessionDebug } from "@/lib/ahs";
import { fmtDurMs, fmtUsdFull } from "@/lib/format";

export default async function SessionDebugPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const r = await getSessionDebug(id);
  if (!r.ok) {
    if (r.status === 404) notFound();
    return (
      <Shell active="/sessions" activeSessionId={id}>
        <section className="main">
          <TopBar
            back={{ href: `/sessions/${id}`, label: "Chat" }}
            crumbs={[
              { href: "/sessions", label: "Sessions" },
              { href: `/sessions/${id}`, label: id.slice(0, 8) },
              { label: "debug" },
            ]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS get-session-debug failed." body={r.error} />
          </div>
        </section>
      </Shell>
    );
  }

  const { session, messages, totals } = r.data;
  const startMs = messages.length ? new Date(messages[0].created_at).getTime() : 0;

  return (
    <Shell active="/sessions" activeSessionId={id}>
      <section className="main">
        <TopBar
          back={{ href: `/sessions/${id}`, label: "Chat" }}
          crumbs={[
            { href: "/sessions", label: "Sessions" },
            { href: `/sessions/${id}`, label: session.title || id.slice(0, 8) },
            { label: "debug" },
          ]}
          actions={
            <Link className="top-act" href={`/sessions/${id}`}>
              ← back to chat
            </Link>
          }
        />
        <div className="main-body wide" style={{ paddingTop: 12 }}>
          <div className="debug-summary">
            <div>
              <div className="hint-xs">status</div>
              <div className="big">{session.status}</div>
            </div>
            <div>
              <div className="hint-xs">agent · trigger</div>
              <div className="big">
                {session.agent_name} · {session.trigger}
              </div>
            </div>
            <div>
              <div className="hint-xs">model</div>
              <div className="big mono">{session.model || "—"}</div>
            </div>
            <div>
              <div className="hint-xs">turns</div>
              <div className="big">
                {totals.user_messages}u · {totals.agent_messages}a
              </div>
            </div>
            <div>
              <div className="hint-xs">tool calls</div>
              <div className="big">{totals.tool_calls}</div>
            </div>
            <div>
              <div className="hint-xs">total cost</div>
              <div className="big">{fmtUsdFull(totals.total_cost_usd)}</div>
            </div>
            <div>
              <div className="hint-xs">total duration</div>
              <div className="big">{fmtDurMs(totals.total_duration_ms)}</div>
            </div>
            <div>
              <div className="hint-xs">failed turns</div>
              <div className="big" style={{ color: totals.failed_turns ? "var(--err)" : undefined }}>
                {totals.failed_turns}
              </div>
            </div>
            <div>
              <div className="hint-xs">created</div>
              <div className="mono small">{session.created_at}</div>
            </div>
            <div>
              <div className="hint-xs">last modified</div>
              <div className="mono small">{session.modified_at}</div>
            </div>
            {session.workspace && (
              <div className="debug-summary-wide">
                <div className="hint-xs">workspace</div>
                <div className="mono small">{session.workspace}</div>
              </div>
            )}
            {session.creator_user_id && (
              <div>
                <div className="hint-xs">creator</div>
                <div className="mono small">{session.creator_user_id}</div>
              </div>
            )}
            {session.parent_session_id && (
              <div>
                <div className="hint-xs">parent session</div>
                <Link href={`/sessions/${session.parent_session_id}`} className="mono small">
                  {session.parent_session_id.slice(0, 8)}…
                </Link>
              </div>
            )}
          </div>

          <DebugEventTable messages={messages} startMs={startMs} />
        </div>
      </section>
    </Shell>
  );
}

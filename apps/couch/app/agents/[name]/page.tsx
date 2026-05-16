import { notFound } from "next/navigation";
import { AgentActions } from "@/components/AgentActions";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { getAgent, listSessions } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

export default async function AgentDetailPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  const decoded = decodeURIComponent(name);
  const detail = await getAgent(decoded, { withPrompts: true });
  if (!detail.ok) {
    if (detail.status === 404) notFound();
    return (
      <Shell active="/agents">
        <section className="main">
          <TopBar
            back={{ href: "/agents", label: "Agents" }}
            crumbs={[{ href: "/agents", label: "Agents" }, { label: decoded }]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS get-agent failed." body={detail.error} />
          </div>
        </section>
      </Shell>
    );
  }
  const agent = detail.data.agent;
  const promptsMap = detail.data.system_prompts ?? {};
  const promptEntries = Object.entries(promptsMap);

  const sessionsRes = await listSessions({ agent_name: agent.name, limit: 25 });
  const sessions = sessionsRes.ok ? sessionsRes.data.sessions : [];

  // Pick out sys:role (the canonical "role prompt") so it renders prominently.
  const roleEntry = promptEntries.find(([k]) => /:role$/i.test(k));

  // Inline helper for the policy table — joins comma-separated string lists
  // into rendered <code> chips, or em-dash when the list is empty.
  function listOrDash(xs: string[] | undefined | null): React.ReactNode {
    if (!xs || xs.length === 0) return "—";
    return xs.map((x, i) => (
      <code key={i} style={{ marginRight: 6 }}>{x}</code>
    ));
  }

  return (
    <Shell active="/agents">
      <section className="main">
        <TopBar
          back={{ href: "/agents", label: "Agents" }}
          crumbs={[{ href: "/agents", label: "Agents" }, { label: agent.name }]}
        />
        <div className="main-body wide">
          <h1>
            {agent.name}
            {agent.display_name && agent.display_name !== agent.name && (
              <span
                style={{
                  fontFamily: "var(--font-serif)",
                  fontStyle: "italic",
                  color: "var(--ink-soft)",
                  fontSize: 18,
                  marginLeft: 8,
                }}
              >
                {agent.display_name}
              </span>
            )}
          </h1>
          {agent.description && <p className="lede">{agent.description}</p>}

          <AgentActions detail={detail.data} />

          {roleEntry && (
            <div className="role-box">
              <div className="role-label">ROLE prompt — <code>{roleEntry[0]}</code></div>
              {roleEntry[1]}
            </div>
          )}
          {!roleEntry && detail.data.additional_system_prompt && (
            <div className="role-box">
              <div className="role-label">ROLE prompt</div>
              {detail.data.additional_system_prompt}
            </div>
          )}

          <h2>Metadata</h2>
          <table className="t" style={{ maxWidth: 720 }}>
            <tbody>
              <tr><th style={{ width: 160 }}>Name</th><td><code>{agent.name}</code></td></tr>
              <tr><th>Display name</th><td>{agent.display_name}</td></tr>
              <tr><th>Executor</th><td>{agent.executor_type.toUpperCase()}</td></tr>
              <tr><th>Model</th><td>{agent.executor_model || agent.llm_model || "(default)"}</td></tr>
              {agent.creator_user_id && (
                <tr><th>Creator user_id</th><td><code>{agent.creator_user_id}</code></td></tr>
              )}
              <tr><th>Sandbox</th><td>{agent.sandbox_enabled ? "enabled" : "disabled"}</td></tr>
              <tr><th>Max turns</th><td>{agent.max_turns}</td></tr>
              <tr><th>Max budget</th><td>${agent.max_budget_usd.toFixed(2)}</td></tr>
              <tr><th>Timeout</th><td>{agent.timeout_s}s</td></tr>
            </tbody>
          </table>

          {detail.data.config && (
            <>
              <h2>Skill / tool policy</h2>
              <table className="t" style={{ maxWidth: 880 }}>
                <tbody>
                  <tr><th style={{ width: 160 }}>skills_allowed</th><td>{listOrDash(detail.data.config.skills_allowed)}</td></tr>
                  <tr><th>skills_denied</th><td>{listOrDash(detail.data.config.skills_denied)}</td></tr>
                  <tr><th>tools_allowed</th><td>{listOrDash(detail.data.config.tools_allowed)}</td></tr>
                  <tr><th>tools_denied</th><td>{listOrDash(detail.data.config.tools_denied)}</td></tr>
                  <tr><th>always_load</th><td>{listOrDash(detail.data.config.always_load)}</td></tr>
                </tbody>
              </table>
            </>
          )}

          <h2>Loaded system prompts</h2>
          {promptEntries.length === 0 ? (
            <p className="hint">
              No system prompts force-loaded for this agent.
            </p>
          ) : (
            <table className="t" style={{ maxWidth: 880 }}>
              <thead>
                <tr><th>Slug</th><th>Bytes</th><th>Preview</th></tr>
              </thead>
              <tbody>
                {promptEntries.map(([slug, body]) => (
                  <tr key={slug}>
                    <td><code>{slug}</code></td>
                    <td>{body.length}</td>
                    <td style={{ color: "var(--ink-soft)", fontSize: 12 }}>
                      {body.length > 120 ? `${body.slice(0, 120)}…` : body}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h2>Recent sessions</h2>
          {!sessionsRes.ok ? (
            <Banner kind="warn" title="Could not list sessions for this agent." body={sessionsRes.error} />
          ) : sessions.length === 0 ? (
            <p className="hint">No sessions yet.</p>
          ) : (
            <table className="t" style={{ maxWidth: 880 }}>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Trigger</th>
                  <th>Started</th>
                  <th>Status</th>
                  <th>Turns</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td>
                      <a href={`/sessions/${s.session_id}`}>
                        {s.title || `Session ${s.session_id.slice(0, 6)}…`}
                      </a>
                    </td>
                    <td>{s.trigger}</td>
                    <td>{fmtRelative(s.created_at)}</td>
                    <td>{s.status}</td>
                    <td>{s.message_count}</td>
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

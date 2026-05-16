import Link from "next/link";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { listAgents } from "@/lib/ahs";

export default async function AgentsPage() {
  const r = await listAgents();
  return (
    <Shell active="/agents">
      <section className="main">
        <TopBar
          crumbs={[{ label: "Agents" }]}
          actions={
            <Link href="/agents/new" className="top-act primary">
              ＋ agent
            </Link>
          }
        />
        <div className="main-body wide">
          <h1>Agents</h1>
          <p className="lede">
            Each agent is a configured persona — model, executor type, and policy. Click one to see its system prompts.
          </p>

          {!r.ok && (
            <Banner kind="error" title="AHS list-agents failed." body={r.error} />
          )}

          {r.ok && r.data.agents.length === 0 && (
            <Banner
              kind="warn"
              title="No agents registered."
              body={<>Use <code>plugctl agents push</code> from a seeds directory to register one.</>}
            />
          )}

          {r.ok && r.data.agents.length > 0 && (
            <div className="agent-grid">
              {r.data.agents.map((a) => (
                <Link key={a.name} href={`/agents/${encodeURIComponent(a.name)}`} className="agent-card">
                  <div className="slug">
                    {a.name}
                    {a.display_name && a.display_name !== a.name && (
                      <span
                        style={{
                          fontFamily: "var(--font-serif)",
                          fontStyle: "italic",
                          color: "var(--ink-soft)",
                          fontWeight: 400,
                          fontSize: 14,
                          marginLeft: 4,
                        }}
                      >
                        {a.display_name}
                      </span>
                    )}
                  </div>
                  <div className="desc">{a.description ?? "—"}</div>
                  <div className="meta">
                    <span>{a.executor_type}</span>
                    <span>·</span>
                    <span>{a.executor_model || a.llm_model || "(default model)"}</span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      </section>
    </Shell>
  );
}

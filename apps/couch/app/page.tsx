import { Banner } from "@/components/Banner";
import { Composer } from "@/components/Composer";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { listAgents } from "@/lib/ahs";

export default async function HomePage() {
  const r = await listAgents();
  const agents = r.ok ? r.data.agents : [];
  const defaultAgent = agents[0]?.name ?? "";

  return (
    <Shell active="/">
      <section className="main">
        <TopBar crumbs={[{ label: "Home" }]} />
        <div className="hero">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/logo.png" alt="" style={{ width: 64, height: 64, opacity: 0.95, marginBottom: 18 }} />
          <h1>What should we work on?</h1>
          <div className="sub">Pick an agent and start a session.</div>

          {!r.ok && (
            <Banner
              kind="error"
              title="AHS is not reachable."
              body={
                <>
                  Composer is disabled until <code>cmd/ahs</code> is running and{" "}
                  <code>AHS_BASE_URL</code> + <code>AHS_API_KEY</code> are set in{" "}
                  <code>web/.env.local</code>.
                </>
              }
            />
          )}

          {r.ok && agents.length === 0 && (
            <Banner
              kind="warn"
              title="No agents registered yet."
              body={
                <>
                  Seed at least one agent via <code>plugctl</code> (see{" "}
                  <code>seeds/examples/</code>) before starting a session.
                </>
              }
            />
          )}

          {r.ok && agents.length > 0 && (
            <Composer
              agentName={defaultAgent}
              agentChoices={agents.map((a) => ({ name: a.name, display_name: a.display_name }))}
            />
          )}

          {agents.length > 0 && (
            <div className="recent">
              Recent agents:
              {" "}
              {agents.slice(0, 6).map((a) => (
                <a key={a.name} href={`/agents/${encodeURIComponent(a.name)}`}>
                  {a.name}
                </a>
              ))}
            </div>
          )}
        </div>
      </section>
    </Shell>
  );
}

import { AgentForm } from "@/components/AgentForm";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { getAgent } from "@/lib/ahs";

export default async function NewAgentPage({
  searchParams,
}: {
  searchParams?: Promise<{ from?: string }>;
}) {
  const sp = (await searchParams) ?? {};
  let prefillAgent = null;
  let prefillConfig = null;

  if (sp.from) {
    const r = await getAgent(sp.from);
    if (r.ok) {
      prefillAgent = r.data.agent;
      prefillConfig = r.data.config ?? null;
    }
  }

  return (
    <Shell active="/agents">
      <section className="main">
        <TopBar
          back={{ href: "/agents", label: "Agents" }}
          crumbs={[
            { href: "/agents", label: "Agents" },
            { label: sp.from ? `Duplicate ${sp.from}` : "New agent" },
          ]}
        />
        <div className="main-body wide">
          <h1>{sp.from ? `Duplicate ${sp.from}` : "New agent"}</h1>
          <p className="lede">
            Create a new agent persona. Use <code>*</code> for full skill / tool access; comma-separated
            list otherwise (e.g. <code>public:*, sys:*</code>).
          </p>
          {sp.from && !prefillAgent && (
            <Banner kind="warn" title={`Could not load ${sp.from} as the source agent.`} />
          )}
          <AgentForm agent={prefillAgent} config={prefillConfig} duplicateOf={sp.from} />
        </div>
      </section>
    </Shell>
  );
}

import { notFound } from "next/navigation";
import { AgentForm } from "@/components/AgentForm";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { getAgent } from "@/lib/ahs";

export default async function EditAgentPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  const decoded = decodeURIComponent(name);
  const r = await getAgent(decoded);
  if (!r.ok) {
    if (r.status === 404) notFound();
    throw new Error(r.error);
  }
  return (
    <Shell active="/agents">
      <section className="main">
        <TopBar
          back={{ href: `/agents/${encodeURIComponent(decoded)}`, label: decoded }}
          crumbs={[
            { href: "/agents", label: "Agents" },
            { href: `/agents/${encodeURIComponent(decoded)}`, label: decoded },
            { label: "Edit" },
          ]}
        />
        <div className="main-body wide">
          <h1>Edit {r.data.agent.name}</h1>
          <AgentForm agent={r.data.agent} config={r.data.config ?? null} />
        </div>
      </section>
    </Shell>
  );
}

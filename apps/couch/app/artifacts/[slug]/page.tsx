import { notFound } from "next/navigation";
import { ArtifactViewer } from "@/components/ArtifactViewer";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { getArtifact, listArtifactVersions } from "@/lib/ahs";

export default async function ArtifactDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const decoded = decodeURIComponent(slug);
  const [detail, versions] = await Promise.all([
    getArtifact(decoded),
    listArtifactVersions(decoded),
  ]);
  if (!detail.ok) {
    if (detail.status === 404) notFound();
    return (
      <Shell active="/artifacts">
        <section className="main">
          <TopBar
            back={{ href: "/artifacts", label: "Artifacts" }}
            crumbs={[{ href: "/artifacts", label: "Artifacts" }, { label: decoded }]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS get-artifact failed." body={detail.error} />
          </div>
        </section>
      </Shell>
    );
  }
  const a = detail.data.artifact;
  const permalinkPath = `/artifacts/${encodeURIComponent(a.named_slug || a.artifact_id)}`;
  return (
    <Shell active="/artifacts">
      <section className="main">
        <TopBar
          back={{ href: "/artifacts", label: "Artifacts" }}
          crumbs={[
            { href: "/artifacts", label: "Artifacts" },
            { label: a.named_slug || a.title },
          ]}
        />
        <ArtifactViewer
          detail={detail.data}
          versions={versions.ok ? versions.data : null}
          permalinkPath={permalinkPath}
        />
      </section>
    </Shell>
  );
}

import Link from "next/link";
import { Banner } from "@/components/Banner";
import { Shell } from "@/components/Shell";
import { TopBar } from "@/components/TopBar";
import { listArtifacts } from "@/lib/ahs";
import { fmtBytes, fmtRelative } from "@/lib/format";

const TYPES = ["all types", "TEXT", "MEMORY", "SKILL", "TOOL_DEF", "CODE_REVIEW", "PACKAGE", "ATTACHMENT"];

export default async function ArtifactsPage({
  searchParams,
}: {
  searchParams?: Promise<{ type?: string; q?: string }>;
}) {
  const params = (await searchParams) ?? {};
  const r = await listArtifacts({
    type: params.type && params.type !== "all types" ? params.type : undefined,
    q: params.q,
    limit: 200,
  });
  return (
    <Shell active="/artifacts">
      <section className="main">
        <TopBar
          crumbs={[{ label: "Artifacts" }]}
          meta={r.ok ? `${r.data.total} results` : undefined}
        />
        <div className="main-body wide" style={{ paddingBottom: 0 }}>
          {!r.ok && <Banner kind="error" title="AHS list-artifacts failed." body={r.error} />}

          <form className="filters" id="artifact-filters">
            <input
              type="search"
              name="q"
              defaultValue={params.q ?? ""}
              placeholder="Search title or slug…"
              style={{ width: 320 }}
            />
            <select name="type" defaultValue={params.type ?? ""}>
              {TYPES.map((t) => (
                <option key={t} value={t === "all types" ? "" : t}>{t}</option>
              ))}
            </select>
            <button className="top-act primary" type="submit">Apply</button>
            {(params.q || params.type) && (
              <Link href="/artifacts" className="top-act">Clear</Link>
            )}
          </form>

          <div className="filter-bar" style={{ padding: "0 0 14px" }}>
            <Link
              href="/artifacts"
              className={`filter-chip${!params.type ? " on" : ""}`}
            >
              all types
            </Link>
            {TYPES.slice(1).map((t) => (
              <Link
                key={t}
                href={`/artifacts?type=${encodeURIComponent(t)}`}
                className={`filter-chip${params.type === t ? " on" : ""}`}
              >
                {t}
              </Link>
            ))}
          </div>

          {r.ok && (
            r.data.artifacts.length === 0 ? (
              <p className="hint">No artifacts match.</p>
            ) : (
              <table
                className="t artifact-table"
                style={{ tableLayout: "fixed", width: "100%" }}
              >
                <colgroup>
                  <col />
                  <col style={{ width: 110 }} />
                  <col style={{ width: 80 }} />
                  <col style={{ width: 70 }} />
                  <col style={{ width: 240 }} />
                  <col style={{ width: 60 }} />
                  <col style={{ width: 220 }} />
                </colgroup>
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Type</th>
                    <th>Updated</th>
                    <th>Size</th>
                    <th>Slug</th>
                    <th>Ver</th>
                    <th>Created by</th>
                  </tr>
                </thead>
                <tbody>
                  {r.data.artifacts.map((a) => (
                    <tr key={a.artifact_id} className="row-clickable">
                      <td>
                        <Link href={`/artifacts/${encodeURIComponent(a.named_slug || a.artifact_id)}`}>
                          {a.title}
                        </Link>
                      </td>
                      <td>{a.type}</td>
                      <td>{fmtRelative(a.modified_at)}</td>
                      <td>{a.size_bytes ? fmtBytes(a.size_bytes) : "—"}</td>
                      <td>
                        <code style={{ fontSize: 11, color: "var(--ink-faint)" }}>
                          {a.named_slug ?? "—"}
                        </code>
                      </td>
                      <td>{a.version != null && a.version > 0 ? `v${a.version}` : "—"}</td>
                      <td>
                        <div className="creator-cell">
                          <span>{a.creator_user_id ?? "—"}</span>
                          <span className="agent">
                            {a.creator_agent ? `🤖 ${a.creator_agent}` : "—"}
                          </span>
                        </div>
                      </td>
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

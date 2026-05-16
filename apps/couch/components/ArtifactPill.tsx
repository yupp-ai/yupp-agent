"use client";

import Link from "next/link";

export interface ArtifactRef {
  id: string;
  slug?: string;
  title?: string;
  type?: string;
  version?: number;
  action: "created" | "updated";
}

// parseArtifactResult inspects an artifact-mutating tool's input/output and
// returns a link target if the tool produced one. Today: add_artifact,
// update_artifact_content. Outputs look like:
//   {"artifact_id":"…","slug":"…","version":N,"tier":"inline"}
// Inputs we use only for the title fallback (so the user sees a name
// instead of just a UUID before the catalog round-trip).
export function parseArtifactResult(
  name: string,
  input: unknown,
  output: string | undefined,
): ArtifactRef | undefined {
  if (!output) return undefined;
  const action: ArtifactRef["action"] =
    name === "update_artifact_content" ? "updated" :
      name === "add_artifact" ? "created" :
        // Heuristic for anything else that returns an artifact_id: fall through.
        undefined as unknown as ArtifactRef["action"];
  if (!action) return undefined;
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(output) as Record<string, unknown>;
  } catch {
    return undefined;
  }
  const id = typeof parsed.artifact_id === "string" ? parsed.artifact_id : "";
  if (!id) return undefined;
  const slugVal = typeof parsed.slug === "string" ? parsed.slug : "";
  const ver = typeof parsed.version === "number" ? parsed.version : undefined;
  let title: string | undefined;
  let type: string | undefined;
  if (input && typeof input === "object") {
    const inp = input as Record<string, unknown>;
    if (typeof inp.title === "string") title = inp.title;
    if (typeof inp.artifact_type === "string") type = inp.artifact_type;
  }
  return { id, slug: slugVal || undefined, title, type, version: ver, action };
}

export function ArtifactPill({ a }: { a: ArtifactRef }) {
  const href = `/artifacts/${encodeURIComponent(a.slug || a.id)}`;
  const label = a.title || a.slug || a.id.slice(0, 8);
  return (
    <Link href={href} className="artifact-pill">
      <span className="artifact-pill-icon">📎</span>
      <span className="artifact-pill-action">{a.action === "updated" ? "updated" : "created"}</span>
      {a.type && <span className="artifact-pill-type">{a.type}</span>}
      <span className="artifact-pill-label">{label}</span>
      {a.version != null && a.version > 0 && (
        <span className="artifact-pill-ver">v{a.version}</span>
      )}
      <span className="artifact-pill-open">open ↗</span>
    </Link>
  );
}

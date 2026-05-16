// /artifacts/{slug-or-id}/raw — serves the artifact body as the response
// body so HTML artifacts can be opened as a real page (not framed inside
// the chrome). Used by the "Full Page" button on the detail view.

import { NextResponse } from "next/server";
import { getArtifact } from "@/lib/ahs";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ slug: string }> },
): Promise<Response> {
  const { slug } = await params;
  const decoded = decodeURIComponent(slug);
  const r = await getArtifact(decoded);
  if (!r.ok) {
    return new NextResponse(`artifact not found: ${r.error}`, { status: r.status || 404 });
  }
  const a = r.data.artifact;
  // URL-tier artifacts: redirect rather than echo a stub body.
  if (a.tier === "url" && r.data.body_url) {
    return NextResponse.redirect(r.data.body_url, 302);
  }
  const body = r.data.body ?? "";
  const ct = a.content_type && /html|text|json|xml|css|javascript/i.test(a.content_type)
    ? a.content_type
    : "text/html; charset=utf-8";
  return new NextResponse(body, {
    status: 200,
    headers: {
      "Content-Type": ct,
      "X-Artifact-Id": a.artifact_id,
      "Cache-Control": "no-store",
    },
  });
}

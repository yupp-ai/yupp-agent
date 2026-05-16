// Server-only proxy: hide X-API-Key. POST /api/web/agents creates an
// agent; PATCH edits; DELETE archives. Body shapes mirror the wire dto.

import { NextResponse } from "next/server";
import { archiveAgent, createAgent, editAgent } from "@/lib/ahs";

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.name || !body.display_name) {
    return NextResponse.json({ error: "name + display_name required" }, { status: 400 });
  }
  const r = await createAgent(body as Parameters<typeof createAgent>[0]);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

export async function PATCH(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.name) {
    return NextResponse.json({ error: "name required" }, { status: 400 });
  }
  const r = await editAgent(body as Parameters<typeof editAgent>[0]);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

export async function DELETE(req: Request) {
  let body: { name?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.name) {
    return NextResponse.json({ error: "name required" }, { status: 400 });
  }
  const r = await archiveAgent(body.name);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

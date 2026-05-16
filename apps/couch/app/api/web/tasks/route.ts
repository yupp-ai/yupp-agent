import { NextResponse } from "next/server";
import { createTask } from "@/lib/ahs";

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.project_id || !body.title) {
    return NextResponse.json({ error: "project_id + title required" }, { status: 400 });
  }
  const r = await createTask(body as Parameters<typeof createTask>[0]);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

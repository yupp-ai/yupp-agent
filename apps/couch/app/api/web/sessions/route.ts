// Server-only proxy that the Composer client component posts to. Hides
// the AHS API key from the browser bundle by terminating the call here.

import { NextResponse } from "next/server";
import { createSession } from "@/lib/ahs";

export async function POST(req: Request) {
  let body: { agent_name?: string; message?: string; trigger?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.agent_name || !body.message) {
    return NextResponse.json({ error: "agent_name + message required" }, { status: 400 });
  }
  const r = await createSession({
    agent_id: body.agent_name,
    trigger: (body.trigger ?? "WEB").toUpperCase(),
    message: body.message,
    source: "WEB",
  });
  if (!r.ok) {
    return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  }
  return NextResponse.json({ session_id: r.data.session_id, status: r.data.status });
}

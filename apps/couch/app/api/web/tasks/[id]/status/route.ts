import { NextResponse } from "next/server";
import { setTaskStatus } from "@/lib/ahs";

export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let body: { status?: string; result?: string; actual_spending_usd?: number };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.status) {
    return NextResponse.json({ error: "status required" }, { status: 400 });
  }
  const r = await setTaskStatus(id, body as { status: string });
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

import { NextResponse } from "next/server";
import { sendMessage } from "@/lib/ahs";

export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let body: { message?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  if (!body.message) {
    return NextResponse.json({ error: "message required" }, { status: 400 });
  }
  const r = await sendMessage(id, body.message);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

import { NextResponse } from "next/server";
import { triggerSchedule } from "@/lib/ahs";

export async function POST(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const r = await triggerSchedule(id);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

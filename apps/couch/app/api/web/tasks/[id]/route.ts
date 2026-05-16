import { NextResponse } from "next/server";
import { deleteTask } from "@/lib/ahs";

export async function DELETE(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const r = await deleteTask(id);
  if (!r.ok) return NextResponse.json({ error: r.error }, { status: r.status || 500 });
  return NextResponse.json(r.data);
}

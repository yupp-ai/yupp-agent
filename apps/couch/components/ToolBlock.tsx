"use client";

import { useState } from "react";

export interface ToolEvent {
  name: string;
  args?: string;
  meta?: string;
  preview?: string;
  full?: string;
}

export function ToolBlock({ event }: { event: ToolEvent }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className={`tool${open ? " expanded" : ""}`}
      onClick={() => setOpen((v) => !v)}
    >
      <div className="row">
        <span className="arrow">→</span>
        <span className="name">{event.name}</span>
        {event.args && <span className="args">{event.args}</span>}
        {event.meta && <span className="meta">{event.meta}</span>}
      </div>
      {event.preview && (
        <div className="row">
          <span className="arrow">←</span>
          <span className="preview">{event.preview}</span>
        </div>
      )}
      {event.full && <div className="full">{event.full}</div>}
    </div>
  );
}

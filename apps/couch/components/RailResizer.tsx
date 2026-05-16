"use client";

import { useEffect, useRef } from "react";

// Drag handle that lives in the shell grid between the left rail and
// main content. Updates the --rail-w CSS variable in-place; persists the
// chosen width to localStorage so subsequent navs keep the user's pick.
//
// Min = 228 (matches the baseline --rail-w in globals.css). Max = 480.

const STORAGE_KEY = "couch.railWidth";
const MIN = 228;
const MAX = 480;

export function RailResizer() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Restore previous width on mount.
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    if (saved >= MIN && saved <= MAX) {
      document.documentElement.style.setProperty("--rail-w", `${saved}px`);
    }
  }, []);

  function onMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    const handle = ref.current;
    if (!handle) return;
    handle.classList.add("dragging");
    const startX = e.clientX;
    const cs = getComputedStyle(document.documentElement).getPropertyValue("--rail-w").trim();
    const startW = parseInt(cs, 10) || MIN;

    function move(ev: MouseEvent) {
      const w = startW + (ev.clientX - startX);
      const clamped = Math.max(MIN, Math.min(MAX, w));
      document.documentElement.style.setProperty("--rail-w", `${clamped}px`);
    }
    function up() {
      handle?.classList.remove("dragging");
      const cur = getComputedStyle(document.documentElement).getPropertyValue("--rail-w").trim();
      const final = parseInt(cur, 10);
      if (!Number.isNaN(final)) localStorage.setItem(STORAGE_KEY, String(final));
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  }

  return (
    <div
      ref={ref}
      className="resizer rail-resizer"
      onMouseDown={onMouseDown}
      title="drag to resize sidebar"
    />
  );
}

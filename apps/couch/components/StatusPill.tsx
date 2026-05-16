// Map any status string to one of the css modifier classes in globals.css.
// Status text is printed verbatim — only the class name is normalised.

const KNOWN = new Set([
  "active",
  "pending",
  "completed",
  "failed",
  "cancelled",
  "in_progress",
  "in_review",
  "ready",
  "blocked",
]);

export function StatusPill({ status }: { status: string }) {
  const slug = status.toLowerCase().replace(/[ -]/g, "_");
  const mod = KNOWN.has(slug) ? slug : "";
  return <span className={`status-pill ${mod}`}>{status}</span>;
}

// Inline banner for non-fatal errors (e.g. AHS unreachable). Pages that
// can render their main content without the missing data render a Banner
// + the partial body; pages that need the data render <NotFound /> or a
// dedicated AHS-down state.

export function Banner({
  kind = "error",
  title,
  body,
}: {
  kind?: "info" | "error" | "warn";
  title: string;
  body?: React.ReactNode;
}) {
  const colour =
    kind === "error"
      ? { bg: "#fde7e7", border: "#f4cfcd", ink: "#b3261e" }
      : kind === "warn"
      ? { bg: "#fdf3df", border: "#efe1c2", ink: "#6b4500" }
      : { bg: "#d8e9fb", border: "#c0d8f0", ink: "#2c528a" };
  return (
    <div
      style={{
        margin: "12px 0 18px",
        padding: "10px 14px",
        background: colour.bg,
        border: `1px solid ${colour.border}`,
        borderRadius: 6,
        color: colour.ink,
        fontSize: 13,
        maxWidth: 880,
      }}
    >
      <strong>{title}</strong>
      {body && <div style={{ marginTop: 4, color: "var(--ink-soft)" }}>{body}</div>}
    </div>
  );
}

// Placeholder used on /artifacts, /admin/*, etc. — pages whose AHS endpoint
// does not exist yet. We keep the route reachable so navigation does not
// break; the page just states what's missing.
export function Pending({
  title,
  body,
}: {
  title: string;
  body: React.ReactNode;
}) {
  return (
    <div
      style={{
        margin: "24px 0",
        padding: "24px 28px",
        background: "#fff",
        border: "1px dashed var(--rule)",
        borderRadius: 8,
        maxWidth: 720,
      }}
    >
      <h2 style={{ marginTop: 0 }}>{title}</h2>
      <div style={{ color: "var(--ink-soft)", fontSize: 14, lineHeight: 1.5 }}>{body}</div>
    </div>
  );
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtUsd(n: number | undefined | null): string {
  if (n === undefined || n === null) return "—";
  return `$${n.toFixed(2)}`;
}

export function fmtRelative(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (!isFinite(t)) return iso;
  const diff = Date.now() - t;
  if (diff < 0) {
    const futureDiff = -diff;
    if (futureDiff < 60_000) return "in <1m";
    if (futureDiff < 3_600_000) return `in ${Math.round(futureDiff / 60_000)}m`;
    if (futureDiff < 86_400_000) return `in ${Math.round(futureDiff / 3_600_000)}h`;
    return `on ${new Date(iso).toLocaleDateString()}`;
  }
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  if (diff < 604_800_000) return `${Math.round(diff / 86_400_000)}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function pct(num: number, denom: number): number {
  if (!denom) return 0;
  return Math.min(100, Math.round((num / denom) * 100));
}

export function triggerLetter(t: string): string | null {
  switch (t.toUpperCase()) {
    case "API":    return "A";
    case "SLACK":  return "S";
    case "CRON":   return "C";
    case "GITHUB": return "G";
    case "AGENT":  return "@";
    case "TASK":   return "T";
    case "WEB":    return null;
    default:       return null;
  }
}

export function triggerClass(t: string): string {
  return t.toLowerCase();
}

export function shortId(id: string): string {
  return id.length > 6 ? `${id.slice(0, 6)}…` : id;
}

export function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

export function fmtDurMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

export function fmtUsdFull(n: number | null | undefined): string {
  if (n == null) return "—";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(4)}`;
}

export function fmtClockUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (!isFinite(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

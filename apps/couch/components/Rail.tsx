import Link from "next/link";
import { Tpill } from "./Tpill";
import { listSessions } from "@/lib/ahs";
import { fmtRelative } from "@/lib/format";

const NAV = [
  { href: "/", label: "Home", ico: "⌂" },
  { href: "/sessions", label: "Sessions", ico: "▤" },
  { href: "/schedules", label: "Schedules", ico: "⏱" },
  { href: "/projects", label: "Projects", ico: "⊟" },
  { href: "/agents", label: "Agents", ico: "◉" },
  { href: "/artifacts", label: "Artifacts", ico: "◇" },
  { href: "/admin", label: "Admin", ico: "⚙" },
];

const FILTERS = ["all", "web", "api", "slack", "cron", "github"];

export async function Rail({
  active,
  activeSessionId,
}: {
  active?: string;
  activeSessionId?: string;
}) {
  const result = await listSessions({ limit: 30 });
  const sessions = result.ok ? result.data.sessions : [];

  return (
    <aside className="rail">
      <div className="rail-head">
        <Link
          href="/"
          title="home"
          style={{ display: "flex", alignItems: "center", gap: 8, color: "inherit", textDecoration: "none" }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="logo" src="/logo.png" alt="Couch" />
          <div className="brand">Couch</div>
        </Link>
        <div className="scope">AngelList</div>
      </div>

      <nav className="rail-tabs">
        {NAV.map((n) => {
          const isActive =
            n.href === active || (active && active.startsWith(n.href) && n.href !== "/");
          return (
            <Link
              key={n.href}
              href={n.href}
              className={`rail-tab${isActive ? " active" : ""}`}
            >
              <span className="ico">{n.ico}</span> {n.label}
            </Link>
          );
        })}
      </nav>

      <div className="rail-section">
        Past sessions <span className="grow"></span>
        <Link href="/sessions" className="rail-action" title="search sessions">
          🔍 <span>Search</span>
        </Link>
        <Link href="/" className="rail-action" title="new session">
          ＋ <span>New</span>
        </Link>
      </div>
      <div className="filter-bar">
        {FILTERS.map((f) => (
          <button key={f} className={`filter-chip${f === "all" ? " on" : ""}`}>
            {f}
          </button>
        ))}
      </div>
      <div className="rail-list">
        {!result.ok && (
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--ink-faint)" }}>
            AHS unreachable: {result.error}
          </div>
        )}
        {result.ok && sessions.length === 0 && (
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--ink-faint)" }}>
            No sessions yet — start one from the home composer.
          </div>
        )}
        {sessions.map((s) => {
          const title = s.title?.trim() || `Session ${s.session_id.slice(0, 6)}…`;
          return (
            <Link
              key={s.session_id}
              href={`/sessions/${s.session_id}`}
              className={`session-row${activeSessionId === s.session_id ? " active" : ""}`}
            >
              <div className="body">
                <div className="title">
                  <Tpill trigger={s.trigger} />
                  {title}
                </div>
                <div className="meta">
                  {s.agent_name} · {fmtRelative(s.created_at)}
                </div>
              </div>
            </Link>
          );
        })}
      </div>

      <div className="rail-foot">
        <div className="avatar">T</div>
        <div>tian.wang@al · ADMIN</div>
      </div>
    </aside>
  );
}

import Link from "next/link";

const ITEMS = [
  { href: "/admin", label: "👥 Users" },
  { href: "/admin/roles", label: "🔐 Roles & Permissions" },
  { href: "/admin/slack-agents", label: "💬 Slack Agents" },
];

export function AdminSubNav({ active }: { active: string }) {
  return (
    <aside className="sub-nav">
      <div className="section">Admin</div>
      {ITEMS.map((it) => (
        <Link key={it.href} href={it.href} className={active === it.href ? "active" : ""}>
          {it.label}
        </Link>
      ))}
    </aside>
  );
}

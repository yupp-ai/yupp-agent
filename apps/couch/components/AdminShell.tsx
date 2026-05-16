import { AdminSubNav } from "./AdminSubNav";
import { Shell } from "./Shell";
import { TopBar } from "./TopBar";

// AdminShell wraps every admin page with the left rail, the top
// breadcrumb, and the admin sub-rail. Each page just supplies its
// active slug, crumb tail, and body.

export function AdminShell({
  active,
  crumbTail,
  children,
  actions,
}: {
  active: string;
  crumbTail: string;
  children: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <Shell active="/admin">
      <section className="main">
        <TopBar
          crumbs={[{ href: "/admin", label: "Admin" }, { label: crumbTail }]}
          actions={actions}
        />
        <div className="sub-rail">
          <AdminSubNav active={active} />
          <div className="sub-main">{children}</div>
        </div>
      </section>
    </Shell>
  );
}

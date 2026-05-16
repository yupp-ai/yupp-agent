import { AdminSubNav } from "./AdminSubNav";
import { Pending } from "./Banner";
import { Shell } from "./Shell";
import { TopBar } from "./TopBar";

// Common shell for the four admin sub-pages that point at endpoints
// cmd/ahs has not yet shipped. Each caller supplies the breadcrumb tail,
// the active sub-nav slug, the page H1, and the Pending body.

export function AdminPlaceholder({
  active,
  crumbTail,
  title,
  body,
}: {
  active: string;
  crumbTail: string;
  title: string;
  body: React.ReactNode;
}) {
  return (
    <Shell active="/admin">
      <section className="main">
        <TopBar crumbs={[{ href: "/admin", label: "Admin" }, { label: crumbTail }]} />
        <div className="sub-rail">
          <AdminSubNav active={active} />
          <div className="sub-main">
            <h1>{title}</h1>
            <Pending title={`${crumbTail} endpoint(s) pending`} body={body} />
          </div>
        </div>
      </section>
    </Shell>
  );
}

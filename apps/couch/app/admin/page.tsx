import { AdminShell } from "@/components/AdminShell";
import { AdminUsers } from "@/components/AdminUsers";
import { Banner } from "@/components/Banner";
import { listAdminRoles } from "@/lib/ahs";

export default async function AdminUsersPage() {
  const roles = await listAdminRoles();
  const roleNames = roles.ok ? roles.data.roles.map((r) => r.name) : [];
  return (
    <AdminShell active="/admin" crumbTail="Users">
      <h1 className="admin-h1">👥 Users</h1>
      {!roles.ok && (
        <Banner kind="warn" title="Could not load roles." body={roles.error} />
      )}
      <AdminUsers roleNames={roleNames} />
    </AdminShell>
  );
}

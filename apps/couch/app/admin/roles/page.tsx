import { AdminRoles } from "@/components/AdminRoles";
import { AdminShell } from "@/components/AdminShell";

export default function AdminRolesPage() {
  return (
    <AdminShell active="/admin/roles" crumbTail="Roles & Permissions">
      <h1 className="admin-h1">🔐 Roles & Permissions</h1>
      <AdminRoles />
    </AdminShell>
  );
}

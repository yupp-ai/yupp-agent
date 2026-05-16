"use client";

// Roles & Permissions admin page — one row per role, expandable to
// edit role members + permission grants. Permissions are freeform
// strings (the DB no longer enforces an enum).

import { useEffect, useState } from "react";
import {
  listAdminPermissions,
  listAdminRoles,
  listAdminUsers,
  updateRolePermissions,
  updateRoleUsers,
} from "@/lib/ahs";
import type { AdminRoleDTO, AdminUserDTO } from "@/lib/types";
import { Banner } from "./Banner";

export function AdminRoles() {
  const [roles, setRoles] = useState<AdminRoleDTO[]>([]);
  const [allUsers, setAllUsers] = useState<AdminUserDTO[]>([]);
  const [allPerms, setAllPerms] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);

  async function reload() {
    setErr(null);
    const [r, u, p] = await Promise.all([
      listAdminRoles(),
      listAdminUsers({ limit: 500 }),
      listAdminPermissions(),
    ]);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setRoles(r.data.roles);
    if (u.ok) setAllUsers(u.data.users);
    if (p.ok) setAllPerms(p.data.permissions);
  }

  useEffect(() => {
    void reload();
  }, []);

  return (
    <div className="admin-section">
      {err && <Banner kind="error" title="Load failed." body={err} />}
      <p className="hint-sm">
        Roles are a fixed, seeded set. Edit role memberships and permissions
        here; permission strings are freeform — add any key you need.
      </p>
      <table className="t admin-roles-table">
        <thead>
          <tr>
            <th>Role</th>
            <th>Description</th>
            <th>Permissions</th>
            <th>Users</th>
          </tr>
        </thead>
        <tbody>
          {roles.map((role) => (
            <RoleRow
              key={role.role_id}
              role={role}
              allUsers={allUsers}
              allPerms={allPerms}
              onChanged={() => void reload()}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RoleRow({
  role,
  allUsers,
  allPerms,
  onChanged,
}: {
  role: AdminRoleDTO;
  allUsers: AdminUserDTO[];
  allPerms: string[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [members, setMembers] = useState<AdminUserDTO[] | null>(null);
  const [newPerm, setNewPerm] = useState("");
  const [addUser, setAddUser] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function loadMembers() {
    setErr(null);
    const r = await updateRoleUsers(role.name, {});
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setMembers(r.data.users);
  }

  async function onTogglePerm(perm: string, add: boolean) {
    setErr(null);
    const r = await updateRolePermissions(role.name, add ? { add: [perm] } : { remove: [perm] });
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    onChanged();
  }

  async function onAddPerm() {
    const p = newPerm.trim();
    if (!p) return;
    await onTogglePerm(p, true);
    setNewPerm("");
  }

  async function onAddMember() {
    if (!addUser) return;
    const r = await updateRoleUsers(role.name, { add: [addUser] });
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setMembers(r.data.users);
    setAddUser("");
    onChanged();
  }

  async function onRemoveMember(uid: string) {
    const r = await updateRoleUsers(role.name, { remove: [uid] });
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setMembers(r.data.users);
    onChanged();
  }

  function toggleOpen() {
    const next = !open;
    setOpen(next);
    if (next && members === null) void loadMembers();
  }

  return (
    <>
      <tr>
        <td>
          <b>{role.name}</b>
        </td>
        <td>{role.description}</td>
        <td>
          <div className="admin-perm-badges">
            <span className="admin-perm-count">{role.permissions.length}</span>
            {role.permissions.map((p) => (
              <code key={p} className="admin-perm-badge">
                {p}
              </code>
            ))}
          </div>
        </td>
        <td>{role.user_count}</td>
      </tr>
      <tr className="admin-role-expand-row">
        <td colSpan={4}>
          <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
            <summary onClick={(e) => { e.preventDefault(); toggleOpen(); }}>
              {open ? "▾" : "▸"} Edit {role.name}
            </summary>
            {open && (
              <div className="admin-role-editor-panel">
                {err && <Banner kind="error" title="Update failed." body={err} />}

                <h3>Users in this role</h3>
                {members === null ? (
                  <p className="hint">Loading…</p>
                ) : members.length === 0 ? (
                  <p className="hint">No users.</p>
                ) : (
                  <ul className="admin-bullet-list">
                    {members.map((u) => (
                      <li key={u.user_id}>
                        <code>{u.email}</code>
                        <span className="hint-xs">({u.user_id.slice(0, 8)}…)</span>
                        <button
                          type="button"
                          className="top-act"
                          onClick={() => void onRemoveMember(u.user_id)}
                        >
                          Remove
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="admin-add-row">
                  <label>Add user</label>
                  <select
                    value={addUser}
                    onChange={(e) => setAddUser(e.target.value)}
                  >
                    <option value="">— select user —</option>
                    {allUsers
                      .filter((u) => !(members ?? []).some((m) => m.user_id === u.user_id))
                      .map((u) => (
                        <option key={u.user_id} value={u.user_id}>
                          {u.email}
                        </option>
                      ))}
                  </select>
                  <button type="button" className="top-act" onClick={() => void onAddMember()}>
                    Add
                  </button>
                </div>

                <h3>Permissions for this role</h3>
                {role.permissions.length === 0 ? (
                  <p className="hint">No permissions.</p>
                ) : (
                  <ul className="admin-bullet-list">
                    {role.permissions.map((p) => (
                      <li key={p}>
                        <code>{p}</code>
                        <button
                          type="button"
                          className="top-act"
                          onClick={() => void onTogglePerm(p, false)}
                        >
                          Remove
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="admin-add-row">
                  <label>Add permission</label>
                  <input
                    value={newPerm}
                    onChange={(e) => setNewPerm(e.target.value)}
                    list={`perms-${role.role_id}`}
                    placeholder="e.g. MANAGE_AGENTS or custom_key"
                    onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), void onAddPerm())}
                  />
                  <datalist id={`perms-${role.role_id}`}>
                    {allPerms.map((p) => (
                      <option key={p} value={p} />
                    ))}
                  </datalist>
                  <button type="button" className="top-act" onClick={() => void onAddPerm()}>
                    Add
                  </button>
                </div>
              </div>
            )}
          </details>
        </td>
      </tr>
    </>
  );
}

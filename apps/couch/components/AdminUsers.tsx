"use client";

// Browse + Add User tabs for the admin users page. All state lives here so
// filters, page, and the inline create form can talk to AHS without a hard
// page reload after each mutation.

import { useEffect, useState } from "react";
import {
  createAdminUser,
  listAdminUsers,
  setAdminUserStatus,
  setAdminUserRoles,
} from "@/lib/ahs";
import type { AdminUserDTO } from "@/lib/types";
import { AdminTabs } from "./AdminTabs";
import { Banner } from "./Banner";

const USER_TYPES = ["HUMAN", "AGENT", "SYSTEM"] as const;
const STATUSES = ["ACTIVE", "DEACTIVATED"] as const;

export function AdminUsers({ roleNames }: { roleNames: string[] }) {
  return (
    <AdminTabs
      initial="browse"
      tabs={[
        { key: "browse", label: "Browse" },
        { key: "add", label: "Add User" },
      ]}
      renderPanel={(active) =>
        active === "browse" ? (
          <BrowseUsers roleNames={roleNames} />
        ) : (
          <AddUser roleNames={roleNames} />
        )
      }
    />
  );
}

function BrowseUsers({ roleNames }: { roleNames: string[] }) {
  const [q, setQ] = useState("");
  const [userType, setUserType] = useState("HUMAN");
  const [status, setStatus] = useState("");
  const [users, setUsers] = useState<AdminUserDTO[]>([]);
  const [total, setTotal] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);

  async function reload() {
    setLoading(true);
    setErr(null);
    const r = await listAdminUsers({
      q: q || undefined,
      user_type: userType || undefined,
      status: status || undefined,
      limit: 200,
    });
    setLoading(false);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setUsers(r.data.users);
    setTotal(r.data.total);
  }

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSaveRoles(user: AdminUserDTO, next: string[]) {
    const r = await setAdminUserRoles(user.user_id, next);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setUsers((prev) => prev.map((u) => (u.user_id === user.user_id ? r.data : u)));
    setEditing(null);
  }

  async function onToggleStatus(user: AdminUserDTO) {
    const next = user.status === "ACTIVE" ? "DEACTIVATED" : "ACTIVE";
    const r = await setAdminUserStatus(user.user_id, next);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setUsers((prev) => prev.map((u) => (u.user_id === user.user_id ? r.data : u)));
  }

  return (
    <div className="admin-section">
      {err && <Banner kind="error" title="Request failed." body={err} />}
      <div className="admin-filters">
        <div className="admin-field">
          <label>Filter by email (substring match)</label>
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void reload()}
            placeholder=""
          />
        </div>
        <div className="admin-field">
          <label>User type</label>
          <select value={userType} onChange={(e) => setUserType(e.target.value)}>
            <option value="">(any)</option>
            {USER_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div className="admin-field">
          <label>Status</label>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">(any)</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <button type="button" className="top-act" onClick={() => void reload()}>
          {loading ? "…" : "Refresh"}
        </button>
      </div>
      <p className="hint-sm">
        Showing {users.length} of {total} user{total === 1 ? "" : "s"}.
      </p>
      <table className="t admin-users-table">
        <thead>
          <tr>
            <th>Type</th>
            <th>Name</th>
            <th>Email</th>
            <th>Status</th>
            <th>Roles</th>
            <th>Effective permissions</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.user_id}>
              <td>
                <span className="admin-type">{userTypeIcon(u.user_type)} {u.user_type}</span>
              </td>
              <td><b>{u.name || "—"}</b></td>
              <td><code>{u.email}</code></td>
              <td>{u.status}</td>
              <td>
                {editing === u.user_id ? (
                  <RoleEditor
                    available={roleNames}
                    selected={u.roles}
                    onCancel={() => setEditing(null)}
                    onSave={(next) => onSaveRoles(u, next)}
                  />
                ) : (
                  <div className="admin-roles">
                    {u.roles.length === 0 ? "—" : u.roles.join(", ")}
                  </div>
                )}
              </td>
              <td className="admin-perms">
                {u.permissions.length === 0 ? "—" : u.permissions.join(", ")}
              </td>
              <td>
                <div className="admin-row-actions">
                  {editing === u.user_id ? null : (
                    <button
                      type="button"
                      className="top-act"
                      onClick={() => setEditing(u.user_id)}
                    >
                      Edit
                    </button>
                  )}
                  <button
                    type="button"
                    className="top-act"
                    onClick={() => void onToggleStatus(u)}
                  >
                    {u.status === "ACTIVE" ? "Deactivate" : "Reactivate"}
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {users.length === 0 && !loading && (
            <tr>
              <td colSpan={7} className="hint">
                No users match filter.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function RoleEditor({
  available,
  selected,
  onSave,
  onCancel,
}: {
  available: string[];
  selected: string[];
  onSave: (next: string[]) => void;
  onCancel: () => void;
}) {
  const [picks, setPicks] = useState(selected);
  function toggle(name: string) {
    setPicks((prev) => (prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]));
  }
  return (
    <div className="admin-role-editor">
      <div className="admin-role-checks">
        {available.map((name) => (
          <label key={name}>
            <input
              type="checkbox"
              checked={picks.includes(name)}
              onChange={() => toggle(name)}
            />{" "}
            {name}
          </label>
        ))}
      </div>
      <div className="admin-row-actions">
        <button type="button" className="top-act primary" onClick={() => onSave(picks)}>
          Save
        </button>
        <button type="button" className="top-act" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function AddUser({ roleNames }: { roleNames: string[] }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [userType, setUserType] = useState<"HUMAN" | "AGENT" | "SYSTEM">("HUMAN");
  const [status, setStatus] = useState<"ACTIVE" | "DEACTIVATED">("ACTIVE");
  const [picks, setPicks] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function toggle(name: string) {
    setPicks((prev) => (prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setOk(null);
    if (!email.trim()) {
      setErr("email required");
      return;
    }
    setSubmitting(true);
    const r = await createAdminUser({
      email: email.trim(),
      name: name.trim() || undefined,
      user_type: userType,
      status,
      role_names: picks,
    });
    setSubmitting(false);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setOk(`Created ${r.data.email}`);
    setEmail("");
    setName("");
    setPicks([]);
  }

  return (
    <form className="admin-section admin-form" onSubmit={submit}>
      <h2>Add new user</h2>
      <p className="hint-sm">
        Creates a row in the <code>users</code> table and assigns the selected roles.
        Roles can be adjusted later from the <i>Browse</i> tab.
      </p>
      {err && <Banner kind="error" title="Create failed." body={err} />}
      {ok && <Banner kind="info" title={ok} body="" />}
      <div className="admin-field">
        <label>Email</label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
      </div>
      <div className="admin-field">
        <label>Display name (optional)</label>
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="admin-field">
        <label>User type</label>
        <select
          value={userType}
          onChange={(e) => setUserType(e.target.value as typeof userType)}
        >
          {USER_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>
      <div className="admin-field">
        <label>Status</label>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as typeof status)}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>
      <div className="admin-field">
        <label>Roles</label>
        <div className="admin-role-checks">
          {roleNames.length === 0 && <span className="hint">No roles seeded yet.</span>}
          {roleNames.map((n) => (
            <label key={n}>
              <input
                type="checkbox"
                checked={picks.includes(n)}
                onChange={() => toggle(n)}
              />{" "}
              {n}
            </label>
          ))}
        </div>
      </div>
      <button type="submit" className="top-act primary" disabled={submitting}>
        {submitting ? "Creating…" : "Create user"}
      </button>
    </form>
  );
}

function userTypeIcon(t: string): string {
  switch (t) {
    case "HUMAN":
      return "👤";
    case "AGENT":
      return "🤖";
    case "SYSTEM":
      return "⚙";
    default:
      return "•";
  }
}

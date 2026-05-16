"use client";

import { useEffect, useState } from "react";
import {
  createSlackAgent,
  deleteSlackAgent,
  listAdminUsers,
  listAgents,
  listSlackAgents,
  updateSlackAgent,
} from "@/lib/ahs";
import type { AdminUserDTO, SlackAgentDTO } from "@/lib/types";
import { AdminTabs } from "./AdminTabs";
import { Banner } from "./Banner";

const STATUSES = ["ACTIVE", "DISABLED", "PENDING_APPROVAL"] as const;

export function AdminSlackAgents() {
  const [agents, setAgents] = useState<SlackAgentDTO[]>([]);
  const [agentNames, setAgentNames] = useState<string[]>([]);
  const [users, setUsers] = useState<AdminUserDTO[]>([]);
  const [err, setErr] = useState<string | null>(null);

  async function reload() {
    setErr(null);
    const [s, a, u] = await Promise.all([
      listSlackAgents(),
      listAgents(),
      listAdminUsers({ limit: 500 }),
    ]);
    if (!s.ok) {
      setErr(s.error);
      return;
    }
    setAgents(s.data.agents);
    if (a.ok) setAgentNames(a.data.agents.map((x) => x.name));
    if (u.ok) setUsers(u.data.users);
  }

  useEffect(() => {
    void reload();
  }, []);

  return (
    <>
      {err && <Banner kind="error" title="Load failed." body={err} />}
      <AdminTabs
        initial="browse"
        tabs={[
          { key: "browse", label: "Browse" },
          { key: "add", label: "Add New Slack Agent" },
        ]}
        renderPanel={(active) =>
          active === "browse" ? (
            <BrowseSlackAgents
              agents={agents}
              agentNames={agentNames}
              users={users}
              onChanged={() => void reload()}
            />
          ) : (
            <AddSlackAgent agentNames={agentNames} onCreated={() => void reload()} />
          )
        }
      />
    </>
  );
}

function BrowseSlackAgents({
  agents,
  agentNames,
  users,
  onChanged,
}: {
  agents: SlackAgentDTO[];
  agentNames: string[];
  users: AdminUserDTO[];
  onChanged: () => void;
}) {
  const [pickedID, setPickedID] = useState<string>("");
  const picked = agents.find((a) => a.slack_agent_id === pickedID);
  return (
    <div className="admin-section">
      <h2>Registered Slack agents ({agents.length})</h2>
      <table className="t admin-slack-table">
        <thead>
          <tr>
            <th>app_id</th>
            <th>agent_name</th>
            <th>bot_name</th>
            <th>display_name</th>
            <th>status</th>
            <th>created_by</th>
            <th>created_at</th>
          </tr>
        </thead>
        <tbody>
          {agents.length === 0 ? (
            <tr>
              <td colSpan={7} className="hint">
                No Slack agents registered.
              </td>
            </tr>
          ) : (
            agents.map((a) => (
              <tr key={a.slack_agent_id}>
                <td><code>{a.app_id}</code></td>
                <td>{a.agent_name}</td>
                <td>{a.bot_name}</td>
                <td>{a.display_name}</td>
                <td>{a.status}</td>
                <td>
                  <code className="hint-xs">{creatorLabel(users, a.created_by_user_id)}</code>
                </td>
                <td>{a.created_at ?? "—"}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>

      <h2 style={{ marginTop: 28 }}>Edit / delete an existing Slack agent</h2>
      <div className="admin-field">
        <label>Select a Slack agent</label>
        <select value={pickedID} onChange={(e) => setPickedID(e.target.value)}>
          <option value="">— select —</option>
          {agents.map((a) => (
            <option key={a.slack_agent_id} value={a.slack_agent_id}>
              {a.display_name} ({a.app_id})
            </option>
          ))}
        </select>
      </div>
      {picked && (
        <EditSlackAgent
          key={picked.slack_agent_id}
          agent={picked}
          agentNames={agentNames}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}

function EditSlackAgent({
  agent,
  agentNames,
  onChanged,
}: {
  agent: SlackAgentDTO;
  agentNames: string[];
  onChanged: () => void;
}) {
  const [appID, setAppID] = useState(agent.app_id);
  const [agentName, setAgentName] = useState(agent.agent_name);
  const [botName, setBotName] = useState(agent.bot_name);
  const [displayName, setDisplayName] = useState(agent.display_name);
  const [status, setStatus] = useState(agent.status);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    const r = await updateSlackAgent(agent.slack_agent_id, {
      app_id: appID,
      agent_name: agentName,
      bot_name: botName,
      display_name: displayName,
      status,
    });
    setBusy(false);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    onChanged();
  }

  async function doDelete() {
    if (!confirm(`Delete Slack agent ${agent.display_name}?`)) return;
    const r = await deleteSlackAgent(agent.slack_agent_id);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    onChanged();
  }

  return (
    <form className="admin-form admin-edit-card" onSubmit={save}>
      <p className="hint">
        Changing <code>app_id</code> will break incoming webhooks until Slack is reconfigured — edit with care.
      </p>
      {err && <Banner kind="error" title="Update failed." body={err} />}
      <div className="admin-field">
        <label>App ID</label>
        <input value={appID} onChange={(e) => setAppID(e.target.value)} />
      </div>
      <div className="admin-field">
        <label>Agent name</label>
        <select value={agentName} onChange={(e) => setAgentName(e.target.value)}>
          {agentNames.length === 0 ? (
            <option value={agentName}>{agentName}</option>
          ) : (
            agentNames.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))
          )}
        </select>
      </div>
      <div className="admin-field">
        <label>Bot name</label>
        <input value={botName} onChange={(e) => setBotName(e.target.value)} />
      </div>
      <div className="admin-field">
        <label>Display name</label>
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
      </div>
      <div className="admin-field">
        <label>Status</label>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>
      <div className="admin-row-actions">
        <button type="submit" className="top-act primary" disabled={busy}>
          {busy ? "Saving…" : "Save changes"}
        </button>
        <button type="button" className="top-act" onClick={() => void doDelete()}>
          Delete
        </button>
      </div>
    </form>
  );
}

function AddSlackAgent({
  agentNames,
  onCreated,
}: {
  agentNames: string[];
  onCreated: () => void;
}) {
  const [appID, setAppID] = useState("");
  const [agentName, setAgentName] = useState(agentNames[0] ?? "");
  const [botName, setBotName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("ACTIVE");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!agentName && agentNames.length > 0) setAgentName(agentNames[0]);
  }, [agentNames, agentName]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    const r = await createSlackAgent({
      app_id: appID,
      agent_name: agentName,
      bot_name: botName,
      display_name: displayName,
      status,
    });
    setBusy(false);
    if (!r.ok) {
      setErr(r.error);
      return;
    }
    setAppID("");
    setBotName("");
    setDisplayName("");
    onCreated();
  }

  return (
    <form className="admin-section admin-form" onSubmit={submit}>
      <h2>Add new Slack agent</h2>
      {err && <Banner kind="error" title="Create failed." body={err} />}
      <div className="admin-field">
        <label>App ID</label>
        <input value={appID} onChange={(e) => setAppID(e.target.value)} required />
      </div>
      <div className="admin-field">
        <label>Agent name</label>
        <select value={agentName} onChange={(e) => setAgentName(e.target.value)} required>
          {agentNames.length === 0 ? (
            <option value="">(no agents registered)</option>
          ) : (
            agentNames.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))
          )}
        </select>
      </div>
      <div className="admin-field">
        <label>Bot name</label>
        <input value={botName} onChange={(e) => setBotName(e.target.value)} required />
      </div>
      <div className="admin-field">
        <label>Display name</label>
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
      </div>
      <div className="admin-field">
        <label>Status</label>
        <select value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>
      <button type="submit" className="top-act primary" disabled={busy}>
        {busy ? "Creating…" : "Create"}
      </button>
    </form>
  );
}

function creatorLabel(users: AdminUserDTO[], uid?: string | null): string {
  if (!uid) return "—";
  const u = users.find((x) => x.user_id === uid);
  return u ? u.email : uid.slice(0, 8) + "…";
}

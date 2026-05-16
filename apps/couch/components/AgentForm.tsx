"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { AgentConfigDTO, AgentInfo } from "@/lib/types";

const DEFAULT_MODEL = "anthropic/claude-sonnet-4-6";

export interface AgentFormProps {
  // null = create; agent supplied = edit
  agent?: AgentInfo | null;
  config?: AgentConfigDTO | null;
  // Pre-fill name on duplicate (also clears the agent so we POST create).
  duplicateOf?: string;
}

interface FormState {
  name: string;
  display_name: string;
  description: string;
  executor_type: string;
  executor_model: string;
  skills_allowed: string;
  skills_denied: string;
  tools_allowed: string;
  tools_denied: string;
  always_load: string;
}

function listToText(xs?: string[] | null): string {
  if (!xs || xs.length === 0) return "";
  return xs.join(", ");
}
function textToList(s: string): string[] {
  return s.split(",").map((x) => x.trim()).filter((x) => x.length > 0);
}

export function AgentForm({ agent, config, duplicateOf }: AgentFormProps) {
  const isEdit = !!agent && !duplicateOf;
  const router = useRouter();
  const [pending, start] = useTransition();
  const [err, setErr] = useState<string | null>(null);

  const [form, setForm] = useState<FormState>(() => ({
    name: duplicateOf ? "" : agent?.name ?? "",
    display_name: agent?.display_name ?? "",
    description: agent?.description ?? "",
    executor_type: (agent?.executor_type ?? "HARNESSED").toUpperCase(),
    executor_model: agent?.executor_model ?? agent?.llm_model ?? DEFAULT_MODEL,
    skills_allowed: listToText(config?.skills_allowed ?? ["*"]),
    skills_denied: listToText(config?.skills_denied),
    tools_allowed: listToText(config?.tools_allowed ?? ["*"]),
    tools_denied: listToText(config?.tools_denied),
    always_load: listToText(config?.always_load ?? ["sys:*"]),
  }));

  function field<K extends keyof FormState>(key: K) {
    return {
      value: form[key],
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
        setForm((p) => ({ ...p, [key]: e.target.value })),
      disabled: pending,
    };
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!form.name.trim()) {
      setErr("name required");
      return;
    }
    if (!form.display_name.trim()) {
      setErr("display_name required");
      return;
    }
    const body = {
      name: form.name.trim(),
      display_name: form.display_name.trim(),
      description: form.description.trim() || undefined,
      executor_type: form.executor_type,
      executor_model: form.executor_model.trim() || undefined,
      config: {
        skills_allowed: textToList(form.skills_allowed),
        skills_denied: textToList(form.skills_denied),
        tools_allowed: textToList(form.tools_allowed),
        tools_denied: textToList(form.tools_denied),
        always_load: textToList(form.always_load),
      },
    };

    start(async () => {
      const res = await fetch("/api/web/agents", {
        method: isEdit ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = (await res.json().catch(() => ({}))) as { error?: string; name?: string };
      if (!res.ok) {
        setErr(j.error || `HTTP ${res.status}`);
        return;
      }
      router.push(`/agents/${encodeURIComponent(body.name)}`);
      router.refresh();
    });
  }

  return (
    <form onSubmit={submit} style={{ maxWidth: 720 }}>
      <table className="t">
        <tbody>
          <tr>
            <th style={{ width: 160 }}>Name (slug)</th>
            <td>
              <input type="text" {...field("name")} readOnly={isEdit} placeholder="data-analyst" className="form-input" />
              {isEdit && <div className="hint-xs">name is the immutable slug — duplicate the agent if you need to rename</div>}
            </td>
          </tr>
          <tr>
            <th>Display name</th>
            <td><input type="text" {...field("display_name")} placeholder="Data Analyst" className="form-input" /></td>
          </tr>
          <tr>
            <th>Description</th>
            <td>
              <textarea {...field("description")} rows={2} className="form-input" placeholder="Generic …" />
            </td>
          </tr>
          <tr>
            <th>Executor</th>
            <td>
              <select {...field("executor_type")} className="form-input">
                <option value="HARNESSED">HARNESSED</option>
                <option value="RAW">RAW</option>
              </select>
            </td>
          </tr>
          <tr>
            <th>Model</th>
            <td><input type="text" {...field("executor_model")} className="form-input" /></td>
          </tr>
          <tr>
            <th>skills_allowed</th>
            <td><input type="text" {...field("skills_allowed")} className="form-input" placeholder="public:*, sys:*" /></td>
          </tr>
          <tr>
            <th>skills_denied</th>
            <td><input type="text" {...field("skills_denied")} className="form-input" /></td>
          </tr>
          <tr>
            <th>tools_allowed</th>
            <td><input type="text" {...field("tools_allowed")} className="form-input" placeholder="* (all tools)" /></td>
          </tr>
          <tr>
            <th>tools_denied</th>
            <td><input type="text" {...field("tools_denied")} className="form-input" /></td>
          </tr>
          <tr>
            <th>always_load</th>
            <td><input type="text" {...field("always_load")} className="form-input" placeholder="sys:*" /></td>
          </tr>
        </tbody>
      </table>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 16 }}>
        <button type="submit" className="top-act primary" disabled={pending}>
          {pending ? "Saving…" : isEdit ? "Save changes" : "Create agent"}
        </button>
        <button type="button" className="top-act" onClick={() => router.back()} disabled={pending}>
          Cancel
        </button>
        {err && <span className="error-inline">{err}</span>}
      </div>
    </form>
  );
}

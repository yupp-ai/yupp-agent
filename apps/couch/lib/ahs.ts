// AHS HTTP client.
//
// - Server-side: hits AHS directly with X-API-Key from AHS_API_KEY env.
// - Browser-side: routes through the server proxy at
//   /api/ahs/[...path]/route.ts, which is gated by the Couch session
//   cookie and injects X-API-Key from server-side env. The AHS API key
//   is never bundled into the browser.
// - WebSocket: opened to /api/ahs/session/{id}/ws; a `beforeFiles`
//   rewrite in next.config.mjs forwards the upgrade to AHS with
//   api_key=<server env> appended as a query param. Browser still does
//   not see the key.

import type {
  AdminPermissionListResponse,
  AdminRoleListResponse,
  AdminUserDTO,
  AdminUserListResponse,
  AgentCreateResponse,
  AgentDetailResponse,
  AgentInfo,
  AgentListResponse,
  ArtifactDetailResponse,
  ArtifactListResponse,
  ArtifactVersionsResponse,
  DebugSessionResponse,
  ModelsListResponse,
  ProjectInfo,
  ProjectListResponse,
  ScheduleDetail,
  ScheduleInfo,
  ScheduleListResponse,
  ScheduleRunsResponse,
  SessionCreateRequest,
  SessionCreateResponse,
  SessionDetailResponse,
  SessionHistoryResponse,
  SessionInfo,
  SessionListResponse,
  SlackAgentDTO,
  SlackAgentListResponse,
  TaskInfo,
  TaskListResponse,
} from "./types";

// Env names use a COUCH_ prefix so they don't collide with other apps that
// share /data/ahs/.env (artifact-viewer uses VIEWER_*, etc.). Bare names
// are kept as a fallback for local dev and tests.
const SERVER_BASE = (process.env.COUCH_AHS_BASE_URL ?? process.env.AHS_BASE_URL ?? "http://localhost:8090").replace(/\/$/, "");
const SERVER_KEY = process.env.COUCH_AHS_API_KEY ?? process.env.AHS_API_KEY ?? "";
const IS_BROWSER = typeof window !== "undefined";

export class AhsError extends Error {
  constructor(message: string, public status: number) {
    super(message);
    this.name = "AhsError";
  }
}

interface FetchOpts {
  method?: string;
  body?: unknown;
  search?: Record<string, string | number | boolean | undefined>;
  signal?: AbortSignal;
}

async function ahs<T>(path: string, opts: FetchOpts = {}): Promise<T> {
  // Same-origin proxy in the browser via /api/ahs/*; direct call from
  // server components. path always starts with "/ahs/...".
  const target = IS_BROWSER
    ? "/api" + path
    : SERVER_BASE + path;
  const url = IS_BROWSER ? new URL(target, window.location.origin) : new URL(target);
  if (opts.search) {
    for (const [k, v] of Object.entries(opts.search)) {
      if (v === undefined) continue;
      url.searchParams.set(k, String(v));
    }
  }
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  // Server-side: send X-API-Key directly. Browser-side: the proxy at
  // /api/ahs/[...] adds the key after verifying the session cookie.
  if (!IS_BROWSER) {
    headers["X-API-Key"] = SERVER_KEY;
  }
  const res = await fetch(url, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    // AHS error bodies are guaranteed JSON ({"detail": "..."} — see
    // cmd/ahs/agents.go:190 writeError). The catch only fires on
    // transport-level failures (truncated body, connection reset mid-
    // response), in which case we keep the status-code-only message.
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      // fall through to status-code-only detail
    }
    throw new AhsError(`AHS ${path}: ${detail}`, res.status);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

// Result wrapper so pages can render a clear "AHS not reachable" banner
// instead of crashing the whole render tree when AHS is down. Most pages
// are server components, so an unhandled throw would break the page.
export type Result<T> = { ok: true; data: T } | { ok: false; error: string; status: number };

async function safe<T>(fn: () => Promise<T>): Promise<Result<T>> {
  try {
    const data = await fn();
    return { ok: true, data };
  } catch (err) {
    if (err instanceof AhsError) {
      return { ok: false, error: err.message, status: err.status };
    }
    return { ok: false, error: (err as Error).message ?? String(err), status: 0 };
  }
}

// ---------- agents ----------

export const listAgents = () =>
  safe(() => ahs<AgentListResponse>("/ahs/agents"));

export const getAgent = (name: string, opts?: { withPrompts?: boolean }) =>
  safe(() =>
    ahs<AgentDetailResponse>(`/ahs/agent/${encodeURIComponent(name)}`, {
      search: opts?.withPrompts ? { include_system_prompts: "true" } : undefined,
    }),
  );

export const createAgent = (body: {
  name: string;
  display_name: string;
  description?: string;
  executor_type?: string;
  executor_model?: string;
  config?: Record<string, unknown>;
  creator_user_id?: string;
}) => safe(() => ahs<AgentCreateResponse>("/ahs/agent/create", { method: "POST", body }));

export const editAgent = (body: {
  name: string;
  display_name?: string;
  description?: string;
  executor_type?: string;
  executor_model?: string;
  config?: Record<string, unknown>;
}) => safe(() => ahs<AgentInfo>("/ahs/agent/edit", { method: "POST", body }));

export const archiveAgent = (name: string) =>
  safe(() =>
    ahs<{ archived: boolean; name: string }>("/ahs/agent/archive", {
      method: "POST",
      body: { name },
    }),
  );

// ---------- sessions ----------

export const listSessions = (opts?: {
  q?: string;
  status?: string;
  agent_name?: string;
  user_id?: string;
  limit?: number;
  offset?: number;
  root_sessions_only?: boolean;
}) =>
  safe(() =>
    ahs<SessionListResponse>("/ahs/sessions", {
      search: opts as Record<string, string | number | boolean | undefined>,
    }),
  );

export const getSession = (id: string) =>
  safe(() => ahs<SessionDetailResponse>(`/ahs/session/${id}`));

export const getSessionHistory = (id: string, opts?: { limit?: number; offset?: number }) =>
  safe(() => ahs<SessionHistoryResponse>(`/ahs/session/${id}/history`, { search: opts }));

export const getSessionDebug = (id: string) =>
  safe(() => ahs<DebugSessionResponse>(`/ahs/session/${id}/debug`));

export const createSession = (body: SessionCreateRequest) =>
  safe(() => ahs<SessionCreateResponse>("/ahs/session/create", { method: "POST", body }));

export const stopSession = (sessionId: string) =>
  safe(() =>
    ahs<{ session_id: string; cancelled: boolean }>("/ahs/session/stop", {
      method: "POST",
      body: { session_id: sessionId },
    }),
  );

export const sendMessage = (sessionId: string, message: string, userId?: string) =>
  safe(() =>
    ahs<{ session_id: string; turn_number: number; status: string }>("/ahs/session/message", {
      method: "POST",
      body: { session_id: sessionId, message, user_id: userId, source: "WEB" },
    }),
  );

// ---------- projects ----------

export const listProjects = (opts?: { limit?: number; offset?: number }) =>
  safe(() => ahs<ProjectListResponse>("/ahs/projects", { search: opts }));

export const findProject = async (id: string): Promise<Result<ProjectInfo>> => {
  // No GET /ahs/project/{id} on AHS today; we fetch the page that contains it.
  const page = await listProjects({ limit: 200 });
  if (!page.ok) return page;
  const found = page.data.projects.find((p) => p.project_id === id);
  if (!found) return { ok: false, error: "project not found", status: 404 };
  return { ok: true, data: found };
};

// ---------- tasks ----------

export const listProjectTasks = (projectId: string) =>
  safe(() => ahs<TaskListResponse>(`/ahs/projects/${projectId}/tasks`));

export const getTask = (id: string) => safe(() => ahs<TaskInfo>(`/ahs/task/${id}`));

export const createTask = (body: {
  project_id: string;
  title: string;
  description?: string;
  status?: string;
  priority?: string;
  agent_name?: string;
  estimated_effort?: string;
  depends_on?: unknown[];
  parent_task_id?: string;
}) => safe(() => ahs<TaskInfo>("/ahs/task/create", { method: "POST", body }));

export const setTaskStatus = (
  id: string,
  body: { status: string; result?: string; actual_spending_usd?: number },
) =>
  safe(() => ahs<TaskInfo>(`/ahs/task/${id}/status`, { method: "POST", body }));

export const restartTask = (id: string) =>
  safe(() => ahs<TaskInfo>(`/ahs/task/${id}/restart`, { method: "POST" }));

export const deleteTask = (id: string) =>
  safe(() => ahs<{ deleted: boolean }>(`/ahs/task/${id}`, { method: "DELETE" }));

// ---------- schedules ----------

export const listSchedules = (opts?: { agent_name?: string }) =>
  safe(() => ahs<ScheduleListResponse>("/ahs/schedules", { search: opts }));

export const getSchedule = (id: string) =>
  safe(() => ahs<ScheduleDetail>(`/ahs/schedule/${id}`));

export const listScheduleRuns = (id: string, opts?: { limit?: number }) =>
  safe(() => ahs<ScheduleRunsResponse>(`/ahs/schedule/${id}/runs`, { search: opts }));

export const triggerSchedule = (id: string) =>
  safe(() =>
    ahs<{ schedule_id: string; status: string }>(`/ahs/schedule/${id}/trigger`, { method: "POST" }),
  );

export const deleteSchedule = (id: string) =>
  safe(() => ahs<{ deleted: boolean }>(`/ahs/schedule/${id}`, { method: "DELETE" }));

// ---------- artifacts ----------

export const listArtifacts = (opts?: { type?: string; q?: string; limit?: number }) =>
  safe(() => ahs<ArtifactListResponse>("/ahs/artifacts", { search: opts }));

export const getArtifact = (slug: string) =>
  safe(() => ahs<ArtifactDetailResponse>(`/ahs/artifact/${encodeURIComponent(slug)}`));

export const listArtifactVersions = (slug: string) =>
  safe(() => ahs<ArtifactVersionsResponse>(`/ahs/artifact/${encodeURIComponent(slug)}/versions`));

export const archiveArtifact = (body: { slug?: string; artifact_id?: string }) =>
  safe(() =>
    ahs<{ archived: boolean }>("/ahs/artifact/archive", { method: "POST", body }),
  );

// ---------- misc ----------

export const listModels = () => safe(() => ahs<ModelsListResponse>("/ahs/models"));

export const resolveUser = (email: string) =>
  safe(() =>
    ahs<{ user_id: string; email: string }>("/ahs/resolve_user", {
      method: "POST",
      body: { email },
    }),
  );

// ---------- admin ----------

export const listAdminUsers = (opts?: {
  q?: string;
  user_type?: string;
  status?: string;
  limit?: number;
  offset?: number;
}) =>
  safe(() =>
    ahs<AdminUserListResponse>("/ahs/admin/users", {
      search: opts as Record<string, string | number | boolean | undefined>,
    }),
  );

export const createAdminUser = (body: {
  email: string;
  name?: string;
  user_type?: string;
  status?: string;
  role_names?: string[];
}) => safe(() => ahs<AdminUserDTO>("/ahs/admin/users", { method: "POST", body }));

export const setAdminUserStatus = (id: string, status: string) =>
  safe(() =>
    ahs<AdminUserDTO>(`/ahs/admin/users/${encodeURIComponent(id)}/status`, {
      method: "POST",
      body: { status },
    }),
  );

export const setAdminUserRoles = (id: string, role_names: string[]) =>
  safe(() =>
    ahs<AdminUserDTO>(`/ahs/admin/users/${encodeURIComponent(id)}/roles`, {
      method: "POST",
      body: { role_names },
    }),
  );

export const listAdminRoles = () =>
  safe(() => ahs<AdminRoleListResponse>("/ahs/admin/roles"));

export const updateRolePermissions = (
  roleName: string,
  body: { add?: string[]; remove?: string[] },
) =>
  safe(() =>
    ahs<{ role: string; permissions: string[] }>(
      `/ahs/admin/roles/${encodeURIComponent(roleName)}/permissions`,
      { method: "POST", body },
    ),
  );

export const updateRoleUsers = (
  roleName: string,
  body: { add?: string[]; remove?: string[] },
) =>
  safe(() =>
    ahs<{ role: string; users: AdminUserDTO[] }>(
      `/ahs/admin/roles/${encodeURIComponent(roleName)}/users`,
      { method: "POST", body },
    ),
  );

export const listAdminPermissions = () =>
  safe(() => ahs<AdminPermissionListResponse>("/ahs/admin/permissions"));

export const listSlackAgents = () =>
  safe(() => ahs<SlackAgentListResponse>("/ahs/admin/slack-agents"));

export const createSlackAgent = (body: {
  app_id: string;
  agent_name: string;
  bot_name: string;
  display_name: string;
  status?: string;
}) => safe(() => ahs<SlackAgentDTO>("/ahs/admin/slack-agents", { method: "POST", body }));

export const updateSlackAgent = (
  id: string,
  body: Partial<{
    app_id: string;
    agent_name: string;
    bot_name: string;
    display_name: string;
    status: string;
  }>,
) =>
  safe(() =>
    ahs<SlackAgentDTO>(`/ahs/admin/slack-agents/${encodeURIComponent(id)}`, {
      method: "POST",
      body,
    }),
  );

export const deleteSlackAgent = (id: string) =>
  safe(() =>
    ahs<{ deleted: boolean }>(`/ahs/admin/slack-agents/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  );

// Convenience: probe /healthz for the page-level banner.
export async function ahsHealthy(): Promise<boolean> {
  try {
    const url = IS_BROWSER ? "/api/healthz" : `${SERVER_BASE}/healthz`;
    const res = await fetch(url, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

// re-export the type so call sites only import from "@/lib/ahs"
export type { SessionInfo };

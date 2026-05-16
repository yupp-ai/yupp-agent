import { AdminShell } from "@/components/AdminShell";
import { AdminSlackAgents } from "@/components/AdminSlackAgents";

export default function AdminSlackAgentsPage() {
  return (
    <AdminShell active="/admin/slack-agents" crumbTail="Slack Agents">
      <h1 className="admin-h1">💬 Slack Agents</h1>
      <AdminSlackAgents />
    </AdminShell>
  );
}

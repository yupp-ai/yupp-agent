import Link from "next/link";
import { notFound } from "next/navigation";
import { Banner } from "@/components/Banner";
import { SessionChat } from "@/components/SessionChat";
import { Shell } from "@/components/Shell";
import { Tpill } from "@/components/Tpill";
import { TopBar } from "@/components/TopBar";
import { getSession, getSessionHistory } from "@/lib/ahs";

export default async function SessionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  const [detail, history] = await Promise.all([
    getSession(id),
    getSessionHistory(id, { limit: 200 }),
  ]);

  if (!detail.ok) {
    if (detail.status === 404) notFound();
    return (
      <Shell active="/sessions" activeSessionId={id}>
        <section className="main">
          <TopBar
            back={{ href: "/sessions", label: "Sessions" }}
            crumbs={[{ href: "/sessions", label: "Sessions" }, { label: id.slice(0, 8) }]}
          />
          <div className="main-body wide">
            <Banner kind="error" title="AHS get-session failed." body={detail.error} />
          </div>
        </section>
      </Shell>
    );
  }

  const sess = detail.data.session;
  const initialHistory = history.ok ? history.data.messages : [];
  const initialStatus = sess.status;

  return (
    <Shell active="/sessions" activeSessionId={id}>
      <section className="main">
        <TopBar
          back={{ href: "/sessions", label: "Sessions" }}
          crumbs={[
            { href: "/sessions", label: "Sessions" },
            { label: sess.title || `Session ${sess.session_id.slice(0, 6)}…` },
          ]}
          meta={
            <>
              <Tpill trigger={sess.trigger} variant="wide" /> {sess.agent_name} ·{" "}
              {sess.message_count} messages
            </>
          }
          actions={
            <Link className="top-act" href={`/sessions/${id}/debug`}>
              Debug ⚙
            </Link>
          }
        />
        <SessionChat
          sessionId={sess.session_id}
          agentName={sess.agent_name}
          initialHistory={initialHistory}
          initialStatus={initialStatus}
        />
      </section>
    </Shell>
  );
}

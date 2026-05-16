"use client";

// DebugEventTable — single info-dense table covering both messages and the
// tool calls flattened from each message's raw_events. One row = one event
// in the session timeline. Click a row to expand the body / tool I/O.

import { useMemo, useState } from "react";
import { fmtClockUtc, fmtDurMs, fmtUsdFull, truncate } from "@/lib/format";
import type { DebugMessage, DebugToolCall } from "@/lib/types";

type Row =
  | {
      kind: "message";
      idx: number;
      t: number;
      delta: string;
      msg: DebugMessage;
    }
  | {
      kind: "tool";
      idx: number;
      t: number;
      delta: string;
      msg: DebugMessage;
      tool: DebugToolCall;
    };

export function DebugEventTable({
  messages,
  startMs,
}: {
  messages: DebugMessage[];
  startMs: number;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<"all" | "errors" | "tools" | "messages">("all");

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    let idx = 0;
    for (const msg of messages) {
      const t = new Date(msg.created_at).getTime();
      const delta = startMs ? `+${((t - startMs) / 1000).toFixed(2)}s` : "—";
      out.push({ kind: "message", idx: idx++, t, delta, msg });
      if (msg.tool_calls?.length) {
        for (const tool of msg.tool_calls) {
          out.push({ kind: "tool", idx: idx++, t, delta, msg, tool });
        }
      }
    }
    return out;
  }, [messages, startMs]);

  const visible = useMemo(() => {
    switch (filter) {
      case "errors":
        return rows.filter(
          (r) =>
            (r.kind === "message" &&
              (r.msg.completion_status === "FAILED" ||
                r.msg.error_type !== "NONE" ||
                r.msg.completion_status === "ABORTED")) ||
            (r.kind === "tool" && r.tool.is_error),
        );
      case "tools":
        return rows.filter((r) => r.kind === "tool");
      case "messages":
        return rows.filter((r) => r.kind === "message");
      default:
        return rows;
    }
  }, [rows, filter]);

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <div className="debug-events">
      <div className="debug-toolbar">
        <span className="hint-xs">
          {rows.length} events ({messages.length} messages,{" "}
          {rows.length - messages.length} tool calls)
        </span>
        <div className="debug-filters">
          {(["all", "messages", "tools", "errors"] as const).map((k) => (
            <button
              key={k}
              type="button"
              className={`top-act${filter === k ? " primary" : ""}`}
              onClick={() => setFilter(k)}
            >
              {k}
            </button>
          ))}
        </div>
      </div>

      <table className="t debug-table">
        <colgroup>
          <col className="c-num" />
          <col className="c-time" />
          <col className="c-delta" />
          <col className="c-kind" />
          <col className="c-status" />
          <col className="c-label" />
          <col className="c-model" />
          <col className="c-detail" />
          <col className="c-dur" />
          <col className="c-cost" />
          <col className="c-tt" />
        </colgroup>
        <thead>
          <tr>
            <th>#</th>
            <th>Time</th>
            <th>Δ</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Role/Tool</th>
            <th>Model</th>
            <th>Detail</th>
            <th>Dur</th>
            <th>Cost</th>
            <th>TTFCT/TTLCT</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((r) => {
            const key = r.kind === "message" ? `m-${r.msg.message_id}` : `t-${r.msg.message_id}-${r.tool.step}`;
            const isOpen = expanded.has(key);
            const errClass =
              r.kind === "message"
                ? r.msg.completion_status === "FAILED" || r.msg.error_type !== "NONE"
                  ? " err"
                  : ""
                : r.tool.is_error
                  ? " err"
                  : "";
            return (
              <RowFragment
                key={key}
                row={r}
                errClass={errClass}
                isOpen={isOpen}
                onToggle={() => toggle(key)}
              />
            );
          })}
          {visible.length === 0 && (
            <tr>
              <td colSpan={11} className="hint">
                No events match filter.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function RowFragment({
  row,
  errClass,
  isOpen,
  onToggle,
}: {
  row: Row;
  errClass: string;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const time = fmtClockUtc(row.kind === "message" ? row.msg.created_at : row.msg.created_at);

  let kind: string;
  let status: string;
  let label: string;
  let detail: string;
  let durMs: number | null | undefined;
  let cost: number | null | undefined;
  let timing: string;
  let model: string;

  if (row.kind === "message") {
    const m = row.msg;
    kind = m.role.toLowerCase();
    status = m.completion_status === "SUCCESS" ? "ok" : m.completion_status.toLowerCase();
    if (m.error_type !== "NONE") status += `/${m.error_type.replace("ERROR_", "").toLowerCase()}`;
    label = m.role;
    detail = m.content ? truncate(m.content.replace(/\s+/g, " "), 140) : "(empty)";
    durMs = m.duration_ms;
    cost = m.cost_usd;
    timing =
      m.ttfct_ms != null || m.ttlct_ms != null
        ? `${m.ttfct_ms ?? "—"} / ${m.ttlct_ms ?? "—"}`
        : "—";
    model = m.llm_name || "—";
  } else {
    const t = row.tool;
    kind = "tool";
    status = t.is_error ? "error" : "ok";
    label = t.name;
    const inputStr = t.input ? JSON.stringify(t.input) : "";
    detail = truncate(inputStr, 140);
    durMs = t.duration_ms;
    cost = null;
    timing = "—";
    model = "—";
  }

  return (
    <>
      <tr className={`row-clickable${errClass}`} onClick={onToggle}>
        <td className="mono small">
          <span className="ix">{row.idx}</span>
          <span className="turn">t{row.msg.turn_number}</span>
        </td>
        <td className="mono small">{time}</td>
        <td className="mono small">{row.delta}</td>
        <td>
          <span className={`debug-kind k-${kind}`}>{kind}</span>
        </td>
        <td className="mono small">{status}</td>
        <td className="mono small">{label}</td>
        <td className="mono small">{model}</td>
        <td className="ellipsis mono small">{detail}</td>
        <td className="mono small">{fmtDurMs(durMs)}</td>
        <td className="mono small">{fmtUsdFull(cost)}</td>
        <td className="mono small">{timing}</td>
      </tr>
      {isOpen && (
        <tr className="debug-detail">
          <td></td>
          <td colSpan={10}>
            <DebugRowDetail row={row} />
          </td>
        </tr>
      )}
    </>
  );
}

function DebugRowDetail({ row }: { row: Row }) {
  if (row.kind === "message") {
    const m = row.msg;
    return (
      <div className="debug-detail-body">
        <div className="debug-detail-meta">
          <span>
            <b>id:</b> <code>{m.message_id}</code>
          </span>
          {m.llm_message_id && (
            <span>
              <b>llm_message_id:</b> <code>{m.llm_message_id}</code>
            </span>
          )}
          {m.num_agent_turns != null && (
            <span>
              <b>num_agent_turns:</b> {m.num_agent_turns}
            </span>
          )}
          {m.creator_user_id && (
            <span>
              <b>user:</b> <code>{m.creator_user_id}</code>
            </span>
          )}
          {m.from_agent_name && (
            <span>
              <b>from_agent:</b> {m.from_agent_name}
            </span>
          )}
          {m.slack_ts && (
            <span>
              <b>slack_ts:</b> <code>{m.slack_ts}</code>
            </span>
          )}
        </div>
        <pre className="debug-pre">{m.content ?? "(no content)"}</pre>
      </div>
    );
  }
  const t = row.tool;
  return (
    <div className="debug-detail-body">
      <div className="debug-detail-meta">
        <span>
          <b>tool_use_id:</b> <code>{t.tool_use_id}</code>
        </span>
        <span>
          <b>step:</b> {t.step}
        </span>
      </div>
      {t.input && (
        <>
          <div className="hint-xs" style={{ marginTop: 6 }}>
            input
          </div>
          <pre className="debug-pre">{JSON.stringify(t.input, null, 2)}</pre>
        </>
      )}
      {t.output != null && t.output !== "" && (
        <>
          <div className="hint-xs" style={{ marginTop: 6 }}>
            output {t.is_error ? "(error)" : ""}
          </div>
          <pre className={`debug-pre${t.is_error ? " err" : ""}`}>{t.output}</pre>
        </>
      )}
      {!t.input && (!t.output || t.output === "") && <span className="hint">No I/O recorded.</span>}
    </div>
  );
}

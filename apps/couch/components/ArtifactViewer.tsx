"use client";

// ArtifactViewer — client wrapper for the artifact detail page.
// Holds width state (narrow|medium|wide), the copy-permalink button,
// and the Full Page link for HTML artifacts.

import { useCallback, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ArtifactDetailResponse, ArtifactVersionsResponse } from "@/lib/types";
import { fmtBytes, fmtRelative } from "@/lib/format";

type Width = "narrow" | "medium" | "wide";

export function ArtifactViewer({
  detail,
  versions,
  permalinkPath,
}: {
  detail: ArtifactDetailResponse;
  versions: ArtifactVersionsResponse | null;
  permalinkPath: string;
}) {
  const a = detail.artifact;
  const body = detail.body ?? "";
  const versionRows = versions?.versions ?? [];
  const isHtml = isHtmlish(a.content_type, body);
  const markdownish = !isHtml && isMarkdownish(a.content_type, body);

  const [width, setWidth] = useState<Width>("medium");
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async () => {
    const url = typeof window !== "undefined" ? window.location.origin + permalinkPath : permalinkPath;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Fallback: select-and-copy via a temp input. Some browsers block
      // clipboard.writeText on non-HTTPS or non-user-gesture contexts.
      const ta = document.createElement("textarea");
      ta.value = url;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        setCopied(true);
        setTimeout(() => setCopied(false), 1400);
      } finally {
        document.body.removeChild(ta);
      }
    }
  }, [permalinkPath]);

  return (
    <div className="artifact-page">
      <div className="artifact-toolbar">
        <button type="button" className="top-act" onClick={copy}>
          {copied ? "✓ copied" : "copy permalink"}
        </button>
        <div className="artifact-width-group">
          {(["narrow", "medium", "wide"] as const).map((w) => (
            <button
              key={w}
              type="button"
              className={`top-act${width === w ? " primary" : ""}`}
              onClick={() => setWidth(w)}
            >
              {w}
            </button>
          ))}
        </div>
        {isHtml && (
          <a
            className="top-act"
            href={`${permalinkPath}/raw`}
            target="_blank"
            rel="noreferrer"
          >
            Full Page ↗
          </a>
        )}
        {detail.body_url && (
          <a className="top-act" href={detail.body_url} target="_blank" rel="noreferrer">
            open URL ↗
          </a>
        )}
      </div>

      <div className={`main-body ${width}`} style={{ paddingTop: 18 }}>
        <div className="artifact-meta-box">
          <h1 className="artifact-title">{a.title}</h1>
          <div className="artifact-meta">
            <span className="badge">{a.type}</span>
            {a.version != null && <span className="badge">v{a.version}</span>}
            {a.size_bytes != null && <span>{fmtBytes(a.size_bytes)}</span>}
            <span className="badge">{a.tier}</span>
            {a.content_type && <span className="mono">{a.content_type}</span>}
            {a.sha256 && <span className="mono">sha {a.sha256.slice(0, 8)}…</span>}
            <span>updated {fmtRelative(a.modified_at)}</span>
            {a.named_slug && (
              <span className="mono slug">slug: {a.named_slug}</span>
            )}
          </div>
          {a.description && <p className="artifact-desc">{a.description}</p>}
        </div>

        <article className="artifact-body">
          {a.tier === "url" && detail.body_url ? (
            <p>
              <a href={detail.body_url} target="_blank" rel="noreferrer">
                {detail.body_url}
              </a>
            </p>
          ) : body ? (
            isHtml ? (
              <iframe
                className="artifact-html-frame"
                sandbox="allow-same-origin"
                srcDoc={body}
                title={a.title}
              />
            ) : markdownish ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
            ) : (
              <pre className="artifact-pre">{body}</pre>
            )
          ) : (
            <p className="hint">No body content (tier: {a.tier}).</p>
          )}
        </article>

        {versionRows.length > 0 && (
          <details className="versions-block">
            <summary>Versions ({versionRows.length})</summary>
            <table className="t">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Title</th>
                  <th>Created</th>
                  <th>Size</th>
                  <th>SHA</th>
                </tr>
              </thead>
              <tbody>
                {versionRows.map((v) => (
                  <tr key={v.artifact_id}>
                    <td>
                      <b>v{v.version}</b>
                    </td>
                    <td>{v.title}</td>
                    <td>{fmtRelative(v.created_at)}</td>
                    <td>{v.size_bytes != null ? fmtBytes(v.size_bytes) : "—"}</td>
                    <td>{v.sha256 ? <code>{v.sha256.slice(0, 8)}…</code> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </div>
    </div>
  );
}

function isHtmlish(contentType?: string | null, body?: string): boolean {
  if (contentType && /html/i.test(contentType)) return true;
  if (!body) return false;
  return /<!doctype html|<html[\s>]|<body[\s>]/i.test(body.slice(0, 400));
}

function isMarkdownish(contentType?: string | null, body?: string): boolean {
  if (!body) return false;
  if (contentType && /(markdown|md)/i.test(contentType)) return true;
  return /(^#|\n#|\*\*|```|^- )/m.test(body);
}

"""Content-type-aware rendering helpers.

- ``text/markdown`` → ``markdown-it-py`` → ``bleach.clean`` → HTML snippet.
- ``text/html``     → sandboxed iframe (via ``srcdoc``) so agent-written
  scripts can't escape into our origin or touch our cookies.
- ``text/plain``    → HTML-escaped ``<pre>`` block.
- ``image/*``       → inline ``<img>`` served from the viewer (proxied).
- anything else     → download link only.

The ``attachment:<filename>`` markdown shorthand is rewritten to point at
the viewer's attachment-stream route before markdown parsing, so the
image markdown (``![alt](attachment:foo.png)``) renders inline.
"""

from __future__ import annotations

import html
import re
from typing import Any

import bleach
from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True}).enable(["table", "strikethrough"])

# Allowed HTML tags for rendered markdown. Keep conservative — agents
# produce markdown; anything outside this list is stripped.
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "code",
        "del",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "ins",
        "kbd",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "samp",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "code": ["class"],
    "span": ["class"],
    "th": ["align"],
    "td": ["align"],
}
_ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto"})


def _rewrite_attachment_refs(md: str, artifact_id: str) -> str:
    """Rewrite ``attachment:filename`` → the viewer's attachment URL."""

    def sub(match: re.Match[str]) -> str:
        filename = match.group(1)
        # Basename guard — keep the rewritten URL safe regardless of input.
        safe = filename.replace("/", "").replace("\\", "")
        return f"/artifacts/{artifact_id}/attachments/{safe}"

    return re.sub(r"attachment:([^)\s]+)", sub, md)


def render_markdown(content: str, artifact_id: str) -> str:
    """Render agent-authored markdown into a sanitized HTML fragment."""
    rewritten = _rewrite_attachment_refs(content, artifact_id)
    rendered = _MD.render(rewritten)
    return bleach.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


def render_plain(content: str) -> str:
    """Render plain text as an escaped ``<pre>`` block."""
    return f'<pre class="plain">{html.escape(content)}</pre>'


def render_html_iframe(content: str) -> str:
    """Render agent-authored HTML inside a sandboxed iframe.

    The iframe uses ``srcdoc`` so the HTML never touches a URL Google
    would cache. ``sandbox`` without ``allow-same-origin`` / ``allow-scripts``
    means: no JS, no access to parent cookies, no form submission.
    ``allow-popups`` is kept so ``<a target="_blank">`` still works.
    """
    escaped = html.escape(content, quote=True)
    return (
        '<iframe class="artifact-html-frame" '
        'sandbox="allow-popups allow-popups-to-escape-sandbox" '
        f'srcdoc="{escaped}">'
        "</iframe>"
    )


def is_image(content_type: str | None) -> bool:
    return bool(content_type and content_type.lower().startswith("image/"))


def render_artifact_body(content: str, content_type: str, artifact_id: str) -> tuple[str, str]:
    """Dispatch rendering by MIME type.

    Returns ``(html_fragment, display_mode)`` where ``display_mode`` is a
    tag the template can use for per-mode styling: ``markdown``,
    ``html``, ``plain``.
    """
    ct = (content_type or "").lower()
    if ct.startswith("text/markdown"):
        return render_markdown(content, artifact_id), "markdown"
    if ct.startswith("text/html"):
        return render_html_iframe(content), "html"
    return render_plain(content), "plain"


def attachment_view_html(artifact_id: str, attachments: list[dict[str, Any]]) -> str:
    """Render attachment list: inline images, download links for everything else."""
    if not attachments:
        return ""
    parts = ['<div class="attachments"><h3>Attachments</h3><ul>']
    for att in attachments:
        filename = html.escape(str(att.get("filename", "")))
        ct = str(att.get("content_type") or "")
        size = att.get("size_bytes") or att.get("size") or 0
        url = f"/artifacts/{artifact_id}/attachments/{filename}"
        if is_image(ct):
            parts.append(
                f'<li><img src="{url}" alt="{filename}" class="attachment-img"/>'
                f'<div class="attachment-meta"><a href="{url}">{filename}</a> '
                f'<span class="mono">({ct}, {size} bytes)</span></div></li>'
            )
        else:
            parts.append(
                f'<li><a href="{url}">{filename}</a> <span class="mono">({html.escape(ct)}, {size} bytes)</span></li>'
            )
    parts.append("</ul></div>")
    return "".join(parts)

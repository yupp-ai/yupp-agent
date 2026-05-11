"""Unit tests for ``artifact_viewer.render``."""

from __future__ import annotations

from artifact_viewer import render

ART_ID = "11111111-2222-3333-4444-555555555555"


class TestMarkdown:
    def test_basic_headings_and_paragraphs(self) -> None:
        html = render.render_markdown("# Hi\n\npara", ART_ID)
        assert "<h1>Hi</h1>" in html
        assert "<p>para</p>" in html

    def test_code_block_renders_pre(self) -> None:
        html = render.render_markdown("```\nfoo\n```", ART_ID)
        assert "<pre>" in html
        assert "<code>" in html

    def test_script_tag_escaped(self) -> None:
        """markdown-it with html:false escapes raw HTML — script tags never execute."""
        html = render.render_markdown("hello <script>alert(1)</script>", ART_ID)
        # Rendered as text, not a live <script> tag.
        assert "<script" not in html.lower()
        assert "&lt;script&gt;" in html

    def test_javascript_url_sanitized(self) -> None:
        """markdown-it refuses javascript: URLs in link validation,
        so no ``<a href="javascript:...">`` tag is ever emitted. The
        literal text may survive (rendered as inert text inside <p>)."""
        html = render.render_markdown("[x](javascript:alert)", ART_ID)
        # The key guarantee: no clickable javascript href.
        assert 'href="javascript:' not in html.lower()
        assert "href='javascript:" not in html.lower()

    def test_attachment_shorthand_rewritten(self) -> None:
        html = render.render_markdown("![cat](attachment:pic.png)", ART_ID)
        assert f"/artifacts/{ART_ID}/attachments/pic.png" in html

    def test_attachment_shorthand_strips_path_separators(self) -> None:
        # ``attachment:../secret`` should not escape the artifact's attachment dir.
        html = render.render_markdown("![x](attachment:../secret)", ART_ID)
        assert "../secret" not in html
        assert f"/artifacts/{ART_ID}/attachments/..secret" in html

    def test_tables_allowed(self) -> None:
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        html = render.render_markdown(md, ART_ID)
        assert "<table>" in html
        assert "<td>1</td>" in html


class TestHtmlIframe:
    def test_returns_sandboxed_iframe(self) -> None:
        out = render.render_html_iframe("<h1>Hi</h1><script>alert(1)</script>")
        assert out.startswith("<iframe")
        assert "sandbox=" in out
        # Script tag is escaped inside srcdoc, so it can't execute when
        # the iframe also lacks allow-scripts in its sandbox.
        assert "allow-scripts" not in out

    def test_srcdoc_content_is_escaped(self) -> None:
        out = render.render_html_iframe('<p title="x">&</p>')
        # Double quote in the inner attribute must be HTML-escaped in srcdoc.
        assert "&quot;" in out
        # Original ampersand must be escaped too.
        assert "&amp;" in out

    def test_iframe_uses_shared_sandbox_flags_constant(self) -> None:
        # The full-page route's Content-Security-Policy header must use
        # the same sandbox flags as the iframe — guarding the constant
        # here keeps the two paths from drifting apart.
        out = render.render_html_iframe("<p>x</p>")
        assert f'sandbox="{render.HTML_SANDBOX_FLAGS}"' in out
        # Negative: never grant the dangerous flags accidentally.
        assert "allow-scripts" not in render.HTML_SANDBOX_FLAGS
        assert "allow-same-origin" not in render.HTML_SANDBOX_FLAGS


class TestPlain:
    def test_wraps_in_pre_and_escapes(self) -> None:
        out = render.render_plain("<script>")
        assert out == '<pre class="plain">&lt;script&gt;</pre>'


class TestDispatch:
    def test_markdown_dispatch(self) -> None:
        body, mode = render.render_artifact_body("# H", "text/markdown", ART_ID)
        assert mode == "markdown"
        assert "<h1>H</h1>" in body

    def test_html_dispatch(self) -> None:
        body, mode = render.render_artifact_body("<b>x</b>", "text/html", ART_ID)
        assert mode == "html"
        assert "<iframe" in body

    def test_plain_dispatch(self) -> None:
        body, mode = render.render_artifact_body("hi", "text/plain", ART_ID)
        assert mode == "plain"
        assert "<pre" in body

    def test_unknown_content_type_falls_through_to_plain(self) -> None:
        _, mode = render.render_artifact_body("hi", "application/x-weird", ART_ID)
        assert mode == "plain"


class TestAttachmentRendering:
    def test_image_renders_inline(self) -> None:
        html = render.attachment_view_html(
            ART_ID, [{"filename": "pic.png", "content_type": "image/png", "size_bytes": 123}]
        )
        assert "<img" in html
        assert f"/artifacts/{ART_ID}/attachments/pic.png" in html

    def test_non_image_as_link(self) -> None:
        html = render.attachment_view_html(
            ART_ID, [{"filename": "report.pdf", "content_type": "application/pdf", "size_bytes": 42}]
        )
        assert "<img" not in html
        assert "report.pdf" in html

    def test_empty_list_returns_empty_string(self) -> None:
        assert render.attachment_view_html(ART_ID, []) == ""

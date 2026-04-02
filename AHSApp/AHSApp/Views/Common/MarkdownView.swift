import SwiftUI
import WebKit

/// Renders full markdown (tables, code blocks, headers, etc.) using a self-sizing WKWebView.
struct MarkdownWebView: NSViewRepresentable {
    let markdown: String
    @Binding var dynamicHeight: CGFloat

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let webView = NonScrollingWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.setValue(false, forKey: "drawsBackground")
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        let html = Self.wrapInHTML(markdown)
        if context.coordinator.lastMarkdown != markdown {
            context.coordinator.lastMarkdown = markdown
            context.coordinator.heightBinding = $dynamicHeight
            webView.loadHTMLString(html, baseURL: nil)
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    class Coordinator: NSObject, WKNavigationDelegate {
        var heightBinding: Binding<CGFloat>?
        var lastMarkdown: String = ""

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            // Measure content height after render
            webView.evaluateJavaScript("document.body.scrollHeight") { result, _ in
                if let height = result as? CGFloat, height > 0 {
                    DispatchQueue.main.async {
                        self.heightBinding?.wrappedValue = height + 4
                    }
                }
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
    }

    private static func wrapInHTML(_ markdown: String) -> String {
        let escaped = markdown
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "`", with: "\\`")
            .replacingOccurrences(of: "$", with: "\\$")

        return """
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8">
        <style>
        :root { color-scheme: light dark; }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro", system-ui, sans-serif;
            font-size: 13px;
            line-height: 1.5;
            color: var(--text);
            background: transparent;
            padding: 0;
            overflow: hidden;
            -webkit-text-size-adjust: none;
        }
        @media (prefers-color-scheme: dark) {
            :root { --text: #f5f5f7; --code-bg: #2a2a2c; --border: #38383a; --table-alt: #1e1e20; }
        }
        @media (prefers-color-scheme: light) {
            :root { --text: #1d1d1f; --code-bg: #f4f4f4; --border: #e5e5ea; --table-alt: #f9f9f9; }
        }
        h1, h2, h3, h4 { margin: 0.6em 0 0.3em; font-weight: 600; }
        h1 { font-size: 1.3em; } h2 { font-size: 1.15em; } h3 { font-size: 1.05em; }
        p { margin: 0.4em 0; }
        ul, ol { padding-left: 1.5em; margin: 0.4em 0; }
        code {
            font-family: "SF Mono", Menlo, monospace;
            font-size: 0.9em;
            background: var(--code-bg);
            padding: 0.15em 0.35em;
            border-radius: 4px;
        }
        pre {
            background: var(--code-bg);
            padding: 10px 12px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 0.5em 0;
            line-height: 1.35;
        }
        pre code { background: none; padding: 0; }
        pre p { margin: 0; }
        table {
            border-collapse: collapse;
            width: 100%;
            margin: 0.5em 0;
            font-size: 0.92em;
        }
        th, td {
            border: 1px solid var(--border);
            padding: 5px 10px;
            text-align: left;
        }
        th { font-weight: 600; background: var(--code-bg); }
        tr:nth-child(even) { background: var(--table-alt); }
        blockquote {
            border-left: 3px solid var(--border);
            padding-left: 12px;
            margin: 0.4em 0;
            color: #8e8e93;
        }
        a { color: #4a90d9; }
        hr { border: none; border-top: 1px solid var(--border); margin: 0.6em 0; }
        </style>
        <script>
        function md(s) {
            // Extract code blocks first to protect them from inline transforms
            var codeBlocks = [];
            s = s.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, function(m, lang, code) {
                var i = codeBlocks.length;
                // Collapse multiple blank lines to single, trim trailing whitespace
                var clean = code.replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n{2,}/g,'\\n').replace(/^\\n|\\n$/g,'');
                codeBlocks.push('<pre><code class="' + lang + '">' + clean + '</code></pre>');
                return '%%CODEBLOCK' + i + '%%';
            });
            // Tables
            s = s.replace(/^\\|(.+)\\|\\s*\\n\\|[-| :]+\\|\\s*\\n((?:\\|.+\\|\\s*\\n?)*)/gm, function(m, h, body) {
                var ths = h.split('|').map(c => '<th>' + c.trim() + '</th>').join('');
                var rows = body.trim().split('\\n').map(r =>
                    '<tr>' + r.replace(/^\\||\\|$/g,'').split('|').map(c => '<td>' + c.trim() + '</td>').join('') + '</tr>'
                ).join('');
                return '<table><thead><tr>' + ths + '</tr></thead><tbody>' + rows + '</tbody></table>';
            });
            s = s.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
            s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
            s = s.replace(/^## (.+)$/gm, '<h2>$1</h2>');
            s = s.replace(/^# (.+)$/gm, '<h1>$1</h1>');
            s = s.replace(/^---+$/gm, '<hr>');
            s = s.replace(/\\*\\*\\*(.+?)\\*\\*\\*/g, '<strong><em>$1</em></strong>');
            s = s.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
            s = s.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
            s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
            s = s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank">$1</a>');
            s = s.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
            s = s.replace(/^[*-] (.+)$/gm, '<li>$1</li>');
            s = s.replace(/((?:<li>.+<\\/li>\\s*)+)/g, '<ul>$1</ul>');
            s = s.replace(/^\\d+\\. (.+)$/gm, '<li>$1</li>');
            // Paragraphs — skip lines that are already HTML or code block placeholders
            s = s.replace(/^(?!<[a-z\\/]|%%CODEBLOCK)((?!\\s*$).+)$/gm, '<p>$1</p>');
            // Restore code blocks
            for (var i = 0; i < codeBlocks.length; i++) {
                s = s.replace('%%CODEBLOCK' + i + '%%', codeBlocks[i]);
            }
            return s;
        }
        </script>
        </head>
        <body>
        <script>document.write(md(`\(escaped)`));</script>
        </body>
        </html>
        """
    }
}

/// WKWebView subclass that passes scroll events to the parent ScrollView.
class NonScrollingWebView: WKWebView {
    override func scrollWheel(with event: NSEvent) {
        // Pass scroll events to the next responder (parent ScrollView)
        nextResponder?.scrollWheel(with: event)
    }
}

/// Simple text fallback for user messages (no heavy markdown needed).
struct MarkdownText: View {
    let text: String

    var body: some View {
        Text(LocalizedStringKey(text))
            .textSelection(.enabled)
    }
}

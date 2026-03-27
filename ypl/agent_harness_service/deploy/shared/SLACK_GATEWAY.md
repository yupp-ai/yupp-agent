# Slack Gateway

Your session is triggered by an `@mention` in a Slack thread or channel. The `@mention` text is your first message — it does not include the rest of the thread. Follow-up `@mentions` in the same thread arrive as new messages in the same session.

Your system prompt includes a **"Slack Thread Context"** section with the channel ID and thread TS, and usually the full thread messages already pre-loaded. Read that section before responding — you don't need to tell the user you are reading the thread.

If the thread messages are **not** pre-loaded (the section says to call `read_slack_thread`), call it before responding:

```
read_slack_thread(channel="C01ABCDEF", thread_ts="1234567890.123456")
```

Also call `read_slack_thread` on follow-up turns when the user refers to something earlier in the thread, or if the thread has grown since the session started.

## Formatting for Slack

You are writing directly in Slack's mrkdwn format — not standard Markdown. The syntax is different. Use the reference below.

### Syntax Reference

| Element | Slack mrkdwn syntax |
|---|---|
| Bold | `*text*` |
| Italic | `_text_` |
| Strikethrough | `~text~` |
| Inline code | `` `text` `` |
| Code block | ` ``` ` on its own line before and after |
| Blockquote | `>` at line start |
| Link | See below |
| Bullet list | `•` or `-` at line start |

### Links

Slack link syntax uses angle brackets with a pipe separator (no backslash):

```
<https://example.com|Click here>
```

This renders as a clickable "Click here" link.

### Code Blocks

Slack code blocks use triple backticks on their own lines. There is no language hint support — do not add a language tag after the opening backticks.

```
this is a code block
no language tag on the opening line
```

### Things That Don't Work in Slack

- **No headings.** `#`, `##`, etc. render as plain text. Use `*Bold Text*` with blank lines above and below for section separation.
- **No tables.** Pipe-based tables render as garbled text. Wrap tabular data in a fenced code block as an ASCII table.
- **No numbered lists.** `1.` `2.` `3.` renders as plain text. Use bullet lists, or manually number with `1)`, `2)`, etc. as plain text.
- **No nested lists.** Slack flattens indentation beyond one level.
- **No images.** `![alt](url)` renders as plain text. Just paste the URL directly.

### General Guidelines

- Keep link labels short.
- For tabular data (tables, CSVs, query results), use a fenced code block with ASCII formatting. Avoid overly wide tables.
- Use blank lines generously between sections for visual breathing room.
- Prefer short paragraphs and bullet lists over walls of text.

## Tips

- Thread messages are returned in chronological order. Skim from the top to understand the full story.
- Look for error messages, stack traces, links to PRs/issues, and prior conclusions.
- If the thread is long, use the `limit` and `cursor` parameters to paginate.
- You can also use `search_slack` to find related threads or prior discussions on the same topic.

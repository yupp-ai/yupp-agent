---
name: summarize-thread
description: Summarize a Slack thread to get someone up to speed quickly. Detects thread type (incident, feature discussion, debugging, brainstorming, general) and produces a Slack-friendly summary with context, key points, open questions, and action items per person.
allowed-tools:
---

# Slack Thread Summarizer

Summarize the current Slack thread so a new person can get up to speed quickly. Read all messages in the thread, detect what kind of conversation it is, and produce a structured summary.

## Step 1: Detect Thread Type

Classify the thread into one of these types based on content signals:

| Type | Signals |
|---|---|
| *Incident* | alerts, errors, outages, rollbacks, pages, "is this affecting users?", status updates |
| *Feature Discussion* | proposals, PRs, design docs, tradeoffs, "should we...", specs, requirements |
| *Debugging* | stack traces, logs, "I'm seeing...", reproduction steps, root cause analysis |
| *Brainstorming* | open-ended questions, multiple ideas, "what if we...", no clear decision yet |
| *General* | anything that doesn't fit the above |

## Step 2: Produce the Summary

Use the template matching the detected thread type. Every summary must use Slack-native formatting (see formatting rules below).

### Incident Template

```
:rotating_light: *Incident Summary*

*What happened:* <one-sentence description of the incident>

*Impact:* <who/what was affected and severity>

*Timeline:*
• <HH:MM> — <event>
• <HH:MM> — <event>
• <HH:MM> — <event>

*Root cause:* <root cause if identified, or "Still under investigation">

*Resolution:* <what fixed it, or current status>

*Open Questions:*
• <question>

*Action Items:*
• @person — <action>
```

### Feature Discussion Template

```
:clipboard: *Feature Discussion Summary*

*Context:* <what feature or change is being discussed and why>

*Proposals:*
• <proposal 1 — who suggested it>
• <proposal 2 — who suggested it>

*Key Tradeoffs:*
• <tradeoff or concern raised>

*Decisions Made:*
• <decision, or "No final decision yet">

*Open Questions:*
• <question>

*Action Items:*
• @person — <action>
```

### Debugging Template

```
:mag: *Debugging Summary*

*Problem:* <what's broken or behaving unexpectedly>

*Symptoms:*
• <observable symptom>

*Investigation:*
• <what was tried and by whom>
• <what was ruled out>

*Root Cause:* <if found, otherwise "Still investigating">

*Fix:* <what fixed it or proposed fix>

*Open Questions:*
• <question>

*Action Items:*
• @person — <action>
```

### Brainstorming Template

```
:bulb: *Brainstorming Summary*

*Topic:* <what the group is exploring>

*Ideas Discussed:*
• <idea — who raised it>
• <idea — who raised it>

*Themes & Patterns:*
• <recurring theme or area of agreement>

*Open Questions:*
• <question>

*Action Items:*
• @person — <action>
```

### General Template

```
:speech_balloon: *Thread Summary*

*Context:* <what this thread is about>

*Key Points:*
• <point — who said it>
• <point — who said it>

*Decisions Made:*
• <decision, or "None yet">

*Open Questions:*
• <question>

*Action Items:*
• @person — <action>
```

## Formatting Rules

These rules are mandatory. Slack does not render markdown the same way as GitHub.

- Use `*bold*` for emphasis (not `**bold**`)
- Use `•` (bullet character) for list items (not `-` or `*`)
- Use `:emoji_name:` Slack emoji syntax sparingly — only for section headers
- Do NOT use `#`, `##`, or any markdown headers — Slack renders them as plain text
- Do NOT use markdown links `[text](url)` — Slack uses `<url|text>` but plain URLs auto-unfurl, so just paste URLs directly
- Keep lines short. Slack wraps awkwardly on mobile with long paragraphs
- Use blank lines between sections for readability
- Use `@person` when referencing people by their display name as shown in the thread

## Tone & Style

- Concise and neutral — report what was said, not what you think about it
- Factual — do not editorialize or add opinions
- Attribute key points and decisions to the person who made them
- If there is disagreement, present both sides without taking one
- Use present tense for current state, past tense for events

## Edge Cases

- *Very short thread (< 5 messages):* Still produce the summary structure, but keep it proportionally brief. Skip sections that have no content (e.g., no action items means omit that section)
- *Threads with lots of code:* Do not reproduce code blocks. Summarize what the code does or what change it represents
- *Threads with images/screenshots:* Note that images were shared and describe their apparent purpose if context clues exist (e.g., "screenshot of the error" or "mockup of the new design")
- *Threads with links:* Include important links inline but do not summarize linked content unless it was discussed in the thread
- *No clear action items:* Omit the Action Items section rather than writing "None"
- *No open questions:* Omit the Open Questions section rather than writing "None"

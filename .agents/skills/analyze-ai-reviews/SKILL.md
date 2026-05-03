---
name: analyze-ai-reviews
description: Analyze AI code review patterns across merged PRs in any GitHub repo. Auto-detects repo from git remote. Examines reviewer comments from AI bots (gemini-code-assist, yupp-reviews, copilot, etc.), resolution patterns, AI-fixer vs human-fixer behaviors, and generates a detailed summary report with charts. Posts the final report to artifact for sharing. Use for retrospectives, review quality assessment, and identifying common AI code-generation blind spots.
allowed-tools: Bash, Read, Write, Glob, Grep, Task, mcp__harness__add_artifact
---

# Analyze AI Reviews

Generate a comprehensive analysis of AI code review patterns across merged PRs in a GitHub repository.
Examines bot reviewer comments, how they get resolved, AI-fixer vs human-fixer dynamics, and
common code-generation blind spots.

## Usage

```
/analyze-ai-reviews                  # Analyze last 30 days (default), auto-detect repo
/analyze-ai-reviews 14               # Analyze last 14 days
/analyze-ai-reviews 60               # Analyze last 60 days
```

## Steps

### Step 0: Detect the repository

Auto-detect the GitHub owner and repo from the current git remote:

```bash
gh repo view --json nameWithOwner --jq '.nameWithOwner'
# Returns e.g. "yupp-ai/yupp-agent" → OWNER="yupp-ai", REPO="yupp-agent"
```

Use `OWNER/REPO` in all subsequent API calls. If detection fails, ask the user.

### Step 1: Fetch merged PRs

Get all merged PRs in the time window.

```bash
# Default: 30 days. Replace date with $ARGUMENTS days ago if provided.
gh pr list --repo <OWNER>/<REPO> --state merged \
  --search "merged:>=<DATE>" --limit 200 \
  --json number,title,author,mergedAt \
  --jq '.[].number' | sort -n
```

### Step 2: Fetch review data in parallel batches

Split PR numbers into 4 batches and use the **Task tool** to fetch review data in parallel.
Each batch agent should run these commands for every PR in its batch:

```bash
# Get reviews (who reviewed, state, body)
gh api --paginate repos/<OWNER>/<REPO>/pulls/{PR_NUMBER}/reviews \
  --jq '.[] | {user: .user.login, state: .state, body: .body}'

# Get inline review comments (code-level comments with diffs)
gh api --paginate repos/<OWNER>/<REPO>/pulls/{PR_NUMBER}/comments \
  --jq '.[] | {id: .id, user: .user.login, body: .body, path: .path, in_reply_to_id: .in_reply_to_id, created_at: .created_at}'
```

Each batch agent should:
1. Identify all comments from bot/AI reviewers (see **AI Reviewer Identification** below)
2. Identify resolution comments (replies from humans or AI fixers)
3. Classify AI fixer signatures (see **AI Fixer Signatures** below)
4. Return all data as a structured dump

### Step 3: Classify and count

From the collected data, compute:

#### Resolution outcomes
For each AI review comment, classify its resolution using semantic understanding
(the examples below are illustrative, not exhaustive — use judgment to classify):
- **Fixed** -- the reply indicates the issue was addressed
- **No response** -- no reply and no apparent fix
- **Dismissed** -- the reply indicates the suggestion was rejected or not applicable
- **Deferred** -- the reply acknowledges the issue but defers to a later change
- **Already fixed** -- the reply indicates it was already handled
- **Human escalated** -- a human reviewer separately agrees with the bot comment

#### Severity distribution (gemini only)
Count comments by severity tag: CRITICAL, HIGH, MEDIUM, LOW.

#### Issue categories
Do NOT use a fixed list of categories. Instead, derive the categories dynamically from the
actual comments collected in this analysis run. Read through all AI review comments, identify
recurring themes and patterns, then group them into 8-15 natural categories that reflect the
real distribution of issues found. Name each category concisely.

#### Dual-reviewer agreement
For issues flagged by multiple AI reviewers on the same PR, match by file path,
approximate line location, and semantic similarity of the issue description.
Two comments in the same file but about unrelated issues should not be considered
agreement. Track whether the fix rate differs for dual-reviewer flags vs single-reviewer.

#### AI fixer vs human fixer
Separate replies into:
- **AI fixer**: signed with known AI signatures (see below)
- **Human fixer**: no AI signature

Compare: response rate, dismissal rate, self-contradictions, verbosity.

### Step 4: Generate the report

Write a markdown file to `/tmp/ai-review-analysis.md` with the following structure.
Use ASCII bar charts with **short labels** and **explicit counts** on every bar.

```markdown
# AI Code Review Analysis: <REPO> (<date range>)

## Executive Summary
- Key finding (1 paragraph)
- TL;DR Findings (5-7 bullets with numbers)
- TL;DR Suggested Actions (3 bullets: for fixers, reviewers, team)

## 1. The Players
- AI Reviewers table (reviewer, provider, coverage, style)
- AI Fixers table (user, tool, signature)
- Human Fixers table (user, style)
- AI Fixer vs Human Fixer comparison chart + key differences narrative

## 2. Overall Stats & Patterns
- Volume at a glance
- Severity distribution chart
- Resolution outcomes chart
- Fix rate by agreement chart (dual vs single reviewer)
- Top 10 code-generation blind spots chart
- Reviewer comparison chart with overall scores and verdict

## 3. AI-to-AI Dynamics
<!-- Derive subsections dynamically from the anti-patterns actually observed
     in the data (e.g. dismissal patterns, self-contradictions, identity confusion).
     Do NOT use a hardcoded list of subsections. -->

## 4. Controversies & Disagreements
<!-- Derive subsections dynamically from the actual disagreements and friction
     points found in the data. Do NOT use a hardcoded list. -->

## 5. Issue Categories (Detail)
<!-- Derive sections dynamically from the categories discovered in Step 3.
     For each top category, show examples with PR links and explain why
     the issue matters. Do NOT use a hardcoded list. -->

## 6. What's Working Well
## 7. Recommendations
## Appendix: PRs with Most AI Review Activity
```

### Step 5: Publish to artifact

Upload the final report to artifact for sharing:

```
add_artifact(
    artifact_type="TEXT",
    title="AI review analysis — <REPO> <date-range>",
    content=<full markdown content>,
    named_slug="ai-review-analysis-<REPO>-<date-range>",
    create_new_slug=True,
)
```

Report the artifact link to the user.

## Chart Formatting Guidelines

All ASCII bar charts must follow these rules:

1. **Short labels** -- keep left-side labels under 20 characters to leave room for bars
2. **Counts on every bar** -- show the exact number right after the bar
3. **Percentages where applicable** -- show `(XX%)` after the count
4. **Consistent alignment** -- align all bars, counts, and percentages in columns

Good example:
```
  Fixed            ████████████████████████           120  (34%)
  No response      ████████████████████████████       140  (40%)
  Dismissed        ██████████                          35  (10%)
  Deferred         ████████                            25  ( 7%)
```

Bad example (labels too long, no counts):
```
  Fixed immediately          ████████████████░░░░░░░░░░░░░░
  Dismissed ("Not applicable") ██████░░░░░░░░░░░░░░░░░░░░░░
```

## AI Reviewer Identification

Identify AI reviewers by looking for bot accounts (usernames ending in `[bot]`) that post
code review comments. Common ones include:

### gemini-code-assist[bot]
- Style: Structured "Code Review" summaries with severity badges
- Severity icons: `critical`, `high`, `medium`, `low` (sometimes as image badges)
- Multiple reviews per PR (re-reviews after pushes)

### yupp-reviews[bot]
- Providers indicated by HTML comments:
  - `<!-- provider: codex -->` -- Codex-based, mechanical
  - `<!-- provider: claude -->` -- Claude-based, contextual
- Self-identifies with "I am an AI reviewer", "I'm an AI reviewer", "AI review:"
- Multiple reviews per PR (re-reviews after pushes)

### Other common AI reviewers
- `copilot[bot]` -- GitHub Copilot code review
- `coderabbitai[bot]` -- CodeRabbit AI reviewer

Auto-discover reviewers: scan the collected data for any `[bot]` users that post review
comments and include them in the analysis. Exclude CI-only bots (e.g. `github-actions[bot]`)
that post build/test status output rather than actionable code suggestions.

## AI Fixer Signatures

Identify AI-assisted replies by these common signatures in comment bodies:
- `-- Claude Code` or `-- Claude` suffix
- `-- Cursor` suffix
- `(AI reply)` prefix
- `I am an AI` prefix
- `Generated by` or `Co-authored-by` AI tool references
- Any other patterns discovered in the data

## Tips

1. **Parallelize data collection** -- 100+ PRs is a lot of API calls. Always split into 4+ batch agents.
2. **Rate limits** -- GitHub API has rate limits. If you hit them, add small delays between calls.
3. **Focus on inline comments** -- PR-level review summaries (the top-level "Code Review" comment) are less interesting than inline code comments with specific suggestions.
4. **Track reply chains** -- use `in_reply_to_id` to connect replies to their parent comments for resolution tracking.
5. **Don't double-count** -- AI reviewers may post multiple review rounds. Count unique issues, not review entries.
6. **Notebooks are noise** -- AI reviewers produce many low-value comments on `.ipynb` files. Note this pattern but don't let it skew the analysis.
7. **The report should be opinionated** -- give letter grades, call out anti-patterns, make concrete recommendations. A neutral summary of counts is not useful.
8. **Adapt everything** -- all categories, report sections, and anti-pattern groupings should be derived from the actual data. The template is a structural guide, not a checklist to fill in.

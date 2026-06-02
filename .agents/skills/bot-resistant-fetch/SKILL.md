---
name: bot-resistant-fetch
description: "Policy for fetching content from sites that block bots (Cloudflare, Akamai, DataDome, etc). Use whenever you would otherwise call WebFetch on a press-release / newsroom / corporate / blog / publisher URL. Defines a 3-layer fallback chain (RSS → r.jina.ai → paid escalation), a verified RSS source map for the publishers we actually scrape, and a HARD rule against guessing URLs from date patterns. Trigger before scraping any of: openai.com, x.ai, anthropic.com, salesforce IR, ServiceNow, Glean blog, arxiv listings, scworld.com, nextplatform.com, CISA bulletins, or any other enterprise publisher."
allowed-tools:
---

# Bot-Resistant Fetch

This skill exists because the AHS Performance Daily Digest for 2026-04-29 found WebFetch was the single highest-error tool in the system: 12 of 30 tool errors (8.2% of WebFetch calls) came from 403/404 responses on enterprise newsroom URLs. Almost all of them were avoidable.

**Use this skill** any time you would otherwise call `WebFetch` on:

- a press release, newsroom, corporate / IR / investor page
- a vendor blog (OpenAI, Anthropic, Glean, Salesforce, etc.)
- an arxiv listing or paper page
- a security bulletin or CVE writeup
- any URL where you do not already have a high-confidence direct slug

The naive WebFetch call against these targets gets bot-blocked (403 from Cloudflare / Akamai / DataDome) or hits a guessed-but-wrong slug (404). Both are recoverable. The rules below tell you how.

## The 3-Layer Fallback Chain

Always try the layers **in order**. Stop at the first layer that returns content.

### Layer 1 — RSS feed first (free, deterministic, no bot detection)

If the publisher is in the source map below, fetch the RSS feed instead of the human-readable page. RSS is structured, free, has zero bot detection, and gives you titles + dates + canonical URLs in one shot. Use the URLs returned by the feed for any deeper drill-down.

When the publisher is NOT in the source map, take 30 seconds to look one up:

- WordPress sites almost always expose `/feed/` or `/feed`. Try that first.
- Investor-relations sites (Q4 / S&P platforms) almost always expose `/rss/pressrelease.aspx` or similar. Look for an "RSS" link in the page footer.
- If you find a working feed during a session, **add it to this skill via a follow-up PR** so the next session does not have to re-discover it.

### Layer 2 — `r.jina.ai/<URL>` prefix (free, stealth headless)

If Layer 1 doesn't apply or doesn't have what you need, **prefix the original URL with `https://r.jina.ai/`** and call WebFetch on the prefixed URL.

Example:

```
# Layer 0 (will likely 403):
WebFetch(url="https://www.anthropic.com/news", prompt=...)

# Layer 2 (works — verified 2026-04-29):
WebFetch(url="https://r.jina.ai/https://www.anthropic.com/news", prompt=...)
```

Jina Reader runs a headless Chrome with stealth plugins under the hood and converts the rendered DOM to LLM-friendly Markdown. It's free at 20 requests / minute (200 RPM with a free API key). For the digest workload (~30 fetches/day) we can stay free indefinitely.

Pages where Layer 2 was verified to work on 2026-04-29 (each of these returned 403 via raw WebFetch the same day):

- `https://r.jina.ai/https://openai.com/news/` → 200
- `https://r.jina.ai/https://www.anthropic.com/news` → 200
- `https://r.jina.ai/https://www.glean.com/blog` → 200
- `https://r.jina.ai/https://newsroom.servicenow.com/press-releases/` → 200
- `https://r.jina.ai/https://www.scworld.com/` → 200

### Layer 3 — Paid web-unblocker API (escalation only)

Reach for this **only** if both Layer 1 and Layer 2 fail. As of 2026-04-29 we have not had to. The two best 2026 options if we ever do:

- **Bright Data Web Unlocker** — $1.50 / 1k successful requests, pay-as-you-go. ~98%+ success rate against hard targets. For ~30 fetches/day this is ~$1.35/month.
- **ZenRows Universal Scraper** — $69/mo unified plan, 99.93% claimed success, no KYC.

A new MCP tool will be added when (if) we cross this threshold. Until then, do not try to integrate paid scrapers ad-hoc.

## Verified RSS source map

Maintain this map as the canonical list. Add new entries via PR; do not memorize them in agent prompts.

| Publisher | RSS feed URL | Verified | Notes |
| --- | --- | --- | --- |
| OpenAI | `https://openai.com/news/rss.xml` | 2026-04-29 | Official |
| Salesforce IR / press releases | `https://investor.salesforce.com/rss/pressrelease.aspx` | 2026-04-29 | Official |
| arXiv cs.AI | `https://rss.arxiv.org/rss/cs.AI` | 2026-04-29 | Official. Construct other categories by replacing `cs.AI` with `cs.CL`, `cs.LG`, etc. Updates daily at 00:00 ET; weekends are usually empty. |
| The Next Platform | `https://www.nextplatform.com/feed/` | 2026-04-29 | WordPress default |

Publishers WITHOUT working official RSS as of 2026-04-29 (use Layer 2 instead):

- Anthropic (no `/rss`, no `/feed`)
- xAI / x.ai
- ServiceNow newsroom (the corporate `/feed/` 403s; use `r.jina.ai` on `https://newsroom.servicenow.com/press-releases/`)
- Glean blog
- SC World (the `/feed` path 403s)
- CISA bulletins (CISA killed RSS in May 2025; subscribe via GovDelivery email or use Layer 2)

## HARD rules

These are non-negotiable. Violating them is what created today's error budget.

1. **Never guess a URL from a date pattern.** Examples of forbidden guessing:
   - `https://arxiv.org/list/cs.AI/2604` — invented from "April 2026". The real listing is `https://rss.arxiv.org/rss/cs.AI`.
   - `https://www.nextplatform.com/2026/04/28/microsoft-and-openai-...` — slug invented from a topic guess.
   - `https://www.glean.com/blog/glean-waldo-agentic-search-model` — slug invented from a product name.
   - `https://www.cisa.gov/news-events/bulletins/sb26-117` — bulletin number extrapolated from a sequence.

   The agent **must** discover the canonical URL by either (a) reading it from an RSS feed or (b) reading it from a search result. If neither is available, drop the item.

2. **Never retry a 403 with the same raw URL.** A 403 from a corporate site is bot detection, not a transient network error. Retrying does nothing. Move to Layer 2 immediately.

3. **Never retry a 404 with a "fixed" slug guess.** A 404 means the slug you guessed does not exist. Trying a *different* guess is still guessing. Move up to the index page (Layer 1 or Layer 2) and rediscover the slug from the listing.

4. **Never give up on the item silently.** If a source you care about returned 403/404 from raw WebFetch, walk the layer chain and report the final result — including "could not retrieve via any layer" if all three fail. Do not pretend you got the article.

5. **Cap retries at one per layer.** A bad target burns the whole budget. If Layer 2 also fails, accept it and continue with the items you do have.

## Quick reference (for agents in a hurry)

```
# Step 1 — check source map for the publisher
# Step 2 — if mapped: fetch the RSS, find canonical URLs, fetch those (with Layer 2 if non-mapped)
# Step 3 — if NOT mapped: prefix URL with https://r.jina.ai/
# Step 4 — if Layer 2 also fails: drop the item, log it, move on

# Concretely:
WebFetch(url="https://r.jina.ai/" + raw_url, prompt=...)
```

## Provenance

This skill was created on 2026-04-29 in response to the AHS Performance Daily Digest finding that 12 of 30 tool errors that day (8.2% of all WebFetch calls) were 403/404 on enterprise newsroom URLs that the agent could have reached via RSS or `r.jina.ai`. See artifact `webfetch-403-fix-layered-approach` for the underlying research and tool comparison.

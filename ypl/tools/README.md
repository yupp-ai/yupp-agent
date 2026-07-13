# `ahs-artifact`

Command-line client for the AHS textual-artifact API. Wraps the REST
endpoints at `/ahs/artifacts` so you can create, read, search, and
archive artifacts (markdown/text/html pastes, optionally with sibling
attachments) from a shell.

## Install

The CLI is wired as a `[project.scripts]` entry in `pyproject.toml`, so
installing the package exposes it on `$PATH`:

```bash
pip install -e .
# or
poetry install
```

Verify:

```bash
ahs-artifact --help
```

## Configure

Two environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AGENT_HARNESS_SERVICE_API_KEY` | yes | — | Shared secret; sent as `X-API-Key` header |
| `AHS_BASE_URL` | no | `https://ahs.example.com` | Override for staging / localhost |

```bash
export AGENT_HARNESS_SERVICE_API_KEY=...
# optional — for local dev against a mono_server running on :8090:
export AHS_BASE_URL=http://localhost:8090
```

## Commands

### `add` — create an artifact

```bash
ahs-artifact add [FILE] [--slug NAME] [--title T] [--type TYPE] [--new-slug]
```

- **Content source:** a file path, or stdin (omit `FILE` or pass `-`).
- **Content type:** sniffed from the file extension — `.md`/`.markdown`
  → `text/markdown`, `.html`/`.htm` → `text/html`, anything else →
  `text/plain`. Stdin defaults to `text/markdown`. Override with
  `--type md|markdown|html|plain`.
- **Title:** defaults to the filename stem (or `stdin`). When `--title`
  is omitted and stdin is a TTY, the CLI prompts interactively. Pass
  `--title` in scripts to skip the prompt.
- **Slug:** `--slug NAME` auto-appends a new version if the slug exists,
  or creates it if it doesn't. `--new-slug` forces a fresh slug and
  errors if it's already in use.

Examples:

```bash
# Upload a markdown file, title prompted
ahs-artifact add notes.md

# Non-interactive with explicit title
ahs-artifact add report.md --title "Q1 Incident Report"

# From stdin (defaults to markdown)
echo "# hello" | ahs-artifact add --title hello

# Versioned slug — first call creates, subsequent calls append v2, v3, …
ahs-artifact add report.md --slug q1-incident --title "Q1 incident"
ahs-artifact add report.md --slug q1-incident --title "Q1 incident v2"

# Force a fresh slug; fails if 'foo' already exists
ahs-artifact add file.md --slug foo --new-slug --title "fresh"
```

`add` prints metadata to **stderr** and the public URL to **stdout** so
you can pipe the URL into clipboard tools:

```bash
ahs-artifact add notes.md --title N | pbcopy     # macOS
ahs-artifact add notes.md --title N | xclip -sel c  # Linux
```

### `get` — fetch content + metadata

```bash
ahs-artifact get (UUID | SLUG) [-v VERSION] [-q | -m]
```

- UUID → fetched directly; SLUG → latest non-archived version by default.
- `-v N` pins a specific version when fetching by slug.
- `-q` / `--content-only` → suppress the metadata header (clean pipes).
- `-m` / `--meta-only` → skip content, metadata only.

```bash
ahs-artifact get q1-incident                 # latest
ahs-artifact get q1-incident -v 2            # pinned
ahs-artifact get q1-incident -q > out.md     # content only
ahs-artifact get q1-incident -m              # metadata only
```

Default output: metadata on stderr, content on stdout, so `> file.md`
captures only the artifact body.

### `rm` — archive

```bash
ahs-artifact rm (UUID | SLUG)
```

- UUID → archives a single artifact. Prompts `y/N`.
- SLUG → archives **every non-archived version** under that slug. Lists
  them first, then asks you to type `delete` to confirm.
- The CLI refuses to act without a TTY, so there is no accidental
  scripted deletion — if you truly need non-interactive archive, call
  the REST endpoints directly.

Archive only flips `is_archived=true` in the metadata; the blobs are
retained and can still be read by UUID. A sweep process handles real
deletion later.

### `ls` — browse

```bash
ahs-artifact ls [--limit N] [--offset N] [--include-archived]
```

Reverse-chronological. Default `--limit 50`. Archived artifacts are
hidden unless `--include-archived` is set.

### `versions` — version history

```bash
ahs-artifact versions SLUG
```

Ascending by version. Shows archived versions inline.

### `url` — copy-pasteable URL

```bash
ahs-artifact url (UUID | SLUG)
```

Prints a single line to stdout. Useful for piping into clipboard tools
or shell aliases.

### `search` — find by title / slug / attachment filename

```bash
ahs-artifact search QUERY [--limit N] [--offset N] [--include-archived]
```

Case-insensitive substring match against `title`, `description`,
`named_slug`, and attachment filenames. No body search yet — that needs
a `tsvector` column. Reverse-chronological.

```bash
ahs-artifact search "q1 incident"
ahs-artifact search screenshot.png       # finds artifacts with an attachment of that name
```

## Output conventions

| Stream | Contents |
|---|---|
| **stdout** | The raw artifact content (`get`), the public URL (`add`, `url`), or the table body (`ls`, `versions`, `search`). Always pipe-safe. |
| **stderr** | Metadata headers, table headers, prompts, progress, and error messages. |

Exit codes: `0` on success, `1` on CLI / server / validation errors,
`2` on network errors, `130` on `Ctrl-C`.

## Troubleshooting

- **`AGENT_HARNESS_SERVICE_API_KEY is not set`** — export the shared
  secret from the VM's `.env`.
- **HTTP 403 Invalid API key** — the key you exported doesn't match the
  server's. Keys are distinct per environment (prod, staging, local).
- **HTTP 422 on add with slug** — slug must be `[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}`.
- **HTTP 400 "already has active versions"** — you passed `--new-slug`
  on a slug that already exists. Drop `--new-slug` to append a new
  version instead.

## See also

- `ypl/agent_harness_service/artifact_routes.py` — REST endpoints this
  CLI wraps.
- `ypl/agent_harness_service/artifact_store.py` — service layer.
- `ypl/mcp_server/tools/agent_artifacts.py` — the same capabilities
  exposed as MCP tools for agent use.

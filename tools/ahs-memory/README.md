# ahs-memory

Bulk-import a local Markdown workspace into AHS as `scope=user`
`MEMORY` artifacts via the AHS REST API. Use this when migrating a
notes / OpenClaw / ClawBot workspace into AHS so personal agents can
grep / search / `_always_inject` from it.

Self-contained sub-package — has its own `pyproject.toml` and does not
import from the AHS monorepo. The slug rules in `ahs_memory/slug.py`
are vendored from `ypl/agent_harness_service/memory_slug.py` (T1) and
must be kept in sync.

## Install

From the repo root:

```bash
pip install ./tools/ahs-memory
# or for development:
pip install -e './tools/ahs-memory[dev]'
```

The package registers an `ahs-memory` console script. You can also run
the package directly: `python -m ahs_memory ...`.

## Config

Read in order (later wins): defaults → `~/.ahs/config.toml` → env vars
→ CLI flags.

| Setting     | Env var       | Config key | Default                   |
| ----------- | ------------- | ---------- | ------------------------- |
| API URL     | `AHS_API_URL` | `api_url`  | `https://ahs.agcouch.com` |
| Service key | `AHS_API_KEY` | `api_key`  | _(required for push)_     |
| User ID     | `AHS_USER_ID` | `user_id`  | _(required for push)_     |

The CLI sends `X-API-Key: <api_key>` + `X-User-ID: <user_id>` on every
request. The route layer uses `X-User-ID` to authorize the user-scope
MEMORY write.

Example `~/.ahs/config.toml`:

```toml
api_url = "https://ahs.agcouch.com"
api_key = "ahs_..."
user_id = "11111111-2222-3333-4444-555555555555"
```

## Subcommands

### `ahs-memory walk PATH`

Dry-run: walk a workspace, normalize each `*.md` path into a slug,
print a table of `(path, slug, size, status)`. No network. Useful for
sanity-checking the slug list before pushing.

```bash
ahs-memory walk fixtures/sample-workspace
ahs-memory walk ~/openclaw --prefix openclaw/ --exclude 'archive/*'
```

### `ahs-memory push PATH --user-id ID`

For each `*.md` file, `POST /ahs/artifacts` with `type=MEMORY`,
`scope=user`, `subject=ID`. Per-file outcome:

- `created` — first version of a new slug.
- `new-version` — appended to an existing slug.
- `skipped-unchanged` — `--skip-unchanged` was set and the SHA-256 of
  the local body matches the latest server version.
- `skipped-oversize` — local file is larger than `--max-bytes`.
- `skipped-unsafe` — slug fails `is_safe_slug` (typically the path
  normalized to nothing or to a value with traversal segments).
- `error` — a non-2xx from AHS or a local read failure. Detail is
  reported per-row and the process exits non-zero so CI can flag it.

```bash
ahs-memory push fixtures/sample-workspace --user-id $AHS_USER_ID
ahs-memory push ~/openclaw --user-id $AHS_USER_ID --prefix openclaw/ --skip-unchanged
```

### `ahs-memory diff PATH --user-id ID`

3-way diff of the local workspace against the user's `scope=user`
MEMORY rows. Statuses: `local-only`, `changed`, `remote-only`,
`unchanged`. Useful before a push to confirm nothing's already there
that would collide.

```bash
ahs-memory diff ~/openclaw --user-id $AHS_USER_ID --prefix openclaw/
```

## Slug rules

Vendored from T1; see `ahs_memory/slug.py`:

- Lower-cased, `\` → `/`, trailing `.md` stripped.
- Any char outside `[A-Za-z0-9_./-]` → `-`. Runs of dashes / slashes
  collapse to one.
- Each `/`-segment is stripped of leading/trailing `-.`; empty
  segments are dropped (defense against `./hidden` and `../escape`).
- Optional `--prefix` is normalized through the same pipeline and
  prepended.

## Testing

```bash
# Unit tests (no network)
pytest tools/ahs-memory

# Integration test against a real AHS
AHS_INTEGRATION=1 \
AHS_API_URL=http://localhost:8090 \
AHS_API_KEY=$AGENT_HARNESS_SERVICE_API_KEY \
AHS_USER_ID=<test-user-id> \
pytest tools/ahs-memory/tests/test_integration_local_ahs.py
```

The integration test creates artifacts under
`ahs_memory_e2e/<unix_ts>/...` and archives them in `finally`.

---
name: clone-new-repo
description: >-
  Clone a new repository into the shared repos directory so every agent session
  can read it. Asks whether to persist the entry to shared_repos.yaml (via PR)
  for future VMs. Use when a user asks to make a new repo available to agents,
  add it to the auto-pull list, or share it across sessions.
  Usage: /clone-new-repo <github_url>
---

# Clone a New Shared Repo

Make a new repository available to every agent session on this VM by cloning
it into `/data/ahs/repos/`. Optionally persist it to `shared_repos.yaml` so
it survives VM recreation and is auto-pulled every 5 minutes on every VM.

## Arguments

- `$ARGUMENTS` — the GitHub HTTPS URL of the repo to clone
  (e.g. `https://github.com/wangtian24/pr-status-check`).

Accepted URL shapes:

- `https://github.com/{owner}/{repo}`
- `https://github.com/{owner}/{repo}.git`

Anything else (SSH `git@…`, GitLab, BitBucket) is rejected by `add_shared_repo`.

## Auth constraints

- **Public repos:** any owner works.
- **Private repos:** only the `yupp-ai` org is supported — that's what the
  VM's GitHub App is installed on. Private repos under other owners fail
  at clone time with an auth error.

If the user asks to clone a private non-yupp-ai repo, decline up front and
explain — don't attempt the clone.

## Steps

### 1. Validate the URL

Parse `$ARGUMENTS` and confirm it matches `https://github.com/{owner}/{repo}`.
If it doesn't, tell the user the accepted shapes and stop.

### 2. Clone on this VM

Call `add_shared_repo`:

```
add_shared_repo(url="https://github.com/wangtian24/pr-status-check")
```

Possible results:

- `{"status": "cloned", "path": "/data/ahs/repos/{name}", ...}` — success.
- `{"status": "exists", "path": "..."}` — already cloned; report and skip
  to step 4 (still ask about persisting in case the YAML is missing it).
- `{"status": "error", "error": "..."}` — surface the error and stop. Common
  causes: private repo outside `yupp-ai/*`, network error, name collision
  with a non-git directory.

### 3. Confirm the clone

Tell the user:

> Cloned `{name}` into `/data/ahs/repos/{name}` on this VM. Available to
> every session starting from the next one.

### 4. Ask whether to persist for future VMs

Use `AskUserQuestion`:

> **Persist `{name}` to `shared_repos.yaml`?**
> If yes, I'll open a PR adding it so every future VM (including this one
> after recreation) auto-clones it on the next pull tick (every 5 min).
> If no, the clone only exists on this VM and is lost on VM recreation.

Options:

1. **Yes, open a PR** — go to step 5.
2. **No, just this VM** — skip to step 7.

### 5. Open a PR adding the entry

Only run this branch if the user picked "Yes" in step 4.

1. `request_write_access(repo="yupp-agent", branch="ahs/{agent_name}/add-repo-{name}")`
2. Edit `ypl/agent_harness_service/deploy/shared_repos.yaml` in the worktree
   and append:

   ```yaml
     - name: {name}
       url: {url}
   ```

   Do **not** set `protected: true` — that field is reserved for repos the
   harness itself depends on (currently only `yupp-agent`). If the user
   explicitly asks for protection, ask them to confirm again, since it
   means the repo can never be removed via `remove_shared_repo`.

3. Commit with message: `[AHS] Add {name} to shared_repos.yaml`
4. `create_pr` using the `/create-pr` skill conventions. Suggested title:
   `[AHS] Add {name} to shared_repos.yaml`. Body should include:
   - Why the repo is being added (paraphrase the user's reason, or "Requested
     by {user} via /clone-new-repo" if no reason was given).
   - Clone URL.
   - That the entry is auto-picked-up by `pull_repos.py` on every VM's next
     pull tick after merge.

### 6. Report the PR URL

Per the `create_pr` rules: always surface the PR URL as a clickable link
in your next user-facing message.

### 7. Wrap up

- If you opened a PR (step 5): "Done. Cloned on this VM now; PR opened to
  persist for future VMs: {link}"
- If the user said no in step 4: "Done. Cloned on this VM. Note: this
  clone will be lost if the VM is recreated — re-run `/clone-new-repo` if
  that happens."

## Removing a repo

Out of scope for this skill — use the `remove_shared_repo` MCP tool
directly. It refuses to remove protected entries (currently `yupp-agent`),
and the removal only affects the current VM. If the repo is also in
`shared_repos.yaml`, the next pull tick will re-clone it; to permanently
remove it, also delete the entry from the YAML via a PR.

## Examples

### Public repo, persist

```
User: /clone-new-repo https://github.com/wangtian24/pr-status-check

Steps:
1. add_shared_repo(url=...) → cloned at /data/ahs/repos/pr-status-check
2. AskUserQuestion → user picks "Yes, open a PR"
3. request_write_access → worktree at .../yupp-agent-add-repo-pr-status-check
4. Edit shared_repos.yaml, commit, create_pr
5. Report: "Done — cloned and PR #1234 opened."
```

### Already cloned

```
User: /clone-new-repo https://github.com/wangtian24/pr-status-check
(another agent already cloned it earlier)

Steps:
1. add_shared_repo(...) → {"status": "exists", ...}
2. AskUserQuestion → user picks "Yes" (because YAML doesn't have it yet)
3. PR opens as above.
```

### Private outside yupp-ai

```
User: /clone-new-repo https://github.com/some-other-org/closed-repo

Steps:
1. Decline: "The VM's GitHub App is installed on yupp-ai only — private
   repos under some-other-org can't be cloned. Move the repo to yupp-ai
   or make it public and retry."
```

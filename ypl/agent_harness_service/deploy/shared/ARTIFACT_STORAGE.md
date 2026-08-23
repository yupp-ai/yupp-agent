# Artifact Storage — AL arti is the default

There are **two** artifact stores. Always name them explicitly so nobody
(you, a teammate, a future session) confuses them:

| Name | Host | Reach it via | Use it for |
|------|------|--------------|------------|
| **AL arti** (AngelList arti) | `arti.voltcouch.com` | the **`arti` MCP server** tools (`add_artifact`, `read_artifact`, `list_artifacts`, `search_artifacts`, `append_artifact`, `update_artifact`, `archive_artifact`, `get_artifact`, `list_artifact_versions`) | **Everything new — this is the default for all reads and writes.** |
| **Yupp artifact** (legacy) | `a.voltcouch.com` | the built-in harness tools (`add_artifact`, `update_artifact_content`, `read_artifact`, `search_artifacts`, `artifact_url`, …) | **Reading pre-migration history only.** Do not write here. |

## Rules

1. **Write to AL arti.** Every new artifact — report, investigation, review
   pointer, deliverable — is created and versioned in **AL arti**, using the
   **`arti` MCP server**'s tools. Do not create new Yupp artifacts.
2. **Read from AL arti first.** When you look something up, search/read **AL
   arti**. Only fall back to the **Yupp artifact** store (the built-in
   `search_artifacts` / `read_artifact`) when you need older history that has
   not been migrated yet.
3. **Name the store when you talk about it.** Say "saved to AL arti" / "found
   in the Yupp artifact archive", never a bare "artifact", so it is always
   clear which store you mean.
4. **Announce the URL of anything you save**, exactly as for a PR: after an AL
   arti write, surface the artifact's `arti.voltcouch.com` URL to the user.

## Tool-name collision — read carefully

Both stores expose a tool literally named `add_artifact`. They are different:

- **`arti` MCP server → `add_artifact`** writes to **AL arti** ← use this.
- the **built-in / harness `add_artifact`** writes to the **Yupp artifact**
  store ← legacy, do not use for new work.

If your runtime namespaces MCP tools (e.g. `mcp__arti__add_artifact`), the
`arti`-prefixed one is AL arti. When in doubt, prefer the tool that comes from
the **`arti`** MCP server.

> Agent **memories** (the `search_memory` / memory tools) are a separate system
> and are **not** covered by this rule — keep using the memory tools as before.

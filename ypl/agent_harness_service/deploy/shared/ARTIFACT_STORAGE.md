# Artifact Storage — everything is AL arti

Your built-in artifact tools now read and write **AL arti** (AngelList arti,
`arti.voltcouch.com`) directly. There is nothing extra to do and no second tool
set to choose between — just use the tools you already know:

- `add_artifact`, `update_artifact_content`, `update_artifact`
- `read_artifact`, `search_artifacts`, `list_artifacts`, `list_artifact_versions`
- `artifact_url`, `archive_artifact`, `archive_artifact_slug`

They all operate on **AL arti**. Every artifact you create gets an
`arti.voltcouch.com` URL — announce it to the user just like a PR link.

## What changed (and what you don't need to think about)

- The **legacy Yupp artifact** store (`a.voltcouch.com`) is no longer written.
  It stays online read-only, and `read_artifact` / `artifact_url` **fall back
  to it automatically** for older artifacts that predate the move — so old IDs
  and slugs still resolve. You never choose a store; the tools do the right thing.
- Naming, if you need to refer to them: **AL arti** = the live store (AngelList,
  `arti.voltcouch.com`); **Yupp artifact** = the read-only legacy archive
  (`a.voltcouch.com`). Born at AngelList and Yupp respectively.

## Notes

- Writes are attributed to the connecting user when they've linked arti;
  otherwise to a shared service identity. Either way the write succeeds.
- Attachments and non-text package uploads are not yet supported through these
  tools on AL arti — stick to TEXT `content` for now.
- Agent **memories** (`search_memory` and the memory tools) are a separate
  system and are unaffected — keep using them exactly as before.

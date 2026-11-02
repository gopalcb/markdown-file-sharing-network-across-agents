# Markdown sharing project instructions

This repository is the authoritative source for the Work Organizer and
Markdown-sharing application. Apply future MCP, document API, watcher-contract,
storage, authentication, associated Telemetry UI, and infrastructure changes here.
Do not update the historical Softrobots copy in parallel unless the user explicitly
requests a synchronization task.

Read `README.md`, `ARCHITECTURE.md`, `code-map.yaml`, and applicable nested files
before consequential changes. Use the shared GB Systems `CODEX_HOME` skills and
workflows. Use `.codex/skills/telemetry-document-delivery/SKILL.md` for MCP upload,
native uploader authentication, root watcher, receipt, and reconciliation work.
Use `python-development` for Python changes.

Keep this public repository free of credentials, tokens, OTPs, device keys, private
runtime configuration, generated interaction capture, deployment receipts, and
production data. Do not copy historical Softrobots task evidence here. Public
health checks and fixture tests do not prove private authentication or persistence.

GitHub Actions are validation-only. Do not deploy or add cloud credentials without
an explicit reviewed request that defines repository trust, permissions, ownership,
cost, rollback, and live acceptance. Local AWS commands are read-only unless the
user separately authorizes a bounded mutation.

Work on a branch, inspect the final diff, run focused checks, push to
`gopalcb/markdown-file-sharing-network-across-agents`, and open a PR. Do not push
directly to `main` or merge without required checks and independent approval.

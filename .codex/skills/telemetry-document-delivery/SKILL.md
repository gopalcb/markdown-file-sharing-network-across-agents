---
name: telemetry-document-delivery
description: Safely deliver Markdown through Softrobots Telemetry using the save_markdown MCP tool or the project root watcher, handle private uploader authentication, validate READY receipts, and reconcile uncertain writes. Use for document upload, Work Organizer authentication, watcher operation, MCP availability, or upload receipt/read-back questions in this repository.
---

# Telemetry Document Delivery

Use the supported adapter that matches the request. Keep authentication private and
do not treat a successful HTTP response or a pending local file as proof of storage.

## Choose the delivery path

1. Use the MCP `save_markdown` tool when it is available in the current session and
   the user asks for MCP delivery or supplies content to upload through MCP.
2. Use the project root `watcher.py` for project-local Markdown scanning, continuous
   delivery, a bounded scan, or native uploader authentication.
3. Do not handcraft calls to `/documents` or `/browser-documents`. Those routes
   require trusted gateway identity and exact registered-device proof. The watcher
   is the supported native client.
4. If neither the MCP tool nor the installed watcher runtime is available, report
   the missing capability. Do not invent configuration, credentials, or a receipt.

Read [references/mcp-api.md](references/mcp-api.md) before an MCP upload or when
diagnosing MCP/API behavior. Read [references/watcher.md](references/watcher.md)
before watcher authentication, startup, scanning, status, or reconciliation.

## Protect authentication material

- Keep passwords and email OTPs in the private terminal prompt used by the human.
- Never request, echo, log, copy, or store passwords, OTPs, access or refresh
  tokens, device keys, signed headers, Keychain data, or private runtime config.
- Do not confuse MCP OAuth, native uploader Cognito clients, and portal browser
  sessions. They are separate trust boundaries.
- Public health or OAuth discovery checks do not prove private authentication or
  successful storage.

## Require delivery evidence

For MCP, require `success: true` and inspect every returned file. For either
delivery path, require an exact receipt with:

- `status: READY`;
- a nonempty `file_id`;
- the expected SHA-256 and canonical path when the adapter returns them; and
- `storage_layout: project-path-v1` where that field is returned.

When a linked task is returned, verify its ID matches the file identity. A 200,
201, or 207 status alone is not enough. Do not claim authorized read-back, S3
persistence, or portal visibility unless that separate verification was executed.

## Preserve uncertain outcomes

If a transport or persistence result is uncertain, stop automatic retries. Preserve
the watcher ledger or MCP receipt details, reconcile the known file identity and
remote state, and retry only after the outcome is known. Never clear watcher state
to force another upload.

## Report the result precisely

State which adapter ran, which files received valid READY receipts, and which
checks were not run. Distinguish local scan results, authenticated upload receipts,
authorized read-back, and live portal visibility.

# MCP and Document API Contracts

## MCP upload

The Telemetry MCP exposes the `save_markdown` tool. Its input is one batch:

```json
{
  "payload": {
    "username": "gopalcbala",
    "file-group": "workflow-docs",
    "files": [
      {
        "name": "2026-11-02-example__SUMMARY.md",
        "content": "# Summary\n",
        "metadata": {
          "project": "softrobots-systems",
          "relative_path": "tasks/example"
        }
      }
    ]
  }
}
```

Follow the callable tool schema shown in the active session if its wrapper differs.
The server contract permits 1-10 files, Markdown basenames up to 120 characters,
256 KiB per UTF-8 file, and 2 MB per batch. Extra fields are rejected.

`metadata.relative_path` is bounded editorial metadata. It does not choose the
storage destination. MCP storage derives the canonical path from the file group and
basename, currently `docs/<file-group>/<name>`, and returns a
`project-path-v1` receipt.

The response has this shape:

```json
{
  "success": true,
  "files": [
    {
      "name": "2026-11-02-example__SUMMARY.md",
      "status": "READY",
      "file_id": "...",
      "sha256": "...",
      "storage_layout": "project-path-v1",
      "relative_path": "docs/workflow-docs/2026-11-02-example__SUMMARY.md"
    }
  ]
}
```

Validate the actual returned fields; do not manufacture missing evidence. MCP
reports `success: true` only when every file reaches `READY`. A failed or incomplete
receipt must be reported as incomplete, and an uncertain storage error must be
reconciled before retrying.

## Authentication boundaries

- MCP uses username-pool OAuth, a write scope, and an allowed authenticated subject.
- `/documents` is the family-pool native uploader route.
- `/browser-documents` is the username-pool native uploader route.
- Portal `/api/documents` requests use the portal browser session and CSRF boundary.

Do not transfer credentials or assumptions between these boundaries. Do not give
raw `curl` examples for native routes: the trusted gateway context and device
signature are part of the contract and must be produced by the supported client.

## Read-back limits

The MCP currently provides `save_markdown`; it does not provide a document read
tool. A READY receipt proves the shared storage coordinator acknowledged that
version. Authorized portal/API inspection or exact storage-version verification is
a separate step. State it as unrun unless it was actually performed with an
authorized read path.

## Source of truth

When behavior changes, verify these files before updating this reference:

- `telemetry-api/mcp_api/validation_service.py`
- `telemetry-api/mcp_api/app.py`
- `telemetry-api/mcp_api/storage_service.py`
- `telemetry-api/document_api/s3_document_service.py`
- `telemetry-api/services/native_upload_service.py`
- `ARCHITECTURE.md`

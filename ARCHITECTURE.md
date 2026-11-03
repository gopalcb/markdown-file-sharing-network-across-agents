# Architecture

## Runtime and ownership

`telemetry-api/server.py` dispatches Flask portal routes and authenticated FastMCP
routes in one Lambda image. Browser sessions, native signed uploads and MCP OAuth
retain separate authentication boundaries. Entry points delegate document work to
`document_api/handler.py`.

Document storage has two owners: `dynamodb_document_service.py` manages index,
task, project, device and deletion records; `s3_document_service.py` owns Markdown
objects, paths, reads, edits and storage dispatch. Validation and public response
projection remain separate from persistence. Do not reintroduce the superseded
standalone document Lambda/services tree.

`telemetry-ui/` is the Angular application. Browser verification uses local
synthetic authentication/API fixtures, not live identity or data resources.

## Infrastructure and validation

`aws-infrastructure/cdk/` synthesizes the one telemetry Lambda and edge/data/
identity examples from the top-level templates. Lambda reserved concurrency is 10.
The examples use fictitious account 123456789012 and example identities. The app
rejects real-account synthesis. No deploy, IAM apply, migration or retirement
entry point is retained.

The root Python environment validates the runtime and CDK. The UI has one root npm
workspace and lockfile. GitHub CI has runtime, collector and UI jobs only; no OIDC
or AWS mutation steps.

## Local strategic memory

The independent `strategic-memory-collector-agent/` package validates a completion
manifest, durably admits it to SQLite, and starts a detached serial LangGraph
worker. Qwen extraction is followed by deterministic evidence-reference checks
and candidate persistence. Human review remains necessary before trusted-memory
promotion. Its root CLI, services folder and dedicated tests are documented in its own
architecture/code map.

## State boundaries

The collector queue lives in ignored `.runtime/` state. Copied watcher owner, session,
receipt, deletion and device runtime files remain excluded from Git. None is
required for ordinary validation or automatically uploaded. Task records are local
and ignored. The Softrobots checkout retains deployment history; application
changes should not be copied into both repositories without explicit scope.

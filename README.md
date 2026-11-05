# Markdown file-sharing network across agents

Keep Markdown chats, learning notes and project documents in one shared place
for people and AI agents. Upload through MCP, the web UI or a configured local
watcher, then find and read documents in the Telemetry workspace.

## How it works

```text
 ┌───────────────────────────┐                 ┌───────────────────────────┐
 │          ChatGPT          │                 │           Claude          │
 │        Web / Mobile       ├────────┬────────┤        Web / Mobile       │
 └───────────────────────────┘        │        └───────────────────────────┘
                                      │
                                      │
                                      │
 ┌───────────────────────────┐        │        ┌───────────────────────────┐
 │           Codex           │        │        │        Claude Code        │
 │        Coding agent       ├────────┼────────┤        Coding agent       │
 └───────────────────────────┘        │        └───────────────────────────┘
                                      │
                                      │
                                      │
                                      ▼
                       ┌─────────────────────────────┐
                       │     MCP client requests     │
                       │      OAuth via Cognito      │
                       └─────────────────────────────┘
                                      │
                                      │
                                      ▼
                       ┌─────────────────────────────┐
                       │      Amazon API Gateway     │
                       │    Authenticated routing    │
                       └─────────────────────────────┘
                                      │
                                      │
                                      ▼
                       ┌─────────────────────────────┐
                       │          AWS Lambda         │
                       │       Flask + FastMCP       │
                       │ Validate and store documents│
                       └─────────────────────────────┘
                                      │
                                      │
               ┌──────────────────────┴──────────────────────┐
               │                                             │
               │                                             │
               ▼                                             ▼
 ┌───────────────────────────┐                 ┌───────────────────────────┐
 │         Amazon S3         │                 │      Amazon DynamoDB      │
 │      Markdown content     │                 │ Metadata / document index │
 └───────────────────────────┘                 └───────────────────────────┘


  Upload results return to the client along the request path.
```

Client boxes show intended integration points, subject to each client's MCP
support and configuration; they do not confirm tested web/mobile availability.
MCP clients authenticate through the Cognito-backed OAuth flow.

Other access paths use the same storage:

```text
┌───────────────────────────┐     ┌───────────────────────────┐
│ Local Markdown watcher    │     │ Telemetry web workspace   │
│ Changed .md files         │     │ Angular reader / editor   │
└─────────────┬─────────────┘     └─────────────┬─────────────┘
              │                               │
      JWT + device signature          CloudFront / browser session
              │                               │
              └───────────────┬───────────────┘
                              ▼
                 ┌─────────────────────────┐
                 │ API Gateway → Lambda    │
                 │ Shared S3 / DynamoDB    │
                 └─────────────────────────┘
```

CloudFront serves the UI assets and forwards browser API requests. Browser
sessions, MCP OAuth and signed watcher uploads retain separate authentication
boundaries. These diagrams describe the architecture, not a verified live deployment.

## Main features

- **Share Markdown:** upload from MCP clients or the web UI into shared storage.
- **Sync local documents:** a configured watcher detects changed `.md` files,
  skips unchanged content and preserves project-relative paths.
- **Read and organize:** browse categories, filter your documents and search the
  loaded list in **Workspace → Documents**. Load more pages when needed.
- **Edit safely:** eligible owned documents save with a version check; other
  edits save as a copy in your folder.
- **Prepare reusable lessons:** a separate local collector extracts learning
  candidates from completed task evidence for human review.

## Upload reliability

An upload is complete only when a matching `READY` receipt confirms the file ID
and content hash. Unknown write outcomes must be reconciled before retrying.
The watcher implementation and its private runtime state are excluded from this
public repository. Verify project and identity binding before starting a local
watcher.

## Optional local memory collector

The independent collector follows this flow:

```text
Completed task evidence
          |
     SQLite queue
          |
  LangGraph worker <--> Amazon Bedrock / Qwen
          |
  Evidence and content checks
          |
  Local review candidates
          |
  Human review before promotion
```

Only the extraction call uses Bedrock; the queue and candidates stay local.
Parent completion hooks are not installed. Candidates are not automatically
promoted to trusted memory or published to Telemetry Documents.

## Repository layout

| Path | Purpose |
| --- | --- |
| `telemetry-api/` | Shared Flask/FastMCP runtime and document APIs |
| `telemetry-ui/` | Angular document workspace |
| `aws-infrastructure/` | CDK and CloudFormation examples |
| `strategic-memory-collector-agent/` | Independent local lesson-extraction worker |

S3 stores document content; DynamoDB stores its index and related records.
The older standalone `aws-infrastructure/document-api/` implementation was removed;
application changes belong in the current shared runtime.

## Local validation


Use Python 3.13 and Node 22.13 or later:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
AWS_EC2_METADATA_DISABLED=true AWS_DEFAULT_REGION=ca-central-1 \
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
.venv/bin/python -m pytest -q
.venv/bin/ruff check telemetry-api aws-infrastructure/cdk tests
.venv/bin/cfn-lint aws-infrastructure/*.yaml
npm ci
npm run build
npm test
uv sync --project strategic-memory-collector-agent --locked
(cd strategic-memory-collector-agent && uv run --locked pytest -q && uv run --locked ruff check .)
```

Run collector lint from its folder with `uv run --locked ruff check .`.
The root npm workspace/lockfile and `.npmrc` provide reproducible UI installation.
The compact runtime suite and CDK suite run together by default; the collector
has its own package environment.

For synthetic browser checks, install Chromium with
`npx playwright install chromium`, then run
`WO_BROWSER_EVIDENCE=/tmp/markdown-browser npm run test:browser`.
On macOS the harness uses installed Chrome by default; override
`WO_BROWSER_EXECUTABLE` if needed. Fixtures do not prove private authentication.

## Deployment and local state

CI runs validation only. Infrastructure examples use fictitious account
`123456789012` and example identities; real-account synthesis is rejected.
Live deployment requires separate configuration and authorization.

Do not publish private watcher, device, session or delivery state. The collector's
local review candidates remain separate from shared document storage.

## Further reading

- [Architecture](ARCHITECTURE.md)
- [Code map](code-map.yaml)
- [Collector guide](strategic-memory-collector-agent/README.md)
- [Collector architecture](strategic-memory-collector-agent/ARCHITECTURE.md)
- [HTML overview](project-overview.html)
- [Provenance](PROVENANCE.md)

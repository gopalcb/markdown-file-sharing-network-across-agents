# Markdown file-sharing network across agents

This repository is the authoritative source for the current Work Organizer and
Markdown-sharing application. It replaces the older document-Lambda-only
extraction with the complete tested application boundary:

- `telemetry-ui/` — Angular workspace UI served through CloudFront and private S3.
- `telemetry-api/` — Flask, document upload, and MCP runtime deployed as
  `telemetry-lambda`.
- `aws-infrastructure/` — local AWS CDK application plus the retained
  CloudFormation definitions for identity, data, and edge resources.
- `.codex/skills/telemetry-document-delivery/` — agent guidance for MCP uploads,
  native watcher authentication, receipt validation, and uncertain writes.
- `tests/` — focused API, storage, MCP, Lambda-adapter, and browser journey tests.

The live portal is <https://d2f8oftnpxmjan.cloudfront.net/>. The stable MCP
resource is <https://d3cjbof2ij7jy9.cloudfront.net/mcp/>.

## Local development

```bash
npm ci
npm start
```

Run the compact verification suite from the repository root:

```bash
npm test
npm run build
npm run check:format:web
python -m pip install -r requirements-dev.txt
python -m pytest tests aws-infrastructure/cdk/tests -q
python scripts/format_python.py --check
node tests/workspace-browser.mjs
```

The browser harness uses deterministic fixtures and does not prove live Cognito
or AWS persistence. See the [architecture](ARCHITECTURE.md) for the application
and authentication boundaries.

## Repository ownership

Future Markdown-sharing, MCP, watcher-contract, document API, and associated
Telemetry UI changes belong here. The prior Softrobots source is retained only as
historical/deployment context and must not be updated in parallel unless an
explicit synchronization task says otherwise.

## Deployment boundary

This public repository has validation-only GitHub Actions. It contains current
infrastructure source for review and offline synthesis, but it does not assume an
AWS role or deploy. Enabling release automation requires a separate reviewed change
with repository-specific trust, permissions, environment ownership, and live
acceptance criteria. Local AWS credentials are not a deployment substitute.

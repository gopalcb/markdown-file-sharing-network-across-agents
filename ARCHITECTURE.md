# Work Organizer architecture

## Production request path

```text
Browser
  -> CloudFront E224O80UCWC7IJ
       -> private versioned S3 bucket (Angular and auth-config.json)
       -> HTTP API 9t4clm1ev8
            -> telemetry-lambda (Flask + Lambda Web Adapter)
                 -> browser sessions and Cognito verification
                 -> analytics/projects/tasks
                 -> document_api shared storage implementation

MCP client / ChatGPT
  -> CloudFront E3BS4AEVAMTQSF (stable MCP resource hostname)
       -> same HTTP API
            -> telemetry-lambda (composite ASGI runtime)
                 -> FastMCP stateless JSON transport
                 -> Cognito OAuth adapter and encrypted retained state
                 -> document_api shared storage implementation

Local Codex watcher
  -> CloudFront /browser-documents
       -> HTTP API JWT authorizer
            -> telemetry-lambda
                 -> registered-device proof
                 -> document_api shared storage implementation
```

There is one Work Organizer and Markdown MCP application Lambda. The private UI bucket and API
are owned by stack `softrobots-telemetry`. Stack `softrobots-portal-edge` retains
the canonical distribution and is updated in place. The old
`softrobots-document-api` stack is reduced to retained S3/DynamoDB data after
the replacement passes acceptance.

The MCP distribution remains a separate stable public resource because its URL
is the OAuth audience configured in clients. Stack `markdown-mcp-service-edge`
is adopted in place and changes only its origin from ECS Express to the shared
telemetry HTTP API. The standalone MCP ECS service remains the rollback origin
until deployed private OAuth and storage acceptance passes.

## Authentication boundaries

- Browser login uses the existing family pool, portal client and Cognito hosted
  login. Flask exchanges the authorization code, creates an opaque session and
  enforces group, Origin and CSRF checks.
- `/documents` accepts the existing family-pool native upload client.
- `/browser-documents` accepts the existing username-pool upload client.
- API Gateway validates issuer, audience and scope. The application rechecks
  trusted authorizer claims and validates the exact body/path device signature.
- Browser Cognito `sub` ownership and native username ownership remain distinct.
- MCP uses the existing username-pool client, `markdown-api/write` scope, exact
  owner subject, dynamic client registration, PKCE, encrypted DynamoDB OAuth
  state and persistent Secrets Manager signing material. MCP bearer validation
  remains inside the application rather than an API Gateway JWT authorizer.

Cognito stacks and retained identity/session tables are existing stateful
resources. CDK references their exact identifiers; it does not recreate users,
pools or clients during an application deployment.

## Storage

- Angular assets: private, encrypted, versioned S3 bucket owned by
  `softrobots-telemetry`; only the canonical CloudFront distribution may read.
- Current Markdown: `agent-work-history--249766001220-ca-central-1` under
  `agent-work/raw-markdown/<owner>/<relative-path>`.
- Legacy Markdown remains readable from the previous document bucket.
- DynamoDB stores document identity/status, projects, tasks, sessions, devices
  and analytics metadata.

Native `project-path-v1` uploads keep a stable UUID5 file/task identity for a
source path. Storage is acknowledged only after S3 and a consistent DynamoDB
record are both present. Unknown outcomes are inspected, never blindly retried.

## Validation and deployment boundary

`.github/workflows/validate.yml` builds and tests the current application, validates
templates and policy source, synthesizes every topology, builds the Lambda image,
and runs the browser journey. It has `contents: read`, no OIDC permission, and no
deployment job.

`aws-infrastructure/cdk/deploy.py` retains the reviewed source deployment phases for
architecture continuity, but this repository does not invoke them automatically:

- `prepare`: build Angular, synth/diff/deploy `softrobots-telemetry`, publish and
  read back every asset, and check direct Lambda/API health.
- `reconcile`: rebuild a prepare receipt from completed live resources without
  deploying.
- `cutover`: require the exact prepared source digest, update CloudFront in
  place, and invalidate entry/config routes.
- `mcp-prepare`: deploy the combined image and check discovery plus anonymous
  denial directly through the HTTP API, without moving the MCP edge.
- `mcp-reconcile`: bind those MCP checks to a telemetry image already deployed by
  `prepare`, avoiding a duplicate image deployment in the full-release workflow.
- `mcp-cutover`: require that exact prepare digest, update the retained MCP
  distribution in place, then recheck discovery and anonymous denial.
- retirement is performed only after private Cognito and exact upload read-back
  acceptance.

Any future release automation requires a separate reviewed repository-specific
trust and permission change. The deployment command verifies the fixed account and
region. CDK uses a container image,
512 MiB memory, 28-second timeout and reserved concurrency 10. Static asset
publication avoids helper Lambdas.

## Source responsibilities

- `telemetry-ui/src/app/`: routed Angular components, services, guards and
  interfaces.
- `telemetry-ui/src/theme/`: the application-owned design tokens and primitives.
- `telemetry-api/app.py`: fail-closed Flask application factory.
- `telemetry-api/server.py`: composite ASGI dispatcher for Flask and MCP.
- `telemetry-api/mcp_api/app.py`, `auth_service.py`, `oauth_service.py`,
  `storage_service.py`, and `validation_service.py`: FastMCP protocol, bounded
  ingress, Cognito OAuth with its DynamoDB state, shared storage, and contracts.
- `telemetry-api/routing.py`: public health, session-protected API and
  native upload adapters.
- `telemetry-api/services/authentication_service.py`: portal JWT verification,
  signing-key cache, browser sessions, CSRF, and fixed device bindings. One
  `BrowserSessionService` instance carries the table, Cognito client, verified
  configuration, and signing keys through a request.
- `telemetry-api/services/analytics_service.py`: portfolio cursor/delete
  validation, bounded event projection, public-identity resolution, and atomic
  deletion. Raw session and DynamoDB keys never cross the API boundary.
- `telemetry-api/services/native_upload_service.py`: verified API Gateway upload
  reconstruction and byte-preserving document dispatch.
- `telemetry-api/document_api/dynamodb_document_service.py`: document metadata,
  tasks, projects, device approval, deletion observations, and conditional index
  updates.
- `telemetry-api/document_api/s3_document_service.py`: Markdown paths, uploads,
  reads, edits, folders, and storage-operation dispatch. `DocumentStorage`
  carries the table, S3 client, and current/legacy buckets shared by those
  operations.
- `telemetry-api/document_api/validation.py`, `portal_validation.py`, and
  `portal_contract.py`: native/storage validation, trusted portal/project
  validation, and public response projection kept separate from persistence.
- `telemetry-api/document_api/handler.py`: the thin Lambda-compatible coordinator
  shared by browser, native, and MCP adapters.
- `aws-infrastructure/cdk/telemetry_stack.py`: Lambda, HTTP API, IAM, logs and UI
  storage.
- `aws-infrastructure/cdk/edge_stack.py` and `portal-edge.yaml`: adoption and
  in-place update of the canonical distribution.
- `aws-infrastructure/cdk/mcp_edge_stack.py` and `mcp-edge.yaml`: in-place MCP
  edge origin migration while retaining its public resource URL.
- `aws-infrastructure/cdk/data_stack.py` and `document-data.yaml`:
  retained document data ownership and old-compute retirement.
- `aws-infrastructure/cdk/configure_ci_policy.py`: perform one-time direct-IAM
  mutation denial and then verify the read-only policy used by main-only OIDC.
- `auth.yaml` and `username-auth.yaml`: existing Cognito/stateful stack sources;
  they are not application deployment entry points.

## Retirement boundary

Shared/default networking and AWS service-linked roles remain outside this
architecture. The legacy serverless MCP stack also remains outside this cleanup;
its cloud resources must not be deleted until live private OAuth, protocol,
upload, exact storage read-back, refresh and revocation checks pass against
`telemetry-lambda` and its exact ownership is reconciled.

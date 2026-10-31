# Architecture

The document Lambda owns the authenticated Markdown boundary and the S3/DynamoDB publication protocol. API Gateway validates JWT signatures, issuer, audience, and scope. `app.lambda_handler` validates identity and routes public ingestion versus trusted IAM direct invocations. Public HTTP events cannot select trusted operations.

## Structure and execution

`aws-infrastructure/document-api/src/app.py` configures bounded AWS clients, dispatches requests, and sanitizes outcomes. `src/session_device_service.py` binds verified Cognito refresh families to reported devices and enforces a fixed original-session deadline. It is bundled locally rather than imported from a portal backend.

`src/services/validation_service.py`, `portal_validation_service.py`, and `project_validation_service.py` own request validation. `native_device_service.py` checks administrator-approved public keys and consumes one-use nonces. Enrollment does not approve a key.

`document_service.py` routes batches. `path_document_service.py` uses conditional S3 writes, stable path identity, and explicit incomplete outcomes. `document_storage_service.py` publishes compact owner-index rows. `document_read_service.py` provides bounded latest-byte reads. `portal_document_service.py` supplies trusted adapter operations and conditional ETag edits; its name is historical, and no portal web backend is packaged. `task_service.py` links plans to independently editable tasks, while `project_service.py` owns a shared catalog. `deletion_service.py` records immutable local deletion observations without deleting storage. `logging_service.py` records identifiers without document bodies or credentials.

## Data and configuration

The API template supplies `DOCUMENTS_BUCKET`, `LEGACY_DOCUMENTS_BUCKET`, `USER_FILES_TABLE`, `PROJECTS_TABLE`, and `DELETIONS_TABLE`. Native registry/session table names are source-compatible constants; templates and example policies must agree. S3 keys bind owner, classification, and canonical relative path. Source-binding metadata prevents unrelated source aliases from taking over an occupied path. Stable UUID5 IDs preserve linked tasks across explicit updates.

S3 acknowledgement precedes DynamoDB READY publication. The services are not transactional: report incomplete or uncertain writes without blind retries. Rows retain path/status/identity/task metadata, not immutable S3 version pins. Reads return latest UTF-8 bytes with an ETag. Successful acknowledgement does not promise those bytes remain current after a competing writer.

## Local verification and extension

`scripts/local_smoke.py` invokes the real handler under Moto. It uses the trusted invocation boundary so it can verify storage without forging a production JWT or bypassing native approval. Tests additionally exercise public ingestion/authentication with controlled fixtures. No live AWS or real private login is used.

`aws-infrastructure/document-api/scripts/client/` is a minimal optional macOS native login/signing client, including its Swift Keychain helper. Deployment/reference helpers remain separate from runtime and are not included in CI execution. CI has no AWS credential or deployment step. To add an agent interface, implement a separately authenticated adapter with narrowly scoped invoke permission, or use the independently maintained companion MCP service according to its own contract.

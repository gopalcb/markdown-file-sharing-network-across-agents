# Markdown file-sharing network across agents

The canonical repository is gopalcb/markdown-file-sharing-network-across-agents.
Preserve concurrent work and inspect Git status before edits. Do not repoint this
checkout or use another project's repository, accounts, or live resource IDs.

Read README.md, ARCHITECTURE.md, code-map.yaml, and affected component sources.
Use python-development for Python work. Keep modules readable and update their
architecture/code map when changing responsibilities.

## Validation

CI is validation-only. No deployment, live AWS calls, Bedrock inference, automatic
publication, push, or merge unless explicitly requested. Local AWS tests use fake
credentials and AWS_EC2_METADATA_DISABLED=true. Run the compact runtime/CDK suite,
collector suite, and Angular checks through the commands in README.md. The older
standalone document-api extraction must not be restored beside telemetry-api.

The CDK application synthesizes fictitious resources only. Do not restore removed
production cutover, IAM mutation, or retirement workflows without a new reviewed
deployment design and explicit authorization.

## Local state and records

Never stage watcher/delivery owner, session, state, lock, deletion, receipt, or
log files from .codex/tools or .codex/document-delivery. Preserve them locally.
Do not start copied watcher.py until a human verifies its project and identity
binding. No upload or watcher startup is required for ordinary task records.

Keep concise plans and summaries in ignored .task-records/ using descriptive dated
names ending in __PLAN.md or __SUMMARY.md. Implementation does not authorize
publication. Avoid generic plan.md/summary.md and never store secrets in records.

# Source provenance

This project began at 17c8aea as a standalone extraction of the older document
Lambda. The subsequent import copied the consolidated Softrobots telemetry API,
UI and CDK source. Keeping both trees locally reintroduced obsolete document
services and tests, even though the replacement intent of PR#1 was to remove them.

The maintained source is now the compact telemetry tree, following the two-service
document organization present in Softrobots release PR#66 and the replacement
layout reviewed in this repository's PR #1. The legacy extraction, its IAM helpers,
many unit-test files and local-smoke wrapper have been removed from this tree.
Local recoverable copies were retained outside the repository during cleanup.

The collector was moved from the LangGraph harness project and remains an
independent flat Python package. Production account/identity defaults in the
added CDK examples were replaced with fictitious values. Source deployment,
IAM mutation, cutover and retirement commands are not provided here.

Source history remains available in Git. Current-source cleanup does not erase
history or certify live authentication, storage persistence, or cloud resources.
No deployment or external data mutation was performed as part of this cleanup.

# Current source refresh

Status: Complete locally; publication pending review.

The current application allowlist has replaced the older extraction on an isolated
branch. Documentation, provenance, project instructions, package identity, code map,
and GitHub Actions now describe the standalone repository and its validation-only
boundary. No deployment, authentication, upload, or cloud mutation has run.

The latest local UI work was also carried forward: Documents loaded counts,
per-column tracker pagination/loading, compact analytics search and selection,
receipt-checked analytics deletion, and the shared Telemetry logo. The deletion API
accepts only public event identities, resolves private keys internally, and uses a
table-scoped transactional write permission.

## Verification

- Angular production build passed.
- Angular unit tests passed: 5 tests.
- Web and Python formatting checks passed.
- Python/CDK tests passed: 16 passed, 1 skipped.
- CloudFormation lint and bounded-policy source validation passed.
- All four CDK topologies synthesized locally with `python3`; the local shell lacks
  the `python` alias used by GitHub setup-python.
- The Lambda image built and its real adapter test passed: 1 passed.
- Fixture browser journeys passed at 1440x900 and 390x844, including Documents
  counts and analytics select/confirm/delete/empty-state behavior.
- Secret/private-key filename and content-pattern checks found no matching artifacts.
- Source comparison found only expected ignored build/cache output and the deliberate
  validation-workflow assertion change.

## Limits

No live Cognito authentication, MCP upload, storage read-back, AWS mutation, or
deployment was run. `npm ci` reported 16 moderate dependency audit findings. The
shared system Python environment also reported conflicts with an unrelated
`openai-agents` installation; repository checks still passed with the pinned app
dependencies.

Remaining: inspect the staged public diff, publish the PR, and obtain independent
review.

The premium UI static auditor was also run in strict mode. Its whole-project scan
reported 73 findings: mostly actionless-button false positives because it does not
recognize Angular `(click)` bindings, plus inherited native date/select ownership
and form-style findings outside this port. Targeted anti-pattern searches over the
changed UI returned no native-dialog, clickable-nonsemantic, unsafe-innerHTML,
IME-unsafe Enter, or clickable-table-header matches.

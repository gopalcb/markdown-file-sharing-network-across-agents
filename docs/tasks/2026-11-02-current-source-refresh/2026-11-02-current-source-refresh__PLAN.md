# Refresh the standalone repository from current source

## Summary

Replace the obsolete document-Lambda-only extraction with the current consolidated
Telemetry application and make this repository authoritative for future
Markdown-sharing work.

## Scope

- Import the current tracked UI, API, MCP, document storage, infrastructure,
  scripts, manifests, and tests from Softrobots commit `8c11a24`.
- Include the focused document-delivery skill from reviewed commit `6198eb2`.
- Preserve the standalone license and public, validation-only delivery boundary.
- Exclude source deployment workflows, historical operational docs, generated
  interactions, local watcher runtime, credentials, build output, and Git history.

## Acceptance criteria

- The imported allowlist matches its source bytes except for documented standalone
  naming, ownership, provenance, and CI changes.
- CI validates the complete application without OIDC or an AWS deployment job.
- Repository guidance identifies this project as the future source of truth.
- Focused build, test, format, template, synthesis, image, and browser checks pass,
  or exact unrun/environmental limitations are reported.

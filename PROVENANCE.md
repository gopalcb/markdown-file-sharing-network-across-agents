# Source and extraction

Extracted from [gopalcb/gb-systems-and-agents](https://github.com/gopalcb/gb-systems-and-agents), source commit `ef9daa434b9a046e3e752bbfd0b719d46e729f3a`.

The allowlist contains the `work-organizer-portal/aws-infrastructure/document-api/` runtime, tests, scripts and scoped IAM examples; focused Cognito upload/session resources from `auth.yaml` and `username-auth.yaml`; the single required `session_device_service.py` module from the source backend; and four optional native client dependency files (`cognito_auth.py`, `native_session.py`, `device_signing.py`, `mac_device_key.swift`) from the Work Organizer skill. Those copied modules contain implementation only; no saved identity, key, token, device registry, or Keychain export is included.

The new root README, architecture, code map, license, validation workflow, requirements manifest, and offline smoke script are standalone publication material. Original deployment workflows and IAM policies embedded in the source auth stacks were excluded. Public template issuer/client constraints are now deployment parameters; the hosted-login domain and SES settings are explicit parameters. CloudFront outputs and production upload defaults were removed. Original account/pool/client examples were replaced with fictitious examples. Existing `softrobots-*` resource names and source API/service names remain for compatibility.

Runtime tests were retained. Assertions tied to source GitHub deployment ordering and portal-edge configuration were adapted to verify standalone validation-only CI and the absence of CloudFront resources. Source workflow assertions for project-table bootstrapping were replaced with local table/template checks; runtime table-retention, encryption, approval policy, replay, upload, read, and client boundary tests remain. This repository makes no claims about the source deployment state.

Excluded: portal frontends, portal backend application, design themes, ECS/CloudFront UI infrastructure, MCP server implementation, `.codex`, source AGENTS/workflow records, task and interaction logs, watcher implementation/runtime state, historical documents/data, environment files, credentials, build outputs, and all source Git history.

Copyright 2026 Gopal C Bala. Published under MIT.

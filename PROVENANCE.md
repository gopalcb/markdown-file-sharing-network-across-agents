# Source and refresh

The original standalone extraction came from
[gopalcb/gb-systems-and-agents](https://github.com/gopalcb/gb-systems-and-agents)
commit `ef9daa434b9a046e3e752bbfd0b719d46e729f3a`.

On 2026-11-02 it was replaced with the current consolidated application source
from Softrobots commit `8c11a24ccd4bc3767389896b49ff583d4b18b101`.
The allowlist contains the Angular application, Flask/FastMCP/Lambda runtime,
document storage implementation, CDK and retained CloudFormation source, formatting
scripts, dependency manifests, system/CDK/browser tests, architecture/code map, and
the focused Telemetry document-delivery skill from reviewed commit `6198eb2`.

The source deployment workflow was deliberately excluded. This repository keeps a
validation-only workflow with no OIDC permission and no AWS deployment job. The
restored Softrobots historical `docs/`, generated `doc/` interactions, local watcher
runtime, native sessions, Keychain material, environment files, credentials, build
outputs, deployment receipts, and source Git history were not copied.

The existing infrastructure names and deployment-specific references remain in
source because they are part of the current tested topology. Their presence is not
evidence of a deployment from this repository, and the validation workflow cannot
mutate AWS.

Copyright 2026 Gopal C Bala. Published under MIT.

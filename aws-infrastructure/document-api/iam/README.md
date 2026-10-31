# Example IAM boundaries

These JSON documents use the fictitious AWS account `123456789012` and example region `ca-central-1`. They are reviewable policy examples, not an installed configuration or evidence of live IAM permissions. Replace account, region, role names and resource scopes deliberately before separately authorized use.

`runtime-trust.json` and `runtime-policy.json` separate Lambda trust from scoped storage, index, logging, session, enrollment, approval-read and nonce operations. Runtime object deletion is denied. Runtime can read approval decisions but cannot grant approval.

`cloudformation-trust.json` and `cloudformation-policy.json` describe a separate provisioner. `deployer-policy.json` describes a delivery principal that can pass the provisioner and access the intended artifact prefix. `device-approval-policy.json` is for an explicit administrator; do not attach it to the document runtime.

The source-compatible names must match templates and service constants. The API stack expects the runtime role to exist. No IAM bootstrap or AWS deployment runs in this repository's CI. The Python configuration and approval scripts are tested reference helpers with example target guards; review and adapt them before real use. Policy source assertions are not IAM simulation or deployed permission evidence.

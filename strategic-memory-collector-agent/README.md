# Strategic memory collector agent

A separate, local LangGraph workflow that turns completed and verified engineering
evidence into review-only strategic-memory candidates. Queue admission is fast and
idempotent; model work runs in a detached one-shot process.

The collector never edits repository source, native Codex memory, `AGENTS.md`,
skills, or prompts. It never promotes a candidate automatically.

## Model

The live adapter uses Amazon Bedrock Runtime Converse structured output with Qwen3
32B. Defaults:

```text
STRATEGIC_MEMORY_BEDROCK_MODEL_ID=qwen.qwen3-32b-v1:0
STRATEGIC_MEMORY_AWS_REGION=us-east-1
```

Qwen3 32B is not available in `ca-central-1`. Override the region only with one
listed as supported by the AWS model card. Credentials come from the standard AWS
provider chain and require only the Bedrock inference permission needed for the
configured model. Never store credentials in manifests or this directory.

## Install and test

```sh
uv sync --project strategic-memory-collector-agent
uv run --project strategic-memory-collector-agent pytest -q
uv run --project strategic-memory-collector-agent ruff check .
```

Tests use an injected fake model and make no AWS calls.

## Connect the completion workflow

After the parent workflow finishes its required verification and writes its final
summary, create the completion manifest shown below and invoke `enqueue`. Keep
this call after the successful completion boundary; a Codex turn ending by itself
does not establish task completion. The package is intentionally independent of
the harness directories being reorganized in parallel. Adding this command to
the parent workflow is the integration point; installing the package alone does
not automatically intercept existing runs.

The parent should record the returned job ID and report collection as queued.
Use `status` to inspect later progress. Restart interrupted work with `drain`;
retry provider failures explicitly with `retry JOB_ID` after fixing the cause.

## Enqueue completed work

Create a JSON manifest containing only bounded, non-sensitive evidence:

```json
{
  "project_id": "deep-agent-harness",
  "task_id": "durable-queue",
  "status": "complete",
  "source_revision": "abcdef1234567",
  "completed_at": "2026-11-02T15:00:00Z",
  "summary": "Added transactional queue admission and verified interruption recovery.",
  "changed_paths": ["strategic-memory-collector-agent/services/storage_service.py"],
  "checks": [{"name": "pytest", "passed": true, "source": "local:test"}]
}
```

Then enqueue it:

```sh
uv run --project strategic-memory-collector-agent \
  strategic-memory-collector-agent enqueue --manifest /absolute/path/completion.json
```

The command commits the job to `.runtime/strategic-memory-collector-agent/`, starts
a detached drainer, and returns. Admission does not mean collection or promotion
succeeded.

## Operate and review

```sh
uv run --project strategic-memory-collector-agent strategic-memory-collector-agent status
uv run --project strategic-memory-collector-agent strategic-memory-collector-agent drain
uv run --project strategic-memory-collector-agent strategic-memory-collector-agent retry JOB_ID
uv run --project strategic-memory-collector-agent \
  strategic-memory-collector-agent candidates --project deep-agent-harness
```

`candidates` is an explicit review view. A separate human-authorized workflow must
decide whether a candidate belongs in any authoritative memory or instruction file.

AWS references:

- <https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-qwen-qwen3-32b.html>
- <https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html>

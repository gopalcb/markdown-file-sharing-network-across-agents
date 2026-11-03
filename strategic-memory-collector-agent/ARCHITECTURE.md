# Architecture

## Purpose

Collect durable learning after completed engineering work without delaying the
foreground workflow or allowing a model to rewrite trusted instructions.

## Execution flow

```text
completion manifest
  -> enqueue CLI
  -> SQLite pending job
  -> detached single-writer drainer
  -> LangGraph: extract -> gate -> persist
  -> review-only candidate records and job receipt
```

`enqueue` validates all evidence before committing it. The detached drainer claims
one job using a lease and invokes Qwen through Bedrock Runtime. Bedrock constrains
the response with JSON Schema. Pydantic validates it again, and deterministic gates
require exact passed-evidence references and reject sensitive, transient, foreign,
or duplicate output. Candidate and successful job writes share one transaction.

## Module responsibilities

- `services/contracts_service.py`: manifest, evidence, candidate, sensitivity, and schema contracts.
- `services/model_service.py`: one Bedrock Qwen Converse structured-output invocation.
- `services/storage_service.py`: SQLite queue, leases, retries, receipts, and candidate records.
- `services/workflow_service.py`: the three-node LangGraph and deterministic candidate gate.
- `cli.py`: bounded file input, detached process lifecycle, and operational commands.

## Configuration

Runtime data defaults to the repository's ignored
`.runtime/strategic-memory-collector-agent/`. Public model configuration is read
from `STRATEGIC_MEMORY_BEDROCK_MODEL_ID` and `STRATEGIC_MEMORY_AWS_REGION`.
Credentials stay in the standard AWS provider chain.

## Safety and recovery

The agent has no shell, repository-write, Git, deployment, or memory-promotion
tool. It receives only the admitted manifest. SQLite WAL plus a single drainer file
lock provides local concurrency control. Expired running leases can be reclaimed;
failed jobs require an explicit bounded retry. Logs contain job IDs and exception
types, never evidence or provider response bodies.

This is local durability, not a continuously available service. OS shutdown may
stop the detached process, but the admitted job remains recoverable with `drain`.

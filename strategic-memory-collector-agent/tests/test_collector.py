"""Verify contracts, queue transactions, graph behavior, and the Bedrock request.

Temporary databases exercise real SQLite/LangGraph code. FakeModel replaces model
reasoning; FakeBedrockClient captures the request shape without proving live AWS
model access or quality of generated strategic-memory candidates.
"""

import json
import threading
from datetime import UTC, datetime

import pytest

from cli import build_parser, drain_jobs, load_manifest
from services.contracts_service import CandidateBatch, CompletionManifest, MemoryCandidate
from services.model_service import BedrockQwenModel
from services.storage_service import CollectorStore


def manifest_payload(**updates) -> dict:
    """Build one verified completion fixture with caller-selected overrides."""
    payload = {
        "project_id": "harness",
        "task_id": "task-42",
        "status": "complete",
        "source_revision": "abcdef1234567",
        "completed_at": datetime.now(UTC).isoformat(),
        "summary": "Implemented durable queue admission and verified recovery after interruption.",
        "changed_paths": ["strategic-memory-collector-agent/services/storage_service.py"],
        "checks": [{"name": "pytest", "passed": True, "source": "local:test"}],
    }
    payload.update(updates)
    return payload


class FakeModel:
    """Return deterministic candidates without external credentials or network calls."""

    identity = "fake:qwen"

    def __init__(self, candidates=None, error=None):
        """Configure one batch or one provider-style failure."""
        self.candidates = candidates or []
        self.error = error

    def extract(self, manifest: CompletionManifest) -> CandidateBatch:
        """Return the configured strict batch after observing a validated manifest."""
        if self.error:
            raise self.error
        return CandidateBatch(candidates=self.candidates)


class FakeBedrockClient:
    """Capture the Converse request and return schema-shaped Qwen output."""

    def __init__(self):
        """Start without a captured request."""
        self.request = None

    def converse(self, **request) -> dict:
        """Retain public request fields and return one valid candidate batch."""
        self.request = request
        output = CandidateBatch(candidates=[candidate()]).model_dump_json()
        return {"output": {"message": {"content": [{"text": output}]}}}


def candidate(
    content="Use transactional queue admission for idempotent background work.", **updates
):
    """Build one evidence-bound candidate fixture."""
    payload = {
        "category": "failure-shield",
        "content": content,
        "scope": "local background workers",
        "evidence_sources": ["check:pytest"],
        "confidence": "high",
        "revalidate_after": "when queue storage changes",
    }
    payload.update(updates)
    return MemoryCandidate.model_validate(payload)


def test_manifest_rejects_unverified_sensitive_and_unsafe_inputs():
    """Reject failed evidence, secrets, private identities, and path traversal."""
    sensitive_summary = "Completed work with token=" + ("a" * 16) + " in captured output."
    for update in [
        {"checks": [{"name": "pytest", "passed": False, "source": "local"}]},
        {"summary": sensitive_summary},
        {"summary": "Completed verification for private.person@company.invalid successfully."},
        {"changed_paths": ["../outside.txt"]},
        {"status": "complete", "completed_at": "2026-11-02T10:00:00"},
    ]:
        with pytest.raises(ValueError):
            CompletionManifest.model_validate(manifest_payload(**update))


def test_load_manifest_rejects_symlink_and_oversized_file(tmp_path):
    """Bound file ingestion before JSON parsing or model dispatch."""
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest_payload()))
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(ValueError):
        load_manifest(link)
    source.write_bytes(b"x" * 256_001)
    with pytest.raises(ValueError):
        load_manifest(source)


def test_cli_accepts_runtime_directory_before_subcommand(tmp_path):
    """Keep detached drainer arguments compatible with the top-level parser."""
    arguments = build_parser().parse_args(["--data-dir", str(tmp_path), "drain"])
    assert arguments.command == "drain"
    assert arguments.data_dir == str(tmp_path)


def test_bedrock_qwen_uses_converse_structured_output():
    """Bind live extraction to the documented Qwen model and JSON Schema contract."""
    client = FakeBedrockClient()
    model = BedrockQwenModel(client=client)
    result = model.extract(CompletionManifest.model_validate(manifest_payload()))
    assert result.candidates[0].category == "failure-shield"
    assert client.request["modelId"] == "qwen.qwen3-32b-v1:0"
    text_format = client.request["outputConfig"]["textFormat"]
    assert text_format["type"] == "json_schema"
    schema = json.loads(text_format["structure"]["jsonSchema"]["schema"])
    assert schema["additionalProperties"] is False


def test_concurrent_identical_enqueue_creates_one_job(tmp_path):
    """Make repeated completion signals converge on one durable job identity."""
    store = CollectorStore(tmp_path)
    manifest = CompletionManifest.model_validate(manifest_payload())
    results = []

    def enqueue() -> None:
        """Record one concurrent admission result."""
        results.append(store.enqueue(manifest))

    threads = [threading.Thread(target=enqueue) for _ in range(4)]
    # Share one validated manifest so identity stays identical across contenders.
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({result[0]["job_id"] for result in results}) == 1
    assert sum(created for _, created in results) == 1
    assert store.status()["jobs"] == {"pending": 1}


def test_drainer_persists_review_only_candidates_and_deduplicates(tmp_path):
    """Persist accepted candidates once without marking them trusted or promoted."""
    store = CollectorStore(tmp_path)
    store.enqueue(CompletionManifest.model_validate(manifest_payload()))
    duplicate = candidate()
    # Repeat the same model output to exercise the graph's batch deduplication.
    assert drain_jobs(store, FakeModel([duplicate, duplicate])) == 1
    assert store.status() == {"jobs": {"succeeded": 1}, "candidate_count": 1}
    records = store.list_candidates("harness")
    assert records[0]["status"] == "candidate"
    assert "reviewed_by" not in records[0]


def test_candidate_gate_rejects_foreign_evidence_and_transient_content(tmp_path):
    """Drop model output that lacks exact evidence or describes temporary state."""
    with pytest.raises(ValueError):
        candidate("This pending task should become durable memory after it finishes.")
    store = CollectorStore(tmp_path)
    store.enqueue(CompletionManifest.model_validate(manifest_payload()))
    candidates = [candidate(evidence_sources=["check:not-run"])]
    assert drain_jobs(store, FakeModel(candidates)) == 1
    assert store.status()["candidate_count"] == 0


def test_model_failure_records_safe_retryable_state_without_candidates(tmp_path):
    """Keep provider failures observable without partial candidate writes."""
    store = CollectorStore(tmp_path)
    job, _ = store.enqueue(CompletionManifest.model_validate(manifest_payload()))
    assert drain_jobs(store, FakeModel(error=RuntimeError("provider body must not persist"))) == 0
    assert store.status() == {"jobs": {"failed": 1}, "candidate_count": 0}
    retry = store.retry(job["job_id"])
    assert retry["status"] == "pending"
    assert retry["error_type"] is None


def test_expired_running_lease_is_recovered_without_duplicate_candidate(tmp_path):
    """Reclaim interrupted work and preserve candidate idempotency."""
    store = CollectorStore(tmp_path)
    store.enqueue(CompletionManifest.model_validate(manifest_payload()))
    claimed = store.claim_next(lease_seconds=-1)
    # An already-expired lease simulates a worker that stopped before completion.
    assert claimed["status"] == "running"
    assert drain_jobs(store, FakeModel([candidate()])) == 1
    assert store.status()["candidate_count"] == 1
    assert drain_jobs(store, FakeModel([candidate()])) == 0
    assert store.status()["candidate_count"] == 1

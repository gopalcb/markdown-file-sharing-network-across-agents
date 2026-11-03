"""Connect extraction, deterministic checks, and candidate storage with LangGraph.

Graph checkpoints retain intermediate run state. The separate CollectorStore is
the authority for queue status and candidate receipts. Neither store promotes a
candidate into trusted memory or edits repository instructions.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from services.contracts_service import CandidateBatch, CompletionManifest, MemoryCandidate
from services.model_service import CandidateModel
from services.storage_service import CollectorStore


class CollectorState(TypedDict, total=False):
    """Carry one admitted job through extraction, policy gates, and persistence."""

    job_id: str
    manifest: dict
    extracted: list[dict]
    accepted: list[dict]
    receipt: dict


class StrategicMemoryWorkflow:
    """Coordinate one model call and deterministic candidate review boundaries."""

    def __init__(self, store: CollectorStore, model: CandidateModel, checkpoint_path: str | Path):
        """Compile the graph with local persistent checkpoints."""
        self.store = store
        self.model = model
        self.connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
        graph = StateGraph(CollectorState)
        # Each named node returns only the state fields it changes. Keeping the
        # three stages separate makes extracted and accepted output inspectable.
        graph.add_node("extract", self.extract_candidates)
        graph.add_node("gate", self.gate_candidates)
        graph.add_node("persist", self.persist_candidates)
        graph.add_edge(START, "extract")
        graph.add_edge("extract", "gate")
        graph.add_edge("gate", "persist")
        graph.add_edge("persist", END)
        self.graph = graph.compile(checkpointer=SqliteSaver(self.connection))

    def extract_candidates(self, state: CollectorState) -> dict:
        """Invoke the configured model once with the admitted completion manifest."""
        manifest = CompletionManifest.model_validate(state["manifest"])
        batch = self.model.extract(manifest)
        return {"extracted": [candidate.model_dump(mode="json") for candidate in batch.candidates]}

    def gate_candidates(self, state: CollectorState) -> dict:
        """Keep only valid candidates citing exact passed evidence references."""
        manifest = CompletionManifest.model_validate(state["manifest"])
        allowed = manifest.allowed_evidence_sources()
        # Reference membership prevents invented check/path names. It does not
        # prove a claim logically follows from the source; human review still does.
        accepted = []
        seen = set()
        for payload in state.get("extracted", []):
            try:
                candidate = MemoryCandidate.model_validate(payload)
            except ValueError:
                # An injected adapter might bypass batch validation. Reject that
                # individual malformed candidate while checking the others.
                continue
            if not set(candidate.evidence_sources) <= allowed:
                continue
            digest = candidate.content.casefold().strip()
            # This is exact normalized text deduplication within the batch, not
            # semantic similarity. Storage also deduplicates across project jobs.
            if digest in seen:
                continue
            seen.add(digest)
            accepted.append(candidate.model_dump(mode="json"))
        return {"accepted": accepted}

    def persist_candidates(self, state: CollectorState) -> dict:
        """Store review-only candidates and a truthful completion receipt atomically."""
        manifest = CompletionManifest.model_validate(state["manifest"])
        candidates = [MemoryCandidate.model_validate(item) for item in state.get("accepted", [])]
        receipt = self.store.complete_job(
            # Candidate rows and the successful job receipt commit together.
            state["job_id"],
            manifest.project_id,
            candidates,
            {
                "status": "candidate-review-required",
                "model": self.model.identity,
                "source_revision": manifest.source_revision,
                "completed_at": datetime.now(UTC).isoformat(),
                "accepted_count": len(candidates),
            },
        )
        return {"receipt": receipt}

    def run(self, job: dict) -> dict:
        """Execute one admitted job using its stable job ID as graph thread identity."""
        # A retry supplies input again and may repeat extraction. Checkpoints
        # provide inspection/history; this call does not resume a suspended node.
        return self.graph.invoke(
            {"job_id": job["job_id"], "manifest": job["payload"]},
            {"configurable": {"thread_id": f"strategic-memory:{job['job_id']}"}},
        )

    def close(self) -> None:
        """Close the owned checkpoint connection after the drainer exits."""
        self.connection.close()


def validate_model_batch(payload: dict) -> CandidateBatch:
    """Expose strict batch validation for injected adapters and focused tests."""
    return CandidateBatch.model_validate(payload)

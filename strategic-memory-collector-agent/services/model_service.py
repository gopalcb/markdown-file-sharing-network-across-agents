"""Translate a validated completion manifest into one Bedrock Qwen request.

This adapter owns credentials lookup and response parsing, not queue scheduling.
Tests inject a fake client with the same converse method to avoid AWS calls.
"""

import json
import os
from typing import Protocol

import boto3

from services.contracts_service import (
    CandidateBatch,
    CompletionManifest,
    bedrock_candidate_schema,
)

DEFAULT_MODEL_ID = "qwen.qwen3-32b-v1:0"
DEFAULT_REGION = "us-east-1"


class CandidateModel(Protocol):
    """Define the single model operation used by the LangGraph workflow."""

    @property
    def identity(self) -> str:
        """Return a non-sensitive provider and model identity."""

    def extract(self, manifest: CompletionManifest) -> CandidateBatch:
        """Extract bounded memory candidates from verified task evidence."""


class BedrockQwenModel:
    """Use Bedrock Runtime Converse structured output with Qwen3 32B."""

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, region: str = DEFAULT_REGION, client=None):
        """Retain public model configuration and create the AWS client lazily."""
        self.model_id = model_id
        self.region = region
        self.client = client

    @classmethod
    def from_environment(cls) -> "BedrockQwenModel":
        """Load public model settings while leaving credentials to the AWS provider chain."""
        return cls(
            model_id=os.environ.get("STRATEGIC_MEMORY_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
            region=os.environ.get(
                "STRATEGIC_MEMORY_AWS_REGION", os.environ.get("AWS_REGION", DEFAULT_REGION)
            ),
        )

    @property
    def identity(self) -> str:
        """Return the provider, region, and model without credential material."""
        return f"bedrock:{self.region}:{self.model_id}"

    def extract(self, manifest: CompletionManifest) -> CandidateBatch:
        """Ask Qwen for schema-constrained candidates using verified evidence only."""
        if self.client is None:
            # boto3 resolves credentials through its normal provider chain.
            # Constructing a CLI/status object therefore needs no AWS access.
            self.client = boto3.client("bedrock-runtime", region_name=self.region)
        evidence = {
            # Send supplied metadata only. Changed paths are references, not
            # permission to open files or scan the repository or transcript.
            "project_id": manifest.project_id,
            "source_revision": manifest.source_revision,
            "summary": manifest.summary,
            "changed_paths": manifest.changed_paths,
            "passed_checks": [
                {"name": check.name, "source": check.source}
                for check in manifest.checks
                if check.passed
            ],
            "allowed_evidence_sources": sorted(manifest.allowed_evidence_sources()),
        }

        response = self.client.converse(
            # Converse is a synchronous model request inside the background
            # process; the foreground enqueue command does not wait for it.
            modelId=self.model_id,
            system=[
                {
                    "text": (
                        "You are the strategic-memory-collector-agent. Extract only durable, "
                        "reusable knowledge directly supported by the supplied evidence. Never "
                        "record task status, guesses, secrets, private identity data, or failed "
                        "claims. Cite only an allowed evidence source. Return an empty candidates "
                        "array when nothing is durable enough for later human review."
                    )
                }
            ],
            messages=[{"role": "user", "content": [{"text": json.dumps(evidence)}]}],
            inferenceConfig={"maxTokens": 4096, "temperature": 0},
            outputConfig={
                # Bedrock constrains JSON shape; local Pydantic validation below
                # additionally checks lengths and candidate text policies.
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "schema": json.dumps(bedrock_candidate_schema()),
                            "name": "strategic_memory_candidates",
                            "description": "Evidence-bound candidates awaiting human review",
                        }
                    },
                }
            },
            requestMetadata={"project": manifest.project_id, "task": manifest.task_id[:256]},
        )

        if response.get("stopReason", "end_turn") != "end_turn":
            # Refusal or truncation must fail the job rather than publish partial
            # candidate data as a successful extraction.
            raise ValueError("Bedrock response did not complete successfully")
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = next((block["text"] for block in blocks if isinstance(block.get("text"), str)), None)

        if text is None:
            raise ValueError("Bedrock returned no structured text output")

        return CandidateBatch.model_validate_json(text)

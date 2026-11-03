"""Define the input and output boundaries shared by CLI, model, and graph.

CompletionManifest checks caller-supplied completion evidence. MemoryCandidate
checks extracted text. Neither model verifies external checks actually ran;
the calling workflow must supply truthful evidence and review candidate claims.
"""

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# These are heuristic rejection rules, not comprehensive secret/PII detection.
# Example email domains remain usable in fixtures and explanatory examples.
SENSITIVE_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[^\s,;]{8,}"),
    re.compile(r"(?i)\b(?![^@\s]+@(?:example\.com|example\.test)\b)[^@\s]+@[^@\s]+\.[^@\s]+\b"),
]

TRANSIENT_PATTERNS = [
    # Exclude explicit task-state wording from durable memory suggestions.
    re.compile(pattern, re.IGNORECASE)
    for pattern in [r"\bin progress\b", r"\bpending\b", r"\btemporary\b", r"\bblocked\b"]
]

ALLOWED_CATEGORIES = Literal[
    "architecture-decision",
    "failure-shield",
    "reusable-procedure",
    "stable-convention",
    "user-preference",
]


def contains_sensitive_text(value: str) -> bool:
    """Return whether bounded text contains a high-confidence secret or private identity."""
    return any(pattern.search(value) for pattern in SENSITIVE_PATTERNS)


def validate_relative_path(value: str) -> str:
    """Require a bounded repository-relative path outside sensitive local state."""
    if not value or len(value) > 300 or "\\" in value:
        raise ValueError("changed paths must be nonempty bounded POSIX paths")

    path = PurePosixPath(value)
    # Paths remain descriptive references; this validator does not read files.
    if path.is_absolute() or ".." in path.parts or path.parts[0] in {".git", ".env"}:
        raise ValueError("changed path escapes the allowed repository source boundary")

    return value


class CheckEvidence(BaseModel):
    """Describe one completed verification without embedding its raw output."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    passed: bool
    source: str = Field(min_length=1, max_length=300)

    @field_validator("name", "source")
    @classmethod
    def reject_sensitive_evidence(cls, value: str) -> str:
        """Keep credentials and private identities out of durable evidence metadata."""
        if contains_sensitive_text(value):
            raise ValueError("sensitive evidence metadata is not allowed")

        return value.strip()


class CompletionManifest(BaseModel):
    """Represent a fully completed task eligible for background memory extraction."""

    # Unexpected fields fail validation instead of silently entering evidence.
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}$")
    status: Literal["complete"]
    source_revision: str = Field(pattern=r"^[a-fA-F0-9]{7,64}$")
    completed_at: datetime
    summary: str = Field(min_length=20, max_length=20000)
    changed_paths: list[str] = Field(default_factory=list, max_length=100)
    checks: list[CheckEvidence] = Field(min_length=1, max_length=50)

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: list[str]) -> list[str]:
        """Normalize unique safe paths while retaining the caller's order."""
        return list(dict.fromkeys(validate_relative_path(value) for value in values))

    @field_validator("summary")
    @classmethod
    def reject_sensitive_summary(cls, value: str) -> str:
        """Reject manifests that would send sensitive task material to a model."""
        if contains_sensitive_text(value):
            raise ValueError("completion summary contains sensitive material")

        return value.strip()

    @model_validator(mode="after")
    def require_verified_completion(self) -> "CompletionManifest":
        """Require timezone-aware completion and successful verification evidence."""
        if self.completed_at.tzinfo is None:
            raise ValueError("completed_at must include a timezone")
        if not any(check.passed for check in self.checks):
            # At least one passed check is required. This does not require every
            # supplied check to pass; the parent decides when work is complete.
            raise ValueError("at least one passed check is required")

        return self

    def allowed_evidence_sources(self) -> set[str]:
        """Return the exact evidence references candidates may cite."""
        sources = {f"check:{check.name}" for check in self.checks if check.passed}
        # Failed checks cannot be cited as passed verification. The summary and
        # changed paths can also be cited, but do not imply storage read-back.
        sources.update(f"path:{path}" for path in self.changed_paths)
        sources.add("summary")
        return sources


class MemoryCandidate(BaseModel):
    """Represent one untrusted strategic-memory candidate awaiting human review."""

    model_config = ConfigDict(extra="forbid")

    category: ALLOWED_CATEGORIES
    content: str = Field(min_length=20, max_length=2000)
    scope: str = Field(min_length=1, max_length=200)
    evidence_sources: list[str] = Field(min_length=1, max_length=10)
    confidence: Literal["medium", "high"]
    revalidate_after: str = Field(min_length=1, max_length=120)

    @field_validator("content", "scope", "revalidate_after")
    @classmethod
    def reject_sensitive_candidate_text(cls, value: str) -> str:
        """Reject sensitive or explicitly transient candidate text."""
        if contains_sensitive_text(value):
            raise ValueError("candidate contains sensitive material")

        if any(pattern.search(value) for pattern in TRANSIENT_PATTERNS):
            raise ValueError("candidate describes transient task state")

        return value.strip()

    @field_validator("evidence_sources")
    @classmethod
    def normalize_evidence_sources(cls, values: list[str]) -> list[str]:
        """Deduplicate bounded evidence references without inventing provenance."""
        if any(not value or len(value) > 320 for value in values):
            raise ValueError("candidate evidence sources must be bounded")

        return list(dict.fromkeys(values))


class CandidateBatch(BaseModel):
    """Constrain the Bedrock model to a bounded candidate collection."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=12)


def bedrock_candidate_schema() -> dict:
    """Return the Bedrock-supported JSON Schema subset for structured extraction."""
    # Keep provider JSON Schema separate from Pydantic's richer local schema:
    # provider constraints differ, so text lengths and policy checks remain local.
    candidate = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "architecture-decision",
                    "failure-shield",
                    "reusable-procedure",
                    "stable-convention",
                    "user-preference",
                ],
            },
            "content": {"type": "string"},
            "scope": {"type": "string"},
            "evidence_sources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "confidence": {"type": "string", "enum": ["medium", "high"]},
            "revalidate_after": {"type": "string"},
        },
        "required": [
            "category",
            "content",
            "scope",
            "evidence_sources",
            "confidence",
            "revalidate_after",
        ],
        "additionalProperties": False,
    }

    return {
        "type": "object",
        "properties": {"candidates": {"type": "array", "items": candidate}},
        "required": ["candidates"],
        "additionalProperties": False,
    }

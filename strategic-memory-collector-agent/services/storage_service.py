"""Own local queue admission, crash recovery, and review-only candidate records.

Each method opens its own SQLite connection. Write transactions serialize queue
changes; a process-level file lock keeps drainers from executing inference in
parallel. Candidate insertion and the successful receipt share one transaction.
"""

import fcntl
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from services.contracts_service import CompletionManifest, MemoryCandidate

MAX_ATTEMPTS = 3


def canonical(value: dict) -> str:
    """Encode a record deterministically for identity and conflict checks."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CollectorStore:
    """Own transactional queue admission, leases, receipts, and candidate storage."""

    def __init__(self, data_dir: str | Path):
        """Initialize ignored runtime storage without retaining an open connection."""
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        # Restrict access to this runtime directory, which contains task summaries
        # and model output even though high-confidence secret patterns are rejected.
        self.database = self.data_dir / "strategic-memory.sqlite"
        with self.connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            # WAL lets status readers coexist with a writer. SQLite still has
            # one writer at a time; inference never runs inside a transaction.
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL,
                    error_type TEXT,
                    receipt TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(project_id, content_digest)
                );
                """
            )

    @contextmanager
    def connect(self):
        """Open one bounded-wait SQLite transaction owned by the caller."""
        database = sqlite3.connect(self.database, timeout=15)
        database.row_factory = sqlite3.Row
        try:
            with database:
                # sqlite3 commits on normal exit and rolls back on exceptions.
                yield database
        finally:
            database.close()

    def enqueue(self, manifest: CompletionManifest) -> tuple[dict, bool]:
        """Admit identical completion evidence once and return its durable job."""
        payload = canonical(manifest.model_dump(mode="json"))
        identity = manifest.model_dump(mode="json", exclude={"completed_at"})
        # Repeating the same completion with a new timestamp is the same job.
        # Changing the summary, checks, paths, task, project, or revision creates
        # a new identity because the evidence being collected has changed.
        job_id = hashlib.sha256(canonical(identity).encode()).hexdigest()[:32]
        now = time.time()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            # Reserve the write transaction before insert/read so simultaneous
            # callers all receive the original admitted row for this identity.
            cursor = database.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id,payload,status,created_at,updated_at
                ) VALUES(?,?,'pending',?,?)
                """,
                (job_id, payload, now, now),
            )
            row = database.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self.public_job(row), cursor.rowcount == 1

    def claim_next(self, lease_seconds: int = 300) -> dict | None:
        """Transactionally claim the oldest pending or expired in-flight job."""
        now = time.time()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            # Exhausted interrupted jobs are marked failed before selecting work;
            # they must not prevent newer pending jobs from being processed.
            database.execute(
                """
                UPDATE jobs SET status='failed', error_type='attempt_limit', updated_at=?
                WHERE status='running' AND lease_until < ? AND attempts >= ?
                """,
                (now, now, MAX_ATTEMPTS),
            )
            row = database.execute(
                """
                SELECT * FROM jobs
                WHERE attempts < ? AND (
                    status='pending' OR (status='running' AND lease_until < ?)
                )
                ORDER BY created_at, job_id LIMIT 1
                """,
                (MAX_ATTEMPTS, now),
            ).fetchone()
            if row is None:
                return None
            attempts = row["attempts"] + 1
            # Commit running state and lease before the caller invokes Bedrock.
            # A fresh process can reclaim an expired lease after interruption.
            database.execute(
                """
                UPDATE jobs SET status='running', attempts=?, lease_until=?, error_type=NULL,
                    updated_at=? WHERE job_id=?
                """,
                (attempts, now + lease_seconds, now, row["job_id"]),
            )
            claimed = database.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return {**self.public_job(claimed), "payload": json.loads(claimed["payload"])}

    def complete_job(
        self, job_id: str, project_id: str, candidates: list[MemoryCandidate], receipt: dict
    ) -> dict:
        """Persist deduplicated candidates and the successful job receipt atomically."""
        now = time.time()
        candidate_ids = []
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            for candidate in candidates:
                # Retain the originating job so review can find the full admitted
                # manifest. Cross-job duplicates keep the first stored provenance.
                payload = canonical(
                    {
                        **candidate.model_dump(mode="json"),
                        "source_job_id": job_id,
                    }
                )
                content_digest = hashlib.sha256(candidate.content.casefold().encode()).hexdigest()
                # Project-scoped uniqueness prevents case-only duplicate content.
                # Different projects can retain their own copy of the same text.
                candidate_id = hashlib.sha256(
                    f"{project_id}:{content_digest}".encode()
                ).hexdigest()[:32]
                database.execute(
                    "INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?)",
                    (candidate_id, project_id, content_digest, payload, now),
                )
                candidate_ids.append(candidate_id)
            final_receipt = {**receipt, "candidate_ids": sorted(set(candidate_ids))}
            # A crash before commit rolls back candidates and success together.
            database.execute(
                """
                UPDATE jobs SET status='succeeded', lease_until=NULL, receipt=?, updated_at=?
                WHERE job_id=?
                """,
                (canonical(final_receipt), now, job_id),
            )
        return final_receipt

    def fail_job(self, job_id: str, error: Exception) -> None:
        """Record a bounded error type without persisting provider response bodies."""
        # Exception messages can include request bodies or provider diagnostics;
        # only the class name is safe enough for routine status output here.
        with self.connect() as database:
            database.execute(
                """
                UPDATE jobs SET status='failed', lease_until=NULL, error_type=?, updated_at=?
                WHERE job_id=?
                """,
                (type(error).__name__[:120], time.time(), job_id),
            )

    def retry(self, job_id: str) -> dict:
        """Return one failed job to pending while respecting the attempt limit."""
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "failed" or row["attempts"] >= MAX_ATTEMPTS:
                raise ValueError("only retryable failed jobs can return to pending")
            database.execute(
                "UPDATE jobs SET status='pending', error_type=NULL, updated_at=? WHERE job_id=?",
                (time.time(), job_id),
            )
            updated = database.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self.public_job(updated)

    def status(self) -> dict:
        """Return operational counts without exposing evidence or candidate content."""
        with self.connect() as database:
            jobs = {
                row["status"]: row["count"]
                for row in database.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                ).fetchall()
            }
            candidate_count = database.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        return {"jobs": jobs, "candidate_count": candidate_count}

    def list_candidates(self, project_id: str) -> list[dict]:
        """Return reviewable candidate records within one explicit project scope."""
        with self.connect() as database:
            rows = database.execute(
                """
                SELECT candidate_id,payload,created_at FROM candidates
                WHERE project_id=? ORDER BY created_at,candidate_id
                """,
                (project_id,),
            ).fetchall()
        return [
            {
                "candidate_id": row["candidate_id"],
                "status": "candidate",
                "candidate": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def public_job(self, row: sqlite3.Row) -> dict:
        """Project a job without its completion evidence body."""
        return {
            "job_id": row["job_id"],
            "status": row["status"],
            "attempts": row["attempts"],
            "error_type": row["error_type"],
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
        }

    @contextmanager
    def drainer_lock(self):
        """Allow only one detached queue drainer for this runtime directory."""
        lock_path = self.data_dir / "drainer.lock"
        with lock_path.open("a+") as lock_file:
            # The OS releases flock when this descriptor/process closes, even
            # after a crash. The lock file's continued existence is not ownership.
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

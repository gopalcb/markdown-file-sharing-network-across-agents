"""Own the foreground CLI and the detached worker's process lifecycle.

The enqueue command validates and commits evidence, starts another Python process,
and returns its job ID. Only the drain command executes LangGraph and Bedrock work.
Storage operations live in services/storage_service.py; model and candidate policy live elsewhere.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from services.contracts_service import CompletionManifest
from services.model_service import BedrockQwenModel
from services.storage_service import CollectorStore
from services.workflow_service import StrategicMemoryWorkflow

MAX_MANIFEST_BYTES = 256_000
DEFAULT_DATA_DIR = Path.cwd() / ".runtime" / "strategic-memory-collector-agent"
MAX_LOG_BYTES = 2_000_000


def load_manifest(path: str | Path) -> CompletionManifest:
    """Load one bounded JSON manifest without following a symlink."""
    manifest_path = Path(path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("manifest must be a regular file")
    content = manifest_path.read_bytes()
    # Reject large input before parsing JSON or handing its summary to the model.
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the maximum size")
    return CompletionManifest.model_validate_json(content)


def rotate_log(path: Path) -> None:
    """Keep one bounded previous collector log without reading its contents."""
    if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
        path.replace(path.with_suffix(".log.1"))


def start_drainer(data_dir: Path) -> int:
    """Start a detached one-shot drainer and return its local process ID."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "collector.log"
    rotate_log(log_path)
    with log_path.open("ab") as log_file:
        # Reuse this environment's interpreter so the child has the installed
        # dependencies. A new session and disconnected stdin let the CLI return
        # while the worker continues; OS shutdown can still interrupt that worker.
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cli",
                "--data-dir",
                str(data_dir),
                "drain",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
            cwd=data_dir,
            env=os.environ.copy(),
        )
    return process.pid


def drain_jobs(store: CollectorStore, model=None) -> int:
    """Drain admitted jobs serially while holding the runtime's single-writer lock."""
    with store.drainer_lock() as acquired:
        # Multiple enqueues can start children; only the child holding this lock
        # processes jobs. Other children exit without competing for model work.
        if not acquired:
            return 0
        collector_model = model or BedrockQwenModel.from_environment()
        workflow = StrategicMemoryWorkflow(
            store, collector_model, store.data_dir / "checkpoints.sqlite"
        )
        completed = 0
        try:
            while job := store.claim_next():
                # Claim commits before external inference. If the process dies,
                # a later drain can reclaim the job after its lease expires.
                try:
                    workflow.run(job)
                    completed += 1
                except Exception as error:  # noqa: BLE001 - persist safe type and continue queue
                    store.fail_job(job["job_id"], error)
                    logging.error(
                        "Collector job %s failed: %s", job["job_id"], type(error).__name__
                    )
        finally:
            # Close checkpoints even after provider failures or an empty queue.
            workflow.close()
        return completed


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line contract for admission and local operations."""
    parser = argparse.ArgumentParser(prog="strategic-memory-collector-agent")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("STRATEGIC_MEMORY_DATA_DIR", str(DEFAULT_DATA_DIR)),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--manifest", required=True)
    commands.add_parser("drain")
    commands.add_parser("status")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id")
    candidates = commands.add_parser("candidates")
    candidates.add_argument("--project", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute one bounded collector command and print only safe structured status."""
    arguments = build_parser().parse_args(argv)
    data_dir = Path(arguments.data_dir).resolve()
    store = CollectorStore(data_dir)
    if arguments.command == "enqueue":
        # Persist first: starting a process alone is not durable queue admission.
        job, created = store.enqueue(load_manifest(arguments.manifest))
        pid = start_drainer(data_dir) if job["status"] in {"pending", "running"} else None
        print(json.dumps({**job, "created": created, "drainer_pid": pid}, sort_keys=True))
        return 0
    if arguments.command == "drain":
        print(json.dumps({"completed": drain_jobs(store)}, sort_keys=True))
        return 0
    if arguments.command == "status":
        print(json.dumps(store.status(), sort_keys=True))
        return 0
    if arguments.command == "retry":
        # Retry is explicit and capped by storage; failures do not spin endlessly.
        job = store.retry(arguments.job_id)
        pid = start_drainer(data_dir)
        print(json.dumps({**job, "drainer_pid": pid}, sort_keys=True))
        return 0
    if arguments.command == "candidates":
        print(json.dumps(store.list_candidates(arguments.project), sort_keys=True))
        return 0
    raise ValueError("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())

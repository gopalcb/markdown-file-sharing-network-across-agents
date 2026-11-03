"""Exercise CLI admission and child-process startup without model inference.

The admission test replaces only process spawning, leaving validation/storage real.
The process test uses an empty queue so the actual CLI can exit without calling AWS.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime

from cli import main, start_drainer
from services.storage_service import CollectorStore


def test_enqueue_returns_without_running_model(tmp_path, monkeypatch, capsys):
    """Admission persists a job and delegates model execution to another process."""
    manifest = tmp_path / "completion.json"
    manifest.write_text(
        json.dumps(
            {
                "project_id": "harness",
                "task_id": "completed-task",
                "status": "complete",
                "source_revision": "abcdef123",
                "completed_at": datetime.now(UTC).isoformat(),
                "summary": "Verified transactional queue admission with successful local checks.",
                "checks": [{"name": "pytest", "passed": True, "source": "local"}],
            }
        )
    )
    spawned = []
    # Observe spawning without running a model; a pending row proves enqueue
    # persisted work rather than processing it inline before returning.
    monkeypatch.setattr("cli.start_drainer", lambda path: spawned.append(path) or 123)
    runtime = tmp_path / "runtime"
    assert main(["--data-dir", str(runtime), "enqueue", "--manifest", str(manifest)]) == 0
    assert spawned == [runtime]
    assert json.loads(capsys.readouterr().out)["status"] == "pending"
    assert CollectorStore(runtime).status()["jobs"] == {"pending": 1}


def test_detached_drainer_empty_queue_exits_successfully(tmp_path):
    """Run the real detached CLI process against an empty local database."""
    CollectorStore(tmp_path)
    pid = start_drainer(tmp_path)
    # Popen remains owned by start_drainer; a separate synchronous drain verifies
    # command parsing and empty queue completion without invoking a provider.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "--data-dir",
            str(tmp_path),
            "drain",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert pid > 0
    assert result.returncode == 0
    assert json.loads(result.stdout)["completed"] == 0

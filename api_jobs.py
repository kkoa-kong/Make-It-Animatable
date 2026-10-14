from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


class JobStore:
    """Persist job metadata while retaining results in one directory per job."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._database = self.root / "jobs.sqlite3"

    def initialize_schema(self) -> None:
        """Create or migrate metadata without changing any job's state."""
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK (
                            status IN ('queued', 'running', 'succeeded', 'failed')
                        ),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error TEXT,
                        error_code INTEGER,
                        request TEXT NOT NULL,
                        worker_id TEXT,
                        worker_run_id TEXT
                    )
                    """
                )
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(jobs)")
                }
                for column in ("worker_id", "worker_run_id"):
                    if column not in columns:
                        connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
                # SQLite indexes include rowid, preserving FIFO within queued rows.
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_queued "
                    "ON jobs(status) WHERE status = 'queued'"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_running_owner "
                    "ON jobs(worker_run_id) WHERE status = 'running'"
                )
        finally:
            connection.close()

    def recover_interrupted(self) -> int:
        """Fail interrupted running work; called once by the owning supervisor."""
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', updated_at = ?, error = ?, error_code = ?
                    WHERE status = 'running'
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        "Job interrupted by a server restart",
                        500,
                    ),
                )
                return result.rowcount
        finally:
            connection.close()

    def create(
        self,
        request: dict,
        *,
        input_file: Path | None = None,
        animation_file: Path | None = None,
    ) -> dict:
        """Create a queued job, rolling back both resources on ordinary failure."""
        request_json = json.dumps(request)
        job_id = uuid4().hex
        job_directory = self.directory(job_id)
        directory_created = False
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            with connection:
                # Serialize creation before assigning timestamps and creating the
                # directory, so pending jobs have an unambiguous insertion order.
                connection.execute("BEGIN IMMEDIATE")
                timestamp = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, status, created_at, updated_at, error, error_code, request
                    ) VALUES (?, 'queued', ?, ?, NULL, NULL, ?)
                    """,
                    (job_id, timestamp, timestamp, request_json),
                )
                job_directory.mkdir()
                directory_created = True
                if input_file is not None:
                    input_file.replace(job_directory / "character.glb")
                if animation_file is not None:
                    animation_file.replace(job_directory / "animation.fbx")
        except BaseException:
            if directory_created:
                (job_directory / "character.glb").unlink(missing_ok=True)
                (job_directory / "animation.fbx").unlink(missing_ok=True)
                job_directory.rmdir()
            raise
        finally:
            connection.close()
        return {
            "job_id": job_id,
            "status": "queued",
            "created_at": timestamp,
            "updated_at": timestamp,
            "error": None,
            "error_code": None,
            "request": json.loads(request_json),
        }

    def get(self, job_id: str) -> dict | None:
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT job_id, status, created_at, updated_at, error, error_code, request, "
                "worker_id, worker_run_id FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        job = dict(row)
        job["request"] = json.loads(job["request"])
        return job

    def claim_next(self, worker_id: str, worker_run_id: str) -> dict | None:
        """Atomically assign the earliest queued job to one worker incarnation."""
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM jobs WHERE status = 'queued' ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                job = dict(row)
                job["request"] = json.loads(job["request"])
                timestamp = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', updated_at = ?, worker_id = ?, worker_run_id = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (timestamp, worker_id, worker_run_id, job["job_id"]),
                )
                job.update(
                    status="running",
                    updated_at=timestamp,
                    worker_id=worker_id,
                    worker_run_id=worker_run_id,
                )
            return job
        finally:
            connection.close()

    def finish(
        self,
        job_id: str,
        worker_run_id: str,
        status: str,
        *,
        error: str | None = None,
        error_code: int | None = None,
    ) -> bool:
        """Complete running work only if this worker incarnation still owns it."""
        if status not in ("succeeded", "failed"):
            raise ValueError("Completion status must be succeeded or failed")
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, error = ?, error_code = ?
                    WHERE job_id = ? AND worker_run_id = ? AND status = 'running'
                    """,
                    (
                        status,
                        datetime.now(timezone.utc).isoformat(),
                        error,
                        error_code,
                        job_id,
                        worker_run_id,
                    ),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def fail_worker(
        self, worker_run_id: str, *, error: str, error_code: int = 500
    ) -> int:
        """Fail only unfinished work owned by an exited worker incarnation."""
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', updated_at = ?, error = ?, error_code = ?
                    WHERE worker_run_id = ? AND status = 'running'
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        error,
                        error_code,
                        worker_run_id,
                    ),
                )
                return result.rowcount
        finally:
            connection.close()

    def running_count(self) -> int:
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
            ).fetchone()[0]
        finally:
            connection.close()

    def directory(self, job_id: str) -> Path:
        """Return a job path only for canonical lowercase UUID hex identifiers."""
        try:
            valid = isinstance(job_id, str) and UUID(job_id).hex == job_id
        except (ValueError, AttributeError, TypeError):
            valid = False
        if not valid:
            raise ValueError("job_id must be a 32-character lowercase UUID hex string")
        return self.root / job_id

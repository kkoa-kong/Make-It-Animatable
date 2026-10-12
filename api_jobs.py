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

    def initialize(self) -> None:
        """Create the store and fail work interrupted by a previous process."""
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            with connection:
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
                        request TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
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
                "SELECT job_id, status, created_at, updated_at, error, error_code, request "
                "FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        job = dict(row)
        job["request"] = json.loads(job["request"])
        return job

    def pending(self) -> list[str]:
        """Return queued identifiers in persistent creation order."""
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE status = 'queued' ORDER BY rowid"
            ).fetchall()
        finally:
            connection.close()
        return [row[0] for row in rows]

    def update(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        error_code: int | None = None,
    ) -> None:
        if status not in ("queued", "running", "succeeded", "failed"):
            raise ValueError("Unknown job status")
        connection = sqlite3.connect(self._database, timeout=30)
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, error = ?, error_code = ?
                    WHERE job_id = ?
                    """,
                    (
                        status,
                        datetime.now(timezone.utc).isoformat(),
                        error,
                        error_code,
                        job_id,
                    ),
                )
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

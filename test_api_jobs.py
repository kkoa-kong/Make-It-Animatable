import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from api_jobs import JobStore


def _call_store(root, method, args, kwargs, results):
    try:
        result = getattr(JobStore(Path(root)), method)(*args, **kwargs)
        results.put({"result": result})
    except BaseException as error:
        results.put({"error": repr(error)})
        raise


def _claim_jobs(root, worker_id, worker_run_id, barrier, results):
    try:
        store = JobStore(Path(root))
        barrier.wait(timeout=30)
        first = store.claim_next(worker_id, worker_run_id)
        claims = [] if first is None else [first]
        # Every process must have claimed before any can drain the rest.
        barrier.wait(timeout=30)
        while True:
            job = store.claim_next(worker_id, worker_run_id)
            if job is None:
                break
            claims.append(job)
        results.put({"result": claims})
    except BaseException as error:
        results.put({"error": repr(error)})
        raise


class StoreProcessTestCase(unittest.TestCase):
    def process_call(self, method, *args, **kwargs):
        results = self.context.Queue()
        process = self.context.Process(
            target=_call_store,
            args=(str(self.root), method, args, kwargs, results),
        )
        process.start()
        try:
            response = results.get(timeout=30)
            process.join(timeout=30)
            self.assertFalse(process.is_alive(), "store operation did not exit")
            self.assertEqual(process.exitcode, 0, response)
            self.assertNotIn("error", response)
            return response["result"]
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=30)
            process.close()
            results.close()
            results.join_thread()


class JobStoreTests(StoreProcessTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = JobStore(self.root)
        self.store.initialize_schema()
        self.context = multiprocessing.get_context("spawn")

    def test_processes_claim_each_job_exactly_once(self):
        jobs = [self.store.create({"number": number}) for number in range(24)]
        worker_count = 6
        barrier = self.context.Barrier(worker_count)
        results = self.context.Queue()
        processes = [
            self.context.Process(
                target=_claim_jobs,
                args=(str(self.root), f"worker-{number}", f"run-{number}", barrier, results),
            )
            for number in range(worker_count)
        ]
        for process in processes:
            process.start()
        try:
            responses = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive(), "claiming process did not exit")
                self.assertEqual(process.exitcode, 0)
            claims = []
            for response in responses:
                self.assertNotIn("error", response)
                self.assertGreaterEqual(len(response["result"]), 1)
                claims.extend(response["result"])
            identifiers = [job["job_id"] for job in claims]
            self.assertEqual(len(identifiers), len(set(identifiers)))
            self.assertEqual(set(identifiers), {job["job_id"] for job in jobs})
            self.assertEqual(self.store.running_count(), len(jobs))
            for claim in claims:
                self.assertEqual(claim["status"], "running")
                self.assertEqual(claim["worker_run_id"], claim["worker_id"].replace("worker", "run"))
                self.assertEqual(self.store.get(claim["job_id"]), claim)
            self.assertIsNone(self.process_call("claim_next", "worker-last", "run-last"))
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=30)
                process.close()
            results.close()
            results.join_thread()

    def test_claim_order_is_insertion_order_not_timestamp_or_identifier(self):
        jobs = [self.store.create({"number": number}) for number in range(4)]
        connection = sqlite3.connect(self.root / "jobs.sqlite3")
        try:
            with connection:
                for number, job in enumerate(jobs):
                    connection.execute(
                        "UPDATE jobs SET created_at = ? WHERE job_id = ?",
                        (str(10 - number), job["job_id"]),
                    )
        finally:
            connection.close()
        for expected in jobs:
            claim = self.process_call("claim_next", "worker", "run")
            self.assertEqual(claim["job_id"], expected["job_id"])
            self.assertEqual(claim["request"], expected["request"])
        self.assertIsNone(self.process_call("claim_next", "worker", "run"))

    def test_wrong_and_stale_worker_runs_cannot_finish(self):
        first = self.store.create({"number": 1})
        queued = self.store.get(first["job_id"])
        self.assertFalse(self.process_call("finish", first["job_id"], "old-run", "succeeded"))
        self.assertEqual(self.store.get(first["job_id"]), queued)
        self.process_call("claim_next", "worker", "old-run")
        before = self.store.get(first["job_id"])
        self.assertFalse(self.process_call("finish", first["job_id"], "wrong-run", "failed", error="wrong"))
        self.assertEqual(self.store.get(first["job_id"]), before)
        self.assertTrue(self.process_call("finish", first["job_id"], "old-run", "succeeded"))
        completed = self.store.get(first["job_id"])
        self.assertFalse(self.process_call("finish", first["job_id"], "old-run", "failed", error="late"))
        self.assertEqual(self.store.get(first["job_id"]), completed)

        second = self.store.create({"number": 2})
        self.process_call("claim_next", "worker", "new-run")
        before = self.store.get(second["job_id"])
        self.assertFalse(self.process_call("finish", second["job_id"], "old-run", "succeeded"))
        self.assertEqual(self.store.get(second["job_id"]), before)
        self.assertEqual(self.process_call("fail_worker", "old-run", error="late worker exit"), 0)
        self.assertEqual(self.store.get(second["job_id"]), before)
        self.assertTrue(self.process_call("finish", second["job_id"], "new-run", "failed", error="bad input", error_code=422))
        failed = self.store.get(second["job_id"])
        self.assertEqual((failed["status"], failed["error"], failed["error_code"]), ("failed", "bad input", 422))
        self.assertEqual(self.store.running_count(), 0)

    def test_finish_rejects_nonterminal_status_without_changing_job(self):
        job = self.store.create({})
        self.store.claim_next("worker", "run")
        before = self.store.get(job["job_id"])
        for status in ("queued", "running", "unknown"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.finish(job["job_id"], "run", status)
            self.assertEqual(self.store.get(job["job_id"]), before)

    def test_worker_failure_changes_only_its_owned_running_jobs(self):
        succeeded, failed, running_a, running_b, other, queued = [
            self.store.create({"number": number}) for number in range(6)
        ]
        for _ in range(4):
            self.process_call("claim_next", "worker", "owned-run")
        self.process_call("claim_next", "worker", "different-run")
        self.process_call("finish", succeeded["job_id"], "owned-run", "succeeded")
        self.process_call("finish", failed["job_id"], "owned-run", "failed", error="invalid mesh", error_code=422)
        unchanged = {
            job["job_id"]: self.store.get(job["job_id"])
            for job in (succeeded, failed, other, queued)
        }
        self.assertEqual(self.store.running_count(), 3)
        self.assertEqual(self.process_call("fail_worker", "owned-run", error="worker exited", error_code=503), 2)
        for job in (running_a, running_b):
            persisted = self.store.get(job["job_id"])
            self.assertEqual((persisted["status"], persisted["error"], persisted["error_code"]), ("failed", "worker exited", 503))
            self.assertEqual(persisted["worker_run_id"], "owned-run")
        for identifier, before in unchanged.items():
            self.assertEqual(self.store.get(identifier), before)
        self.assertEqual(self.store.running_count(), 1)
        self.assertEqual(self.process_call("fail_worker", "owned-run", error="second exit"), 0)
        for identifier, before in unchanged.items():
            self.assertEqual(self.store.get(identifier), before)

    def test_upload_failure_rolls_back_record_and_job_directory(self):
        staged = self.root / "staged.glb"
        staged.write_bytes(b"character")
        with self.assertRaises(FileNotFoundError):
            self.store.create({}, input_file=staged, animation_file=self.root / "missing.fbx")
        self.assertIsNone(self.store.claim_next("worker", "run"))
        self.assertEqual([path for path in self.root.iterdir() if path.is_dir()], [])

    def test_directory_validation_and_missing_job_lookup(self):
        identifier = uuid4().hex
        self.assertEqual(self.store.directory(identifier), self.root / identifier)
        self.assertIsNone(self.store.get(identifier))
        for invalid in ("../outside", identifier.upper(), str(uuid4()), "", None):
            with self.subTest(identifier=invalid), self.assertRaises(ValueError):
                self.store.directory(invalid)


class LegacyMigrationTests(StoreProcessTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = JobStore(self.root)
        self.context = multiprocessing.get_context("spawn")

    # This fixture deliberately starts without a schema.
    def test_migration_and_recovery_preserve_records_files_and_fifo(self):
        rows = [
            ("ffffffffffffffffffffffffffffffff", "queued", "created-0", "updated-0", None, None, '{"number": 0}'),
            ("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "running", "created-1", "updated-1", None, None, '{"number": 1}'),
            ("dddddddddddddddddddddddddddddddd", "succeeded", "created-2", "updated-2", None, None, '{"number": 2}'),
            ("cccccccccccccccccccccccccccccccc", "failed", "created-3", "updated-3", "old error", 422, '{"number": 3}'),
            ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "queued", "created-4", "updated-4", None, None, '{"number": 4}'),
        ]
        connection = sqlite3.connect(self.root / "jobs.sqlite3")
        try:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error TEXT,
                        error_code INTEGER,
                        request TEXT NOT NULL
                    )
                    """
                )
                connection.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        finally:
            connection.close()
        files = {}
        for row in rows:
            directory = self.store.directory(row[0])
            directory.mkdir()
            files[directory / "result.glb"] = b"existing result " + row[0].encode()
            files[directory / "manifest.json"] = b'{"existing": true}'
        for path, contents in files.items():
            path.write_bytes(contents)

        self.process_call("initialize_schema")
        before = {row[0]: self.store.get(row[0]) for row in rows}
        for row in rows:
            job = before[row[0]]
            self.assertEqual(tuple(job[key] for key in ("job_id", "status", "created_at", "updated_at", "error", "error_code")), row[:6])
            self.assertEqual(job["request"], {"number": rows.index(row)})
            self.assertIsNone(job["worker_id"])
            self.assertIsNone(job["worker_run_id"])
        self.process_call("initialize_schema")
        self.assertEqual({row[0]: self.store.get(row[0]) for row in rows}, before)
        self.assertEqual(self.process_call("recover_interrupted"), 1)
        for row in rows:
            job = self.store.get(row[0])
            if row[1] != "running":
                self.assertEqual(job, before[row[0]])
            else:
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["error_code"], 500)
                self.assertEqual(job["created_at"], row[2])
                self.assertEqual(job["request"], before[row[0]]["request"])
        recovered = {row[0]: self.store.get(row[0]) for row in rows}
        self.assertEqual(self.process_call("recover_interrupted"), 0)
        self.assertEqual({row[0]: self.store.get(row[0]) for row in rows}, recovered)
        for path, contents in files.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(self.process_call("claim_next", "worker", "run")["job_id"], rows[0][0])
        self.assertEqual(self.process_call("claim_next", "worker", "run")["job_id"], rows[4][0])
        self.assertIsNone(self.process_call("claim_next", "worker", "run"))


if __name__ == "__main__":
    unittest.main()

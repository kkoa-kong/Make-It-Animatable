import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from api_config import GPUGroup, load_config
from api_jobs import JobStore
from api_supervisor import SingletonBusyError, Supervisor


# A real lightweight child exercises process ownership and shutdown, not GPU
# inference. Production never selects this code or substitutes fake results.
CHILD = r'''
import json, os, socket, sys, time
from pathlib import Path
from api_config import Settings
from api_jobs import JobStore
settings = Settings.from_json(os.environ['MIA_WORKER_SETTINGS'])
worker_id, run_id, fd = sys.argv[1:]
control = socket.socket(fileno=int(fd))
jobs = JobStore(settings.jobs_dir)
control.sendall(b'{"status":"ready"}\n')
job = jobs.claim_next(worker_id, run_id)
Path(settings.jobs_dir / (worker_id + '.claimed')).write_text(job['job_id'] if job else '')
while control.recv(1024):
    pass
if job:
    jobs.finish(job['job_id'], run_id, 'succeeded')
control.close()
'''


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition did not become true')


class SupervisorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            self.settings = replace(load_config(), jobs_dir=Path(self.temporary.name),
                                    gpu_groups=(GPUGroup(4, 2),), shutdown_timeout_seconds=2)
        self.jobs = JobStore(self.settings.jobs_dir)
        self.jobs.initialize_schema()
        self.supervisor = Supervisor(self.settings, self.jobs)
        self.addCleanup(self.supervisor.close)

    def start_children(self):
        original = subprocess.Popen

        def launch(command, **kwargs):
            return original([sys.executable, '-c', CHILD, command[3], command[5], command[7]], **kwargs)

        with patch.object(self.supervisor, '_prepare_checkpoints'), patch('api_supervisor.subprocess.Popen', side_effect=launch):
            self.supervisor.start()
        until(lambda: self.supervisor.snapshot()['max_concurrency'] == 2)
        until(lambda: all((self.settings.jobs_dir / (spec.worker_id + '.claimed')).exists()
                          for spec in self.settings.worker_specs()))

    def test_worker_exit_fails_only_its_job_and_other_worker_drains(self):
        jobs = [self.jobs.create({'number': number}) for number in range(3)]
        self.start_children()
        self.assertEqual(self.jobs.running_count(), 2)
        first, second = self.supervisor._workers
        first_job = (self.settings.jobs_dir / (first.spec.worker_id + '.claimed')).read_text()
        second_job = (self.settings.jobs_dir / (second.spec.worker_id + '.claimed')).read_text()
        first.process.kill()
        until(lambda: self.jobs.get(first_job)['status'] == 'failed')
        self.assertEqual(self.jobs.get(second_job)['status'], 'running')
        self.assertEqual(self.supervisor.snapshot()['max_concurrency'], 1)
        self.supervisor.begin_stop()
        self.assertEqual(self.supervisor.snapshot()['max_concurrency'], 0)
        self.supervisor.close()
        self.assertEqual(self.jobs.get(second_job)['status'], 'succeeded')
        remaining = {job['job_id'] for job in jobs} - {first_job, second_job}
        self.assertEqual(self.jobs.get(remaining.pop())['status'], 'queued')
        self.assertTrue(all(worker.process.poll() is not None for worker in self.supervisor._workers))

    def test_duplicate_supervisor_cannot_recover_live_tasks(self):
        self.jobs.create({})
        self.start_children()
        other = Supervisor(self.settings, self.jobs)
        with self.assertRaises(SingletonBusyError):
            other.start()
        self.assertEqual(self.jobs.running_count(), 1)
        other.close()
        self.supervisor.close()
        replacement = Supervisor(self.settings, self.jobs)
        try:
            with patch.object(replacement, '_prepare_checkpoints', side_effect=FileNotFoundError('missing assets')):
                replacement.start()
            self.assertEqual(replacement.snapshot()['status'], 'error')
        finally:
            replacement.close()


if __name__ == '__main__':
    unittest.main()

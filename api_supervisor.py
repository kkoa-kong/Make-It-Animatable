"""POSIX worker lifecycle management, without importing compute dependencies."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from api_config import ROOT, Settings, WorkerSpec
from api_jobs import JobStore

logger = logging.getLogger("mia-v2-supervisor")


class SingletonBusyError(RuntimeError):
    """Another service owns this jobs directory; startup must abort."""


@dataclass
class _Worker:
    spec: WorkerSpec
    run_id: str
    process: subprocess.Popen
    control: socket.socket
    phase: str = "starting"
    error: str | None = None
    monitor: threading.Thread | None = None


class Supervisor:
    def __init__(self, settings: Settings, jobs: JobStore):
        self.settings = settings
        self.jobs = jobs
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopping = threading.Event()
        self._workers: list[_Worker] = []
        self._lock_file: BinaryIO | None = None
        self._started = False
        self._closed = False
        self._startup_error: str | None = None

    def start(self) -> None:
        """Acquire ownership and spawn once; asset/child errors remain queryable."""
        with self._lifecycle_lock:
            if self._started or self._closed or self._stopping.is_set():
                return
            self._acquire_singleton()
            self._started = True
            try:
                self.jobs.initialize_schema()
                self.jobs.recover_interrupted()
                if self._stopping.is_set():
                    return
                self._prepare_checkpoints()
                for spec in self.settings.worker_specs():
                    if self._stopping.is_set():
                        break
                    try:
                        self._spawn(spec)
                    except Exception as exc:
                        self._record_startup_error(f"cannot start {spec.worker_id}: {exc}")
            except Exception as exc:
                self._record_startup_error(str(exc))

    def snapshot(self) -> dict:
        with self._state_lock:
            stopping = self._stopping.is_set()
            ready = sum(worker.phase == "ready" and worker.process.poll() is None for worker in self._workers)
            initializing = any(worker.phase == "starting" and worker.process.poll() is None for worker in self._workers)
            errors = [worker.error for worker in self._workers if worker.error]
            if self._startup_error:
                errors.insert(0, self._startup_error)
            if stopping:
                status = "stopping"
            elif ready:
                status = "ready"
            elif initializing or not self._started:
                status = "starting"
            else:
                status = "error"
            return {"status": status, "error": "; ".join(errors) or None,
                    "max_concurrency": 0 if stopping else ready}

    def begin_stop(self) -> None:
        """Close admission and send nonblocking stop requests, even during start."""
        self._stopping.set()
        # Spawning appends before its final stopping check, so a child missed by
        # this snapshot receives its request from _spawn instead.
        for worker in tuple(self._workers):
            self._send_stop(worker)

    def close(self) -> None:
        self.begin_stop()
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            with self._state_lock:
                workers = tuple(self._workers)
            deadline = time.monotonic() + self.settings.shutdown_timeout_seconds
            escalation = min(2.0, self.settings.shutdown_timeout_seconds * 0.2)
            for worker in workers:
                self._send_stop(worker)
            self._wait_processes(workers, deadline - escalation)
            survivors = [worker for worker in workers if worker.process.poll() is None]
            for worker in survivors:
                self._signal_process(worker, kill=False)
            self._wait_processes(survivors, deadline - escalation / 2)
            for worker in survivors:
                if worker.process.poll() is None:
                    self._signal_process(worker, kill=True)
            # SIGKILL cannot be handled by a child. Reap every child before the
            # monitor fails its owned jobs and before releasing the singleton.
            for worker in workers:
                worker.process.wait()
            for worker in workers:
                if worker.monitor is not None:
                    worker.monitor.join()
                worker.control.close()
            if self._lock_file is not None:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
                self._lock_file = None

    @staticmethod
    def _wait_processes(workers: tuple[_Worker, ...] | list[_Worker], deadline: float) -> None:
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                worker.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                return

    @staticmethod
    def _signal_process(worker: _Worker, *, kill: bool) -> None:
        try:
            if kill:
                worker.process.kill()
            else:
                worker.process.terminate()
        except ProcessLookupError:
            pass

    def _acquire_singleton(self) -> None:
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        lock_file = (self.settings.jobs_dir / "supervisor.lock").open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise SingletonBusyError(f"another API service owns jobs directory {self.settings.jobs_dir}") from exc
        except BaseException:
            lock_file.close()
            raise
        self._lock_file = lock_file

    def _prepare_checkpoints(self) -> None:
        source = self.settings.checkpoint_dir.resolve()
        names = ("bw_joints.pth", "joints_coarse.pth", "pose.pth")
        missing = [name for name in names if not (source / name).is_file()]
        if missing:
            raise FileNotFoundError(f"missing MIA v2 checkpoints in {source}: {', '.join(missing)}")
        vae = self.settings.hy3dgen_models / "tencent" / "Hunyuan3D-2.1" / "hunyuan3d-vae-v2-1"
        missing = [name for name in ("config.yaml", "model.fp16.ckpt") if not (vae / name).is_file()]
        if missing:
            raise FileNotFoundError(f"missing Hunyuan3D VAE files in {vae}: {', '.join(missing)}")
        destination = ROOT / "output" / "best" / "v2"
        # The default source already is the destination. Never unlink it or
        # produce a self-referential symlink; never replace unrelated assets.
        if destination.resolve() == source:
            return
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(f"checkpoint destination {destination} does not match configured {source}; refusing to replace it")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source, target_is_directory=True)

    def _spawn(self, spec: WorkerSpec) -> None:
        parent_control, child_control = socket.socketpair()
        process = None
        worker = None
        try:
            parent_control.setblocking(False)
            run_id = str(uuid.uuid4())
            environment = os.environ.copy()
            environment.update({
                "MIA_WORKER_SETTINGS": self.settings.to_json(),
                "CUDA_VISIBLE_DEVICES": str(spec.physical_gpu), "GPU_ID": "0",
                "HF_HOME": str(self.settings.hf_home), "HY3DGEN_MODELS": str(self.settings.hy3dgen_models),
            })
            for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
                environment[variable] = str(self.settings.cpu_threads_per_worker)
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "api_worker.py"), "--worker-id", spec.worker_id,
                 "--worker-run-id", run_id, "--control-fd", str(child_control.fileno())],
                cwd=ROOT, env=environment, pass_fds=(child_control.fileno(),),
            )
            child_control.close()
            worker = _Worker(spec, run_id, process, parent_control)
            monitor = threading.Thread(target=self._monitor, args=(worker,),
                                       name=f"monitor-{spec.worker_id}", daemon=True)
            worker.monitor = monitor
            with self._state_lock:
                self._workers.append(worker)
            monitor.start()
            if self._stopping.is_set():
                self._send_stop(worker)
        except BaseException:
            child_control.close()
            if process is not None:
                process.kill()
                process.wait()
                self.jobs.fail_worker(run_id, error="worker launch failed", error_code=500)
            if worker is not None:
                with self._state_lock:
                    self._workers.remove(worker)
            parent_control.close()
            raise

    @staticmethod
    def _send_stop(worker: _Worker) -> None:
        try:
            # EOF is an explicit graceful stop in the worker protocol. A
            # half-close cannot leave a partially sent JSON command buffered.
            worker.control.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _record_startup_error(self, error: str) -> None:
        logger.error("Worker startup failed: %s", error)
        with self._state_lock:
            self._startup_error = f"{self._startup_error}; {error}" if self._startup_error else error

    def _worker_error(self, worker: _Worker, error: str) -> None:
        with self._state_lock:
            worker.phase = "error"
            worker.error = error
        logger.error("Worker %s: %s", worker.spec.worker_id, error)

    def _monitor(self, worker: _Worker) -> None:
        buffer = b""
        readable = True
        try:
            while True:
                exited = worker.process.poll() is not None
                if not readable:
                    if exited:
                        break
                    time.sleep(0.05)
                    continue
                try:
                    ready, _, _ = select.select([worker.control], [], [], 0 if exited else 0.05)
                    if not ready:
                        if exited:
                            break
                        continue
                    chunk = worker.control.recv(4096)
                    if not chunk:
                        readable = False
                        if exited:
                            break
                        if not self._stopping.is_set():
                            self._worker_error(worker, worker.error or "worker control channel closed")
                        continue
                    buffer += chunk
                    if len(buffer) > 65536:
                        raise ValueError("worker control message exceeds 64 KiB")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError("worker control message must be an object")
                        if message.get("status") == "ready":
                            with self._state_lock:
                                if worker.phase == "starting" and worker.process.poll() is None:
                                    worker.phase = "ready"
                        elif message.get("status") == "error":
                            self._worker_error(worker, str(message.get("error", "worker initialization failed")))
                        else:
                            raise ValueError("unknown worker control status")
                except (OSError, ValueError) as exc:
                    readable = False
                    self._worker_error(worker, f"worker control failure: {exc}")
                    self._send_stop(worker)
        finally:
            # Claim ownership is invalidated only after the OS confirms exit.
            returncode = worker.process.wait()
            with self._state_lock:
                worker.phase = "exited"
                if not self._stopping.is_set() and worker.error is None:
                    worker.error = f"worker exited with code {returncode}"
                error = worker.error or "worker stopped before completing its running job"
            try:
                self.jobs.fail_worker(worker.run_id, error=error, error_code=500)
            except Exception:
                logger.exception("Could not fail jobs owned by exited worker %s", worker.spec.worker_id)
            worker.control.close()

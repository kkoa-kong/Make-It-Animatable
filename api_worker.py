from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from api_config import Settings
    from api_schema import GenerateRequest


logger = logging.getLogger("mia-v2-worker")


def unload_blender_scripts() -> None:
    import addon_utils
    import bpy

    # Unregister Python-backed UI classes before CPython finalization; bpy 4.3
    # otherwise frees them too late during native static destruction.
    addon_utils.disable_all()
    modules = [sys.modules[name] for name in reversed(bpy.utils._registered_module_names)]
    bpy.utils._registered_module_names.clear()
    for module in modules:
        unregister = getattr(module, "unregister", None)
        if unregister is not None:
            unregister()


def _configure_environment(settings: Settings, worker_id: str) -> None:
    spec = next((spec for spec in settings.worker_specs() if spec.worker_id == worker_id), None)
    if spec is None:
        raise ValueError(f"worker ID is not present in the configured GPU groups: {worker_id}")
    os.environ["CUDA_VISIBLE_DEVICES"] = str(spec.physical_gpu)
    os.environ["GPU_ID"] = "0"
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(settings.cpu_threads_per_worker)
    os.environ["HF_HOME"] = str(settings.hf_home)
    os.environ["HY3DGEN_MODELS"] = str(settings.hy3dgen_models)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _read_control(control: socket.socket, stopping: threading.Event) -> None:
    try:
        with control.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                if json.loads(line).get("command") == "stop":
                    stopping.set()
                    return
    except (OSError, ValueError, AttributeError):
        logger.exception("Worker control channel failed")
    finally:
        # Losing the supervisor is a stop request, not permission to keep claiming.
        stopping.set()


def _send_status(control: socket.socket, status: str, *, error: str | None = None) -> None:
    message = {"status": status}
    if error is not None:
        message["error"] = error
    control.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))


class WorkerRuntime:
    def __init__(self, settings: Settings, worker_id: str, worker_run_id: str, stopping: threading.Event):
        from api_animations import AnimationCatalog
        from api_config import ROOT
        from api_jobs import JobStore
        from api_rig_cache import RigCache

        self.settings = settings
        self.root = ROOT
        self.worker_id = worker_id
        self.worker_run_id = worker_run_id
        self.stopping = stopping
        self.jobs = JobStore(settings.jobs_dir)
        self.catalog = AnimationCatalog(settings.animations_dir, translation_path=ROOT / "web" / "motion-index.zh-CN.json")
        self.catalog_error: str | None = None
        self.rig_cache = RigCache(settings.rig_cache_dir)
        self.engine_signature: str | None = None
        self.app_v2 = None
        self.scene_handler = None

    def _verify_checkpoints(self) -> None:
        names = ("bw_joints.pth", "joints_coarse.pth", "pose.pth")
        missing = [name for name in names if not (self.settings.checkpoint_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"missing MIA v2 checkpoints in {self.settings.checkpoint_dir}: {', '.join(missing)}"
            )
        destination = self.root / "output" / "best" / "v2"
        for name in names:
            if not (destination / name).is_file():
                raise FileNotFoundError(f"supervisor has not prepared the MIA checkpoint path: {destination / name}")
            if not (destination / name).samefile(self.settings.checkpoint_dir / name):
                raise RuntimeError(f"prepared MIA checkpoint does not match the configured file: {destination / name}")
        model_dir = self.settings.hy3dgen_models / "tencent" / "Hunyuan3D-2.1" / "hunyuan3d-vae-v2-1"
        missing = [name for name in ("config.yaml", "model.fp16.ckpt") if not (model_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f"missing Hunyuan3D VAE files in {model_dir}: {', '.join(missing)}")

    def _configure_blender_threads(self) -> None:
        import bpy
        from bpy.app.handlers import persistent

        @persistent
        def configure_scenes(*_args):
            for scene in bpy.data.scenes:
                scene.render.threads_mode = "FIXED"
                scene.render.threads = self.settings.cpu_threads_per_worker

        # Both paths replace scenes: factory reset on a miss, open_mainfile on
        # a cache hit. Persistent handlers survive file loads and scene resets.
        self.scene_handler = configure_scenes
        bpy.app.handlers.load_post.append(configure_scenes)
        bpy.app.handlers.load_factory_startup_post.append(configure_scenes)
        configure_scenes()

    def initialize(self) -> None:
        self._verify_checkpoints()
        import torch

        torch.set_num_threads(self.settings.cpu_threads_per_worker)
        torch.set_num_interop_threads(self.settings.cpu_threads_per_worker)
        if not torch.cuda.is_available():
            raise RuntimeError("MIA GPU worker requires CUDA; CPU inference is not supported")
        torch.cuda.set_device(0)
        # Force CUDA initialization here, before the worker can advertise ready.
        torch.cuda.get_device_properties(0)
        os.chdir(self.root)
        self._configure_blender_threads()
        import app_v2
        import app_blender  # noqa: F401 (load the direct exporter before advertising ready)
        from api_rig_cache import fingerprint_engine
        from util.blender_utils import bpy

        self.app_v2 = app_v2
        # Upstream stages return Gradio component updates. Inert keys preserve
        # those stages without constructing or serving any Gradio interface.
        app_v2.state = object()
        for name in (
            "output_joints_coarse", "output_normed_input", "output_sample",
            "output_joints", "output_bw", "output_rest_lbs", "output_rest_vis",
            "output_anim", "output_anim_vis",
        ):
            setattr(app_v2, name, object())
        app_v2.change_Model3D = lambda value, **_kwargs: value
        app_v2.init_models()
        self.engine_signature = fingerprint_engine(
            self.root,
            self.root / "output" / "best" / "v2",
            self.settings.hy3dgen_models / "tencent" / "Hunyuan3D-2.1" / "hunyuan3d-vae-v2-1",
            Path(app_v2.TEMPLATE_PATH),
            bpy.app.version_string,
        )
        try:
            self.catalog.load()
        except (OSError, ValueError) as exc:
            self.catalog_error = str(exc)
            logger.error("Local animation library unavailable: %s", exc)

    def _require_catalog(self):
        from fastapi import HTTPException

        if self.catalog_error is not None:
            raise HTTPException(503, f"animation library unavailable: {self.catalog_error}")
        return self.catalog

    def _animation_snapshot(self, request: GenerateRequest) -> dict | None:
        from fastapi import HTTPException

        if request.animation_id is None:
            return None
        catalog = self._require_catalog()
        record = catalog.get(str(request.animation_id))
        if record is None:
            raise HTTPException(404, "animation not found")
        try:
            catalog.path(str(request.animation_id))
        except FileNotFoundError as exc:
            raise HTTPException(410, "animation file is missing") from exc
        return {**record, "dataset": catalog.dataset, "revision": catalog.revision}

    def _prepare_inputs(
        self, request: GenerateRequest, work_dir: Path, *, uploaded: bool, uploaded_animation: bool
    ) -> tuple[Path, Path | None]:
        from api_mesh_input import prepare_mesh
        from api_schema import ANIMATION_EXTENSIONS, MESH_EXTENSIONS
        from api_url_fetch import download_url

        if uploaded:
            input_path = work_dir / "character.glb"
            if not input_path.is_file():
                raise ValueError("persisted GLB upload is missing")
        else:
            if request.input_url is None:
                raise ValueError("mesh URL or persisted GLB upload is required")
            extension = request.input_format or Path(urlsplit(request.input_url).path).suffix.lower()
            input_path = download_url(
                request.input_url, work_dir, max_bytes=self.settings.max_input_bytes,
                timeout_seconds=self.settings.download_timeout,
                allowed_extensions=MESH_EXTENSIONS, fallback_extension=extension,
            )
            input_path = input_path.rename(work_dir / f"character{extension}")
        animation_path = None
        if uploaded_animation:
            animation_path = work_dir / "animation.fbx"
            if not animation_path.is_file():
                raise ValueError("persisted FBX animation upload is missing")
        elif request.animation_id is not None:
            animation_path = self._require_catalog().path(str(request.animation_id))
        elif request.input_animation_url:
            animation_path = download_url(
                request.input_animation_url, work_dir, max_bytes=self.settings.max_input_bytes,
                timeout_seconds=self.settings.download_timeout, allowed_extensions=ANIMATION_EXTENSIONS,
            )
            animation_path = animation_path.rename(work_dir / "animation.fbx")
        return prepare_mesh(input_path), animation_path

    def _run_pipeline(
        self, request: GenerateRequest, input_path: Path, animation_path: Path | None, animation: dict | None
    ) -> Path:
        from api_schema import MODEL_NAME

        if self.engine_signature is None:
            raise RuntimeError("MIA v2 rig-cache engine signature is not initialized")
        with self.rig_cache.lease(input_path, request.model_dump(mode="json"), self.engine_signature) as rig:
            db = self.app_v2.DB()
            try:
                # The whole generator runs on the process main thread, selecting
                # app_v2's direct app_blender.main path rather than its task queue.
                for _ in self.app_v2._pipeline(
                    input_path=str(input_path), is_gs=False,
                    no_fingers=request.input_no_fingers, rest_pose_type=request.input_rest_pose,
                    ignore_pose_parts=list(request.input_rest_parts), input_normal=request.input_normal,
                    bw_fix=request.input_bw_fix, restore_global=request.input_restore_global,
                    reset_to_rest=request.input_reset_to_rest,
                    animation_file=None if animation_path is None else str(animation_path),
                    retarget=request.input_retarget, inplace=request.input_inplace, db=db,
                    export_temp=True, export_visuals=False, output_format="glb",
                    rig_cache_path=str(rig.path), rig_cache_hit=rig.hit,
                ):
                    pass
                if not db.anim_path or not Path(db.anim_path).is_file():
                    raise RuntimeError("MIA v2 pipeline did not produce an animatable GLB")
                directory = input_path.parent
                result_path = directory / "animatable.glb"
                shutil.move(db.anim_path, result_path)
                included = ["animatable.glb"]
                if self.settings.ui:
                    included.append("animatable_preview.glb")
                included.append("manifest.json")
                (directory / "manifest.json").write_text(
                    json.dumps({
                        "model": MODEL_NAME,
                        "options": request.model_dump(mode="json", exclude={"input_url", "input_animation_url"}),
                        "animation": animation,
                        "artifacts": included,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return result_path
            finally:
                output_dir = db.output_dir
                self.app_v2.clear(db)
                if output_dir:
                    shutil.rmtree(output_dir, ignore_errors=True)

    def _process_job(self, job: dict) -> None:
        from api_schema import GenerateRequest
        from api_url_fetch import DownloadError, DownloadUnavailable

        job_id = job["job_id"]
        work_dir = self.jobs.directory(job_id)
        status = "succeeded"
        error = None
        error_code = None
        try:
            payload = dict(job["request"])
            uploaded = payload.pop("_uploaded_input", False)
            uploaded_animation = payload.pop("_uploaded_animation", False)
            animation = payload.pop("_animation", None)
            # Existing queued requests can still contain this removed visual option.
            payload.pop("input_bw_vis_bone", None)
            request = GenerateRequest.model_validate(payload)
            if request.animation_id is not None and animation != self._animation_snapshot(request):
                raise ValueError("animation library metadata changed since task submission")
            input_path, animation_path = self._prepare_inputs(
                request, work_dir, uploaded=uploaded, uploaded_animation=uploaded_animation
            )
            self._run_pipeline(request, input_path, animation_path, animation)
        except Exception as exc:
            logger.exception("MIA v2 job %s failed", job_id)
            status, error = "failed", str(exc)
            if isinstance(exc, DownloadUnavailable):
                error_code = 502
            elif isinstance(exc, DownloadError):
                error_code = 400
            elif isinstance(exc, (ValueError, self.app_v2.gr.Error)):
                error_code = 422
            else:
                error_code = 500
        # Finishing is outside the task exception handler. A failed CAS or store
        # failure is fatal; only the supervisor may reconcile an exited owner.
        if not self.jobs.finish(job_id, self.worker_run_id, status, error=error, error_code=error_code):
            raise RuntimeError(f"worker lost ownership while finishing job {job_id}")
        for path in work_dir.iterdir():
            if status == "succeeded" and path.name in {"animatable.glb", "manifest.json"}:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    def run(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("MIA GPU worker computation must run on the main thread")
        while not self.stopping.is_set():
            job = self.jobs.claim_next(self.worker_id, self.worker_run_id)
            if job is None:
                self.stopping.wait(self.settings.queue_poll_interval_ms / 1000)
                continue
            # A stop racing the claim must not strand an owned task. Finish this
            # one during graceful shutdown, then never claim another task.
            stopping_after_claim = self.stopping.is_set()
            self._process_job(job)
            if stopping_after_claim or self.stopping.is_set():
                return

    def close(self) -> None:
        self.rig_cache.close()
        if "bpy" in sys.modules:
            import bpy

            if self.scene_handler is not None:
                for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.load_factory_startup_post):
                    if self.scene_handler in handlers:
                        handlers.remove(self.scene_handler)
            try:
                unload_blender_scripts()
            finally:
                if self.app_v2 is not None:
                    self.app_v2.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone MIA GPU worker (started by the API supervisor)")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--worker-run-id", required=True)
    parser.add_argument("--control-fd", type=int, required=True)
    cli = parser.parse_args()
    control = socket.socket(fileno=cli.control_fd)
    stopping = threading.Event()

    def stop(_signum=None, _frame=None):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    reader = threading.Thread(target=_read_control, args=(control, stopping), name="mia-worker-control", daemon=True)
    reader.start()
    runtime = None
    try:
        from api_config import Settings

        settings = Settings.from_json(os.environ["MIA_WORKER_SETTINGS"])
        _configure_environment(settings, cli.worker_id)
        runtime = WorkerRuntime(settings, cli.worker_id, cli.worker_run_id, stopping)
        try:
            if stopping.is_set():
                return 0
            runtime.initialize()
            if not stopping.is_set():
                _send_status(control, "ready")
                runtime.run()
        finally:
            runtime.close()
        return 0
    except BaseException as exc:
        logger.exception("MIA GPU worker %s failed", cli.worker_id)
        try:
            _send_status(control, "error", error=str(exc))
        except OSError:
            logger.exception("Unable to notify supervisor of worker failure")
        return 1
    finally:
        stopping.set()
        try:
            control.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        control.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    raise SystemExit(main())

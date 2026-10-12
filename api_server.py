from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import signal
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from api_jobs import JobStore
from api_mesh_input import prepare_mesh

from api_url_fetch import DownloadError, DownloadUnavailable, download_url


ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
import app_v2  # noqa: E402  (the upstream module owns the inference implementation)


logger = logging.getLogger("mia-v2-api")
MODEL_NAME = "mia-v2"
API_PORT = int(os.getenv("PORT", "8081"))
GPU_ID = int(os.getenv("GPU_ID", "0"))
MAX_INPUT_BYTES = int(os.getenv("MIA_MAX_INPUT_BYTES", str(100 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = float(os.getenv("MIA_DOWNLOAD_TIMEOUT", "30"))
CHECKPOINT_DIR = Path(os.getenv("MIA_CHECKPOINT_DIR", str(ROOT / "output" / "best" / "v2")))
HF_HOME = os.getenv("HF_HOME", str(ROOT / ".cache" / "huggingface"))
HY3DGEN_MODELS = Path(os.getenv("HY3DGEN_MODELS", str(Path.home() / ".cache" / "hy3dgen")))
JOBS_DIR = Path(os.getenv("MIA_JOBS_DIR", str(ROOT / ".cache" / "api-jobs")))

MESH_EXTENSIONS = frozenset({".glb", ".gltf", ".obj", ".stl", ".ply", ".fbx"})
ANIMATION_EXTENSIONS = frozenset({".fbx"})
REST_POSES = Literal["T-pose", "A-pose", "大-pose", "No"]
REST_PARTS = Literal["Fingers", "Arms", "Legs", "Head"]


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_url: str = Field(min_length=1, description="Public HTTP(S) URL of a mesh file")
    input_format: str | None = Field(default=None, description="Required only when the URL path has no file suffix")
    input_no_fingers: bool = False
    input_rest_pose: REST_POSES = "No"
    input_rest_parts: list[REST_PARTS] = Field(default_factory=list)
    input_normal: Literal[True] = True
    input_bw_fix: bool = True
    input_bw_vis_bone: str = "LeftArm"
    input_restore_global: bool = False
    input_reset_to_rest: bool = True
    input_animation_url: str | None = Field(default=None, description="Optional public HTTP(S) Mixamo FBX URL")
    input_retarget: bool = True
    input_inplace: bool = True

    @field_validator("input_bw_vis_bone")
    @classmethod
    def valid_visualization_bone(cls, value: str) -> str:
        valid = {name.lstrip(app_v2.MIXAMO_PREFIX) for name in app_v2.BONES_IDX_DICT}
        if value not in valid:
            raise ValueError("input_bw_vis_bone must name a MIA/Mixamo skeleton bone")
        return value

    @field_validator("input_format")
    @classmethod
    def valid_input_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        extension = value.lower()
        if not extension.startswith("."):
            extension = "." + extension
        if extension not in MESH_EXTENSIONS:
            raise ValueError(f"input_format must be one of {sorted(MESH_EXTENSIONS)}")
        return extension

    @model_validator(mode="after")
    def validate_input_url_suffix(self) -> "GenerateRequest":
        suffix = Path(urlsplit(self.input_url).path).suffix.lower()
        if not suffix and self.input_format is None:
            raise ValueError("input_format is required when input_url has no file suffix")
        if suffix and suffix not in MESH_EXTENSIONS:
            raise ValueError(f"input_url must point to a supported mesh file: {sorted(MESH_EXTENSIONS)}")
        if self.input_format is not None and suffix and self.input_format != suffix:
            raise ValueError("input_format must match the input_url file suffix")
        return self


_status_lock = threading.Lock()
_status = "starting"
_startup_error: str | None = None
_running_jobs = 0
_jobs = JobStore(JOBS_DIR)
_job_queue: queue.Queue[str | None] = queue.Queue()
_stopping = threading.Event()

# The upstream computational stages return Gradio component updates keyed by
# component instances. Give them inert keys; this API never builds or serves a UI.
_STATE_KEY = object()
app_v2.state = _STATE_KEY
for _name in (
    "output_joints_coarse",
    "output_normed_input",
    "output_sample",
    "output_joints",
    "output_bw",
    "output_rest_lbs",
    "output_rest_vis",
    "output_anim",
    "output_anim_vis",
):
    setattr(app_v2, _name, object())
app_v2.change_Model3D = lambda value, **_kwargs: value


def _configure_checkpoints() -> None:
    names = ("bw_joints.pth", "joints_coarse.pth", "pose.pth")
    missing = [name for name in names if not (CHECKPOINT_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"missing MIA v2 checkpoints in {CHECKPOINT_DIR}: {', '.join(missing)}")
    destination = ROOT / "output" / "best" / "v2"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        present = [name for name in names if (destination / name).is_file()]
        if len(present) == len(names):
            return
        if destination.is_dir() and not any(destination.iterdir()):
            destination.rmdir()
        else:
            raise RuntimeError(f"refusing to replace existing checkpoint directory: {destination}")
    destination.symlink_to(CHECKPOINT_DIR, target_is_directory=True)

def _configure_hunyuan_checkpoint() -> None:
    model_dir = HY3DGEN_MODELS / "tencent" / "Hunyuan3D-2.1" / "hunyuan3d-vae-v2-1"
    missing = [name for name in ("config.yaml", "model.fp16.ckpt") if not (model_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"missing Hunyuan3D VAE files in {model_dir}: {', '.join(missing)}"
        )
    os.environ["HY3DGEN_MODELS"] = str(HY3DGEN_MODELS)



def _load_models() -> None:
    global _status, _startup_error
    try:
        os.environ.setdefault("HF_HOME", HF_HOME)
        _configure_checkpoints()
        _configure_hunyuan_checkpoint()
        app_v2.init_models()
    except Exception as exc:
        logger.exception("MIA v2 model initialization failed")
        with _status_lock:
            _status = "error"
            _startup_error = str(exc)
        return
    with _status_lock:
        _status = "ready"
        _startup_error = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _job_queue
    _job_queue = queue.Queue()
    _jobs.initialize()
    _stopping.clear()
    await asyncio.to_thread(_load_models)
    worker = threading.Thread(target=_process_jobs, name="mia-v2-jobs")
    if _status == "ready":
        worker.start()
        for job_id in _jobs.pending():
            _job_queue.put(job_id)
    try:
        yield
    finally:
        _stopping.set()
        _job_queue.put(None)
        if worker.is_alive():
            await asyncio.to_thread(worker.join)
        if _status == "ready":
            app_v2.clear()


app = FastAPI(title="MIA v2 Worker", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    with _status_lock:
        status, error = _status, _startup_error
    if status != "ready":
        raise HTTPException(status_code=503, detail=error or "worker is not ready")
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/info")
def info() -> dict[str, object]:
    with _status_lock:
        status, error = _status, _startup_error
    result: dict[str, object] = {
        "worker_id": os.getenv("WORKER_ID", "mia-v2-0"),
        "model": MODEL_NAME,
        "status": status,
        "gpu": GPU_ID,
        "running_jobs": _running_jobs,
        "max_concurrency": 1,
        "input_formats": sorted(MESH_EXTENSIONS),
        "output_format": "application/zip",
        "submission": "asynchronous",
        "job_status_url": "/jobs/{job_id}",
        "job_result_url": "/jobs/{job_id}/result",
        "parameters": [
            "input_no_fingers",
            "input_rest_pose",
            "input_rest_parts",
            "input_normal",
            "input_bw_fix",
            "input_bw_vis_bone",
            "input_restore_global",
            "input_reset_to_rest",
            "input_animation_url",
            "input_retarget",
            "input_inplace",
        ],
    }
    if error:
        result["error"] = error
    return result


def _download_inputs(request: GenerateRequest, work_dir: Path) -> tuple[Path, Path | None]:
    extension = request.input_format or Path(urlsplit(request.input_url).path).suffix.lower()
    input_path = download_url(
        request.input_url,
        work_dir,
        max_bytes=MAX_INPUT_BYTES,
        timeout_seconds=DOWNLOAD_TIMEOUT,
        allowed_extensions=MESH_EXTENSIONS,
        fallback_extension=extension,
    )
    input_path = input_path.rename(work_dir / f"character{extension}")

    animation_path: Path | None = None
    if request.input_animation_url:
        animation_path = download_url(
            request.input_animation_url,
            work_dir,
            max_bytes=MAX_INPUT_BYTES,
            timeout_seconds=DOWNLOAD_TIMEOUT,
            allowed_extensions=ANIMATION_EXTENSIONS,
        )
        animation_path = animation_path.rename(work_dir / "animation.fbx")
    return prepare_mesh(input_path), animation_path


def _run_pipeline(request: GenerateRequest, input_path: Path, animation_path: Path | None) -> Path:
    db = app_v2.DB()
    try:
        for _ in app_v2._pipeline(
            input_path=str(input_path),
            is_gs=False,
            no_fingers=request.input_no_fingers,
            rest_pose_type=request.input_rest_pose,
            ignore_pose_parts=list(request.input_rest_parts),
            input_normal=request.input_normal,
            bw_fix=request.input_bw_fix,
            bw_vis_bone=request.input_bw_vis_bone,
            restore_global=request.input_restore_global,
            reset_to_rest=request.input_reset_to_rest,
            animation_file=None if animation_path is None else str(animation_path),
            retarget=request.input_retarget,
            inplace=request.input_inplace,
            db=db,
            export_temp=True,
        ):
            pass
        if not db.anim_path or not Path(db.anim_path).is_file():
            raise RuntimeError("MIA v2 pipeline did not produce an animatable FBX")

        artifacts = (
            ("joints_coarse.glb", db.joints_coarse_path),
            ("normalized_mesh", db.normed_path),
            ("sample.glb", db.sample_path),
            ("weights.glb", db.bw_path),
            ("joints.glb", db.joints_path),
            ("rest_lbs", db.rest_lbs_path),
            ("rest.glb", db.rest_vis_path),
            ("animatable.fbx", db.anim_path),
            ("animatable_preview.glb", db.anim_vis_path),
        )
        archive_path = input_path.parent / "mia-v2-result.zip"
        included: list[str] = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for archive_name, source in artifacts:
                if source and Path(source).is_file():
                    archive.write(source, arcname=archive_name)
                    included.append(archive_name)
            if not included:
                raise RuntimeError("MIA v2 pipeline produced no output artifacts")
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "model": MODEL_NAME,
                        "options": request.model_dump(exclude={"input_url", "input_animation_url"}),
                        "artifacts": included,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return archive_path
    finally:
        output_dir = db.output_dir
        app_v2.clear(db)
        if output_dir:
            shutil.rmtree(output_dir, ignore_errors=True)


def _public_job(job: dict) -> dict:
    result = {key: value for key, value in job.items() if key != "request"}
    result["status_url"] = f"/jobs/{job['job_id']}"
    result["result_url"] = (
        f"/jobs/{job['job_id']}/result" if job["status"] == "succeeded" else None
    )
    return result


def _process_jobs() -> None:
    global _running_jobs
    while not _stopping.is_set():
        job_id = _job_queue.get()
        if job_id is None or _stopping.is_set():
            return
        job = _jobs.get(job_id)
        if job is None:
            continue
        work_dir = _jobs.directory(job_id)
        with _status_lock:
            _running_jobs = 1
        try:
            _jobs.update(job_id, "running")
            request = GenerateRequest.model_validate(job["request"])
            input_path, animation_path = _download_inputs(request, work_dir)
            _run_pipeline(request, input_path, animation_path)
            _jobs.update(job_id, "succeeded")
        except Exception as exc:
            logger.exception("MIA v2 job %s failed", job_id)
            if isinstance(exc, DownloadUnavailable):
                code = 502
            elif isinstance(exc, DownloadError):
                code = 400
            elif isinstance(exc, (ValueError, app_v2.gr.Error)):
                code = 422
            else:
                code = 500
            _jobs.update(job_id, "failed", error=str(exc), error_code=code)
        finally:
            # Keep only successful result archives; requests and states live in SQLite.
            job = _jobs.get(job_id)
            for path in work_dir.iterdir():
                if job and job["status"] == "succeeded" and path.name == "mia-v2-result.zip":
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            with _status_lock:
                _running_jobs = 0


@app.post("/generate", status_code=202)
def generate(request: GenerateRequest) -> dict:
    with _status_lock:
        status, error = _status, _startup_error
    if status != "ready" or _stopping.is_set():
        raise HTTPException(status_code=503, detail=error or "worker is not ready")
    job = _jobs.create(request.model_dump())
    _job_queue.put(job["job_id"])
    return _public_job(job)


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _public_job(job)


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str) -> FileResponse:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "succeeded":
        raise HTTPException(status_code=409, detail="job has no successful result")
    archive_path = _jobs.directory(job_id) / "mia-v2-result.zip"
    if not archive_path.is_file():
        raise HTTPException(status_code=410, detail="job result is no longer available")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename="mia-v2-result.zip",
    )


def _serve_http() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level=os.getenv("LOG_LEVEL", "info"))
    server = uvicorn.Server(config)
    _server_holder["server"] = server
    server.run()


_server_holder: dict[str, uvicorn.Server] = {}


def main() -> None:
    server_thread = threading.Thread(target=_serve_http, name="mia-v2-http", daemon=True)
    server_thread.start()
    logger.info("MIA v2 API starting on 0.0.0.0:%d", API_PORT)

    def stop_server(_signum=None, _frame=None) -> None:
        server = _server_holder.get("server")
        if server is not None:
            server.should_exit = True

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    # bpy operators in the upstream implementation run on the main thread.
    import app_blender

    try:
        while server_thread.is_alive():
            try:
                task = app_v2.blender_task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                continue
            args, event, result_box = task
            try:
                app_blender.main(args)
            except Exception as exc:
                logger.exception("Blender export failed")
                result_box.append(exc)
            finally:
                event.set()
    finally:
        stop_server()
        server_thread.join()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    main()

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import shutil
import signal
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from fastapi.exceptions import RequestValidationError
from api_animations import AnimationCatalog
from api_artifacts import describe_result, has_preview
from api_input import input_payload, save_fbx, save_glb
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
ANIMATIONS_DIR = Path(os.getenv("MIA_ANIMATIONS_DIR", str(ROOT / "data" / "mixamo")))
_ui_installed = False

MESH_EXTENSIONS = frozenset({".glb", ".gltf", ".obj", ".stl", ".ply", ".fbx"})
ANIMATION_EXTENSIONS = frozenset({".fbx"})
REST_POSES = Literal["T-pose", "A-pose", "大-pose", "No"]
REST_PARTS = Literal["Fingers", "Arms", "Legs", "Head"]


class GenerateOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_format: str | None = Field(default=None, description="Required only when the URL path has no file suffix")
    input_no_fingers: bool = False
    input_rest_pose: REST_POSES = "No"
    input_rest_parts: list[REST_PARTS] = Field(default_factory=list)
    input_normal: Literal[True] = True
    input_bw_fix: bool = True
    input_restore_global: bool = False
    input_reset_to_rest: bool = True
    input_animation_url: str | None = Field(default=None, min_length=1, description="Optional public HTTP(S) Mixamo FBX URL")
    animation_id: UUID | None = Field(default=None, description="Unique Motion ID from the local animation library")
    input_retarget: bool = True
    input_inplace: bool = True


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
    def validate_animation_selection(self) -> "GenerateOptions":
        if self.animation_id is not None and self.input_animation_url is not None:
            raise ValueError("animation_id and input_animation_url are mutually exclusive")
        return self


class GenerateRequest(GenerateOptions):
    input_url: str | None = Field(default=None, min_length=1, description="Public HTTP(S) mesh URL; absent for uploaded GLB")


    @model_validator(mode="after")
    def validate_input_url_suffix(self) -> "GenerateRequest":
        if self.input_url is None:
            return self
        suffix = Path(urlsplit(self.input_url).path).suffix.lower()
        if not suffix and self.input_format is None:
            raise ValueError("input_format is required when input_url has no file suffix")
        if suffix and suffix not in MESH_EXTENSIONS:
            raise ValueError(f"input_url must point to a supported mesh file: {sorted(MESH_EXTENSIONS)}")
        if self.input_format is not None and suffix and self.input_format != suffix:
            raise ValueError("input_format must match the input_url file suffix")
        return self


class URLGenerateRequest(GenerateRequest):
    input_url: str = Field(min_length=1, description="Public HTTP(S) URL of a mesh file")


_status_lock = threading.Lock()
_status = "starting"
_startup_error: str | None = None
_running_jobs = 0
_jobs = JobStore(JOBS_DIR)
_job_queue: queue.Queue[str | None] = queue.Queue()
_stopping = threading.Event()
_catalog = AnimationCatalog(ANIMATIONS_DIR, translation_path=ROOT / "web" / "motion-index.zh-CN.json")
_catalog_error: str | None = None

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
    global _catalog_error
    try:
        await asyncio.to_thread(_catalog.load)
        _catalog_error = None
    except (OSError, ValueError) as exc:
        _catalog_error = str(exc)
        logger.error("Local animation library unavailable: %s", exc)
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
        "upload_formats": [".glb"],
        "animation_upload_formats": [".fbx"],
        "animation_library": {
            "status": "ready" if _catalog_error is None else "unavailable",
            "count": len(_catalog) if _catalog_error is None else 0,
        },
        "output_format": "model/gltf-binary",
        "submission": "asynchronous",
        "ui_enabled": _ui_installed,
        "job_status_url": "/jobs/{job_id}",
        "job_result_url": "/jobs/{job_id}/result",
        "job_manifest_url": "/jobs/{job_id}/manifest",
        "job_preview_url": "/jobs/{job_id}/preview" if _ui_installed else None,
        "parameters": [
            "input_no_fingers",
            "input_rest_pose",
            "input_rest_parts",
            "input_normal",
            "input_bw_fix",
            "input_restore_global",
            "input_reset_to_rest",
            "input_animation_url",
            "animation_id",
            "input_retarget",
            "input_inplace",
        ],
    }
    if error:
        result["error"] = error
    return result


def _require_catalog() -> AnimationCatalog:
    if _catalog_error is not None:
        raise HTTPException(status_code=503, detail=f"animation library unavailable: {_catalog_error}")
    return _catalog


def _with_display(record: dict) -> dict:
    catalog = _require_catalog()
    category = catalog.category(record["motion_id"])
    return {
        **catalog.display(record),
        "category_id": category["id"],
        "category_label": category["label"],
    }


@app.get("/animations/categories")
def animation_categories() -> dict:
    return _require_catalog().categories()


@app.get("/animations")
def animations(
    q: str = "",
    name: str | None = None,
    category: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        result = _require_catalog().search(
            q=q, name=name, limit=limit, offset=offset,
            category=None if category in (None, "", "all") else category,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["items"] = [_with_display(record) for record in result["items"]]
    return result


@app.get("/animations/{motion_id}")
def animation_info(motion_id: str) -> dict:
    catalog = _require_catalog()
    record = catalog.get(motion_id)
    if record is None:
        raise HTTPException(status_code=404, detail="animation not found")
    return {**_with_display(record), "dataset": catalog.dataset, "revision": catalog.revision}


@app.get("/animations/{motion_id}/file")
def animation_file(motion_id: str) -> FileResponse:
    catalog = _require_catalog()
    try:
        path = catalog.path(motion_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="animation not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="animation file is missing") from exc
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@app.get("/animations/{motion_id}/preview")
def animation_preview(motion_id: str) -> FileResponse:
    catalog = _require_catalog()
    try:
        path = catalog.preview_path(motion_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="animation not found") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="animation preview is missing") from exc
    return FileResponse(
        path, media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


def _animation_snapshot(request: GenerateRequest) -> dict | None:
    if request.animation_id is None:
        return None
    catalog = _require_catalog()
    record = catalog.get(str(request.animation_id))
    if record is None:
        raise HTTPException(status_code=404, detail="animation not found")
    try:
        catalog.path(str(request.animation_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="animation file is missing") from exc
    return {**record, "dataset": catalog.dataset, "revision": catalog.revision}


def _prepare_inputs(
    request: GenerateRequest, work_dir: Path, *, uploaded: bool = False, uploaded_animation: bool = False
) -> tuple[Path, Path | None]:
    if uploaded:
        input_path = work_dir / "character.glb"
        if not input_path.is_file():
            raise ValueError("persisted GLB upload is missing")
    else:
        if request.input_url is None:
            raise ValueError("mesh URL or persisted GLB upload is required")
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
    if uploaded_animation:
        animation_path = work_dir / "animation.fbx"
        if not animation_path.is_file():
            raise ValueError("persisted FBX animation upload is missing")
    elif request.animation_id is not None:
        animation_path = _require_catalog().path(str(request.animation_id))
    elif request.input_animation_url:
        animation_path = download_url(
            request.input_animation_url,
            work_dir,
            max_bytes=MAX_INPUT_BYTES,
            timeout_seconds=DOWNLOAD_TIMEOUT,
            allowed_extensions=ANIMATION_EXTENSIONS,
        )
        animation_path = animation_path.rename(work_dir / "animation.fbx")
    return prepare_mesh(input_path), animation_path


def _run_pipeline(
    request: GenerateRequest,
    input_path: Path,
    animation_path: Path | None,
    animation: dict | None = None,
) -> Path:
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
            restore_global=request.input_restore_global,
            reset_to_rest=request.input_reset_to_rest,
            animation_file=None if animation_path is None else str(animation_path),
            retarget=request.input_retarget,
            inplace=request.input_inplace,
            db=db,
            export_temp=True,
            export_visuals=False,
            output_format="glb",
        ):
            pass
        if not db.anim_path or not Path(db.anim_path).is_file():
            raise RuntimeError("MIA v2 pipeline did not produce an animatable GLB")

        directory = input_path.parent
        result_path = directory / "animatable.glb"
        shutil.move(db.anim_path, result_path)
        included = ["animatable.glb"]
        if _ui_installed:
            included.append("animatable_preview.glb")
        included.append("manifest.json")
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "model": MODEL_NAME,
                    "options": request.model_dump(mode="json", exclude={"input_url", "input_animation_url"}),
                    "animation": animation,
                    "artifacts": included,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return result_path
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
    result["manifest_url"] = (
        f"/jobs/{job['job_id']}/manifest" if job["status"] == "succeeded" else None
    )
    result["preview_url"] = (
        f"/jobs/{job['job_id']}/preview"
        if job["status"] == "succeeded" and _ui_installed and has_preview(_jobs.directory(job["job_id"]))
        else None
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
            payload = dict(job["request"])
            uploaded = payload.pop("_uploaded_input", False)
            uploaded_animation = payload.pop("_uploaded_animation", False)
            animation = payload.pop("_animation", None)
            # Migrate queued requests saved before visual-only API options were removed.
            payload.pop("input_bw_vis_bone", None)
            request = GenerateRequest.model_validate(payload)
            if request.animation_id is not None and animation != _animation_snapshot(request):
                raise ValueError("animation library metadata changed since task submission")
            input_path, animation_path = _prepare_inputs(
                request, work_dir, uploaded=uploaded, uploaded_animation=uploaded_animation
            )
            _run_pipeline(request, input_path, animation_path, animation)
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
            # Keep final successful files; requests and states live in SQLite.
            job = _jobs.get(job_id)
            for path in work_dir.iterdir():
                if job and job["status"] == "succeeded" and path.name in {"animatable.glb", "manifest.json"}:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            with _status_lock:
                _running_jobs = 0


@app.post(
    "/generate",
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": URLGenerateRequest.model_json_schema()},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "input_file": {"type": "string", "format": "binary", "description": "GLB v2; mutually exclusive with options.input_url"},
                            "animation_file": {"type": "string", "format": "binary", "description": "Binary animation FBX; mutually exclusive with options.input_animation_url and options.animation_id"},
                            "options": {
                                "type": "string",
                                "description": "JSON generation options, including input_url, input_animation_url or animation_id as applicable",
                            },
                        },
                    }
                },
            },
        }
    },
)
async def generate(http_request: Request) -> dict:
    with _status_lock:
        status, error = _status, _startup_error
    if status != "ready" or _stopping.is_set():
        raise HTTPException(status_code=503, detail=error or "worker is not ready")
    async with input_payload(http_request, MAX_INPUT_BYTES) as (options, input_upload, animation_upload):
        try:
            request = GenerateRequest.model_validate(options)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        if (request.input_url is not None) == (input_upload is not None):
            raise HTTPException(status_code=422, detail="provide exactly one of input_url and input_file")
        if animation_upload is not None and (
            request.input_animation_url is not None or request.animation_id is not None
        ):
            raise HTTPException(status_code=422, detail="animation_file, input_animation_url and animation_id are mutually exclusive")
        if input_upload is not None:
            if request.input_format not in (None, ".glb"):
                raise HTTPException(status_code=422, detail="uploaded input_format must be glb")
            request.input_format = ".glb"
        animation = _animation_snapshot(request)
        payload = request.model_dump(mode="json")
        payload["_animation"] = animation
        input_staging = None
        animation_staging = None
        try:
            if input_upload is not None:
                input_staging = await asyncio.to_thread(save_glb, input_upload, MAX_INPUT_BYTES, JOBS_DIR)
                payload["_uploaded_input"] = True
            if animation_upload is not None:
                animation_staging = await asyncio.to_thread(save_fbx, animation_upload, MAX_INPUT_BYTES, JOBS_DIR)
                payload["_uploaded_animation"] = True
            job = await asyncio.to_thread(
                _jobs.create, payload, input_file=input_staging, animation_file=animation_staging
            )
        finally:
            for path in (input_staging, animation_staging):
                if path is not None:
                    path.unlink(missing_ok=True)
        _job_queue.put(job["job_id"])
        return _public_job(job)


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _public_job(job)


def _job_result_directory(job_id: str) -> Path:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "succeeded":
        raise HTTPException(status_code=409, detail="job has no successful result")
    directory = _jobs.directory(job_id)
    if not (directory / "animatable.glb").is_file() or not (directory / "manifest.json").is_file():
        raise HTTPException(status_code=410, detail="job result is no longer available; regenerate legacy ZIP-only jobs")
    return directory


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str) -> FileResponse:
    return FileResponse(
        _job_result_directory(job_id) / "animatable.glb",
        media_type="model/gltf-binary",
        filename="animatable.glb",
    )


@app.get("/jobs/{job_id}/manifest")
def job_manifest(job_id: str) -> FileResponse:
    return FileResponse(
        _job_result_directory(job_id) / "manifest.json",
        media_type="application/json",
        filename="manifest.json",
        content_disposition_type="inline",
    )


@app.get("/jobs/{job_id}/preview")
def job_preview(job_id: str) -> FileResponse:
    directory = _job_result_directory(job_id)
    if not _ui_installed or not has_preview(directory):
        raise HTTPException(status_code=404, detail="job preview is not enabled")
    return FileResponse(
        directory / "animatable.glb",
        media_type="model/gltf-binary",
        filename="animatable_preview.glb",
        content_disposition_type="inline",
    )


@app.get("/jobs/{job_id}/artifacts")
def job_artifacts(job_id: str) -> dict:
    result = describe_result(_job_result_directory(job_id), preview_enabled=_ui_installed)
    for item in result["items"]:
        item["url"] = f"/jobs/{job_id}/{item.pop('endpoint')}"
    return {"job_id": job_id, **result}


def enable_ui() -> None:
    global _ui_installed
    if _ui_installed:
        return
    web_root = ROOT / "web"
    if not (web_root / "index.html").is_file():
        raise FileNotFoundError(f"UI assets are missing: {web_root}")

    @app.get("/", include_in_schema=False)
    def ui_index() -> FileResponse:
        return FileResponse(web_root / "index.html", media_type="text/html")

    @app.get("/ui/demo", include_in_schema=False)
    def ui_demo() -> FileResponse:
        path = ROOT / "data" / "examples" / "ninja.glb"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="example character is not installed")
        return FileResponse(path, media_type="model/gltf-binary", filename="ninja.glb")

    app.mount("/ui", StaticFiles(directory=web_root), name="ui")
    _ui_installed = True


def _serve_http() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level=os.getenv("LOG_LEVEL", "info"))
    server = uvicorn.Server(config)
    _server_holder["server"] = server
    server.should_exit = _stopping.is_set()
    server.run()


_server_holder: dict[str, uvicorn.Server] = {}


def _unload_blender_scripts() -> None:
    import addon_utils
    import bpy
    import sys

    # bpy 4.3 frees Python-backed UI classes during native static destruction,
    # after CPython has finalized. Unregister them while Python is still alive.
    addon_utils.disable_all()
    modules = [sys.modules[name] for name in reversed(bpy.utils._registered_module_names)]
    bpy.utils._registered_module_names.clear()
    for module in modules:
        unregister = getattr(module, "unregister", None)
        if unregister is not None:
            unregister()


def main() -> None:
    parser = argparse.ArgumentParser(description="MIA V2 API and optional 3D animation library UI")
    parser.add_argument("--ui", action="store_true", help="serve the HTML/Three.js interface at /")
    cli = parser.parse_args()
    if cli.ui:
        enable_ui()
    server_thread = threading.Thread(target=_serve_http, name="mia-v2-http", daemon=True)
    _stopping.clear()

    def stop_server(_signum=None, _frame=None) -> None:
        _stopping.set()
        server = _server_holder.get("server")
        if server is not None:
            server.should_exit = True

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    try:
        # Model initialization loads native dependencies that cannot safely
        # initialize in the HTTP event loop's executor thread.
        _load_models()
        if _stopping.is_set():
            return

        # bpy operators in the upstream implementation run on the main thread.
        import app_blender

        server_thread.start()
        logger.info("MIA v2 API starting on 0.0.0.0:%d", API_PORT)
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
        if server_thread.ident is not None:
            server_thread.join()
        _unload_blender_scripts()
        if _status == "ready":
            app_v2.clear()


if os.getenv("MIA_UI", "0") == "1":
    enable_ui()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    main()

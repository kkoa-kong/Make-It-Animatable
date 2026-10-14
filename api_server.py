from __future__ import annotations

import argparse
import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from fastapi.exceptions import RequestValidationError
from api_animations import AnimationCatalog
from api_artifacts import describe_result, has_preview
from api_config import ROOT, load_config
from api_input import input_payload, save_fbx, save_glb
from api_jobs import JobStore
from api_rig_cache import RigCache
from api_schema import MODEL_NAME, MESH_EXTENSIONS, GenerateRequest, URLGenerateRequest
from api_supervisor import Supervisor


logger = logging.getLogger("mia-v2-api")
_settings = load_config()
_ui_installed = False
_jobs = JobStore(_settings.jobs_dir)
_supervisor = Supervisor(_settings, _jobs)
_catalog = AnimationCatalog(_settings.animations_dir, translation_path=ROOT / "web" / "motion-index.zh-CN.json")
_catalog_error: str | None = None
_rig_cache = RigCache(_settings.rig_cache_dir)


async def _clean_cache() -> None:
    while True:
        _rig_cache.schedule_cleanup()
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _catalog_error, _rig_cache
    _rig_cache = RigCache(_settings.rig_cache_dir)
    cleaner = None
    try:
        await asyncio.to_thread(_supervisor.start)
        try:
            await asyncio.to_thread(_catalog.load)
            _catalog_error = None
        except (OSError, ValueError) as exc:
            _catalog_error = str(exc)
            logger.error("Local animation library unavailable: %s", exc)
        cleaner = asyncio.create_task(_clean_cache())
        yield
    finally:
        _supervisor.begin_stop()
        if cleaner is not None:
            cleaner.cancel()
            with suppress(asyncio.CancelledError):
                await cleaner
        try:
            await asyncio.to_thread(_supervisor.close)
        finally:
            await asyncio.to_thread(_rig_cache.close)


app = FastAPI(title="MIA v2 Worker", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    state = _supervisor.snapshot()
    if state["status"] != "ready":
        raise HTTPException(status_code=503, detail=state["error"] or "worker is not ready")
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/info")
def info() -> dict[str, object]:
    state = _supervisor.snapshot()
    result: dict[str, object] = {
        "worker_id": _settings.worker_id,
        "model": MODEL_NAME,
        "status": state["status"],
        "gpu": 0,
        "running_jobs": _jobs.running_count(),
        "max_concurrency": state["max_concurrency"],
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
    if state["error"]:
        result["error"] = state["error"]
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




def _public_job(job: dict) -> dict:
    result = {
        key: job[key]
        for key in ("job_id", "status", "created_at", "updated_at", "error", "error_code")
    }
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
    state = _supervisor.snapshot()
    if state["status"] != "ready":
        raise HTTPException(status_code=503, detail=state["error"] or "worker is not ready")
    async with input_payload(http_request, _settings.max_input_bytes) as (options, input_upload, animation_upload):
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
                input_staging = await asyncio.to_thread(save_glb, input_upload, _settings.max_input_bytes, _settings.jobs_dir)
                payload["_uploaded_input"] = True
            if animation_upload is not None:
                animation_staging = await asyncio.to_thread(save_fbx, animation_upload, _settings.max_input_bytes, _settings.jobs_dir)
                payload["_uploaded_animation"] = True
            if _supervisor.snapshot()["status"] != "ready":
                raise HTTPException(status_code=503, detail="worker is not ready")
            job = await asyncio.to_thread(
                _jobs.create, payload, input_file=input_staging, animation_file=animation_staging
            )
        finally:
            for path in (input_staging, animation_staging):
                if path is not None:
                    path.unlink(missing_ok=True)
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


class _Server(uvicorn.Server):
    def handle_exit(self, sig: int, frame) -> None:
        # Stop admission/claiming immediately, before HTTP graceful shutdown.
        _supervisor.begin_stop()
        super().handle_exit(sig, frame)


def main() -> None:
    global _settings, _jobs, _supervisor, _catalog
    parser = argparse.ArgumentParser(description="MIA V2 API with independent GPU workers")
    parser.add_argument("--config", help="TOML configuration (or MIA_CONFIG)")
    parser.add_argument("--ui", action="store_true", help="serve the HTML/Three.js interface at /")
    cli = parser.parse_args()
    _settings = load_config(cli.config)
    if cli.ui:
        _settings = replace(_settings, ui=True)
    _jobs = JobStore(_settings.jobs_dir)
    _supervisor = Supervisor(_settings, _jobs)
    _catalog = AnimationCatalog(
        _settings.animations_dir, translation_path=ROOT / "web" / "motion-index.zh-CN.json"
    )
    if _settings.ui:
        enable_ui()
    server = _Server(uvicorn.Config(
        app, host=_settings.host, port=_settings.port,
        log_level=os.getenv("LOG_LEVEL", "info"),
    ))
    server.run()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    main()
elif _settings.ui:
    enable_ui()

from __future__ import annotations

import json
import shutil
import struct
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

OPTIONS_BYTES = 64 * 1024


class BodyTooLarge(MultiPartException):
    pass


async def _limited_stream(request: Request, limit: int):
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise BodyTooLarge(f"request exceeds the {limit}-byte limit")
            yield chunk
    except ClientDisconnect as exc:
        # MultiPartParser closes its temporary files on MultiPartException.
        raise MultiPartException("client disconnected during upload") from exc


@asynccontextmanager
async def input_payload(request: Request, max_input_bytes: int):
    """Parse JSON options or independently uploaded GLB/FBX with JSON options."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "multipart/form-data"}:
        raise HTTPException(415, "use application/json or multipart/form-data")
    limit = OPTIONS_BYTES if content_type == "application/json" else 2 * max_input_bytes + OPTIONS_BYTES
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError as exc:
            raise HTTPException(400, "invalid Content-Length") from exc
        if size < 0:
            raise HTTPException(400, "invalid Content-Length")
        if size > limit:
            raise HTTPException(413, f"request exceeds the {limit}-byte limit")

    form = None
    try:
        if content_type == "application/json":
            chunks = [chunk async for chunk in _limited_stream(request, limit)]
            try:
                options = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError) as exc:
                raise HTTPException(422, "request body must be valid JSON") from exc
            if not isinstance(options, dict):
                raise HTTPException(422, "request body must be a JSON object")
            yield options, None, None
        else:
            form = await MultiPartParser(
                request.headers,
                _limited_stream(request, limit),
                max_files=2,
                max_fields=1,
                max_part_size=OPTIONS_BYTES,
            ).parse()
            fields = form.multi_items()
            if any(name not in {"input_file", "animation_file", "options"} for name, _ in fields):
                raise HTTPException(422, "multipart fields must be input_file, animation_file and optional options")
            if len({name for name, _ in fields}) != len(fields):
                raise HTTPException(422, "duplicate multipart fields are not allowed")
            input_upload = form.get("input_file")
            animation_upload = form.get("animation_file")
            for name, upload in (("input_file", input_upload), ("animation_file", animation_upload)):
                if upload is not None and not isinstance(upload, UploadFile):
                    raise HTTPException(422, f"{name} must be a file upload")
            raw_options = form.get("options", "{}")
            if not isinstance(raw_options, str):
                raise HTTPException(422, "options must be a JSON text field")
            try:
                options = json.loads(raw_options)
            except ValueError as exc:
                raise HTTPException(422, "options must be valid JSON") from exc
            if not isinstance(options, dict):
                raise HTTPException(422, "options must be a JSON object")
            yield options, input_upload, animation_upload
    except BodyTooLarge as exc:
        raise HTTPException(413, exc.message) from exc
    except MultiPartException as exc:
        raise HTTPException(400, exc.message) from exc
    finally:
        if form is not None:
            await form.close()


def save_glb(upload: UploadFile, max_input_bytes: int, directory: Path) -> Path:
    """Validate a GLB envelope and stage it on the persistent job filesystem."""
    if Path(upload.filename or "").suffix.lower() != ".glb":
        raise HTTPException(422, "file uploads support .glb only")
    if upload.size is None or upload.size > max_input_bytes:
        raise HTTPException(413, f"GLB exceeds the {max_input_bytes}-byte limit")
    upload.file.seek(0)
    header = upload.file.read(12)
    if len(header) != 12:
        raise HTTPException(422, "file is not a complete GLB")
    magic, version, length = struct.unpack("<4sII", header)
    if magic != b"glTF" or version != 2 or length != upload.size:
        raise HTTPException(422, "file must be a GLB v2 with a valid declared length")
    return _stage_upload(upload, directory, ".glb")


def save_fbx(upload: UploadFile, max_input_bytes: int, directory: Path) -> Path:
    """Stage a binary Mixamo animation FBX for the existing Blender importer."""
    if Path(upload.filename or "").suffix.lower() != ".fbx":
        raise HTTPException(422, "animation_file must be .fbx")
    if upload.size is None or upload.size > max_input_bytes:
        raise HTTPException(413, f"FBX exceeds the {max_input_bytes}-byte limit")
    upload.file.seek(0)
    header = upload.file.read(27)
    if len(header) < 27 or not header.startswith(b"Kaydara FBX Binary  \x00\x1a\x00"):
        raise HTTPException(422, "animation_file must be a binary FBX supported by Blender")
    return _stage_upload(upload, directory, ".fbx")


def _stage_upload(upload: UploadFile, directory: Path, suffix: str) -> Path:
    upload.file.seek(0)
    directory.mkdir(parents=True, exist_ok=True)
    path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".upload-", suffix=suffix, dir=directory, delete=False) as output:
            path = Path(output.name)
            shutil.copyfileobj(upload.file, output, length=1024 * 1024)
        return path
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise

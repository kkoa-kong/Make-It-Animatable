"""Expose only known generated outputs without extracting ZIP paths to disk."""
from pathlib import Path
import zipfile
import json

ARTIFACT_NAMES = frozenset({
    "joints_coarse.glb", "normalized_mesh", "sample.glb", "weights.glb", "joints.glb",
    "rest_lbs", "rest.glb", "animatable.fbx", "animatable_preview.glb", "manifest.json",
})


def _info(archive: zipfile.ZipFile, name: str) -> dict:
    if name not in ARTIFACT_NAMES:
        raise KeyError(name)
    entry = archive.getinfo(name)
    if entry.is_dir():
        raise KeyError(name)
    if name.endswith(".json"):
        media_type = "application/json"
    elif name.endswith(".glb"):
        media_type = "model/gltf-binary"
    else:
        with archive.open(entry) as stream:
            media_type = "model/gltf-binary" if stream.read(4) == b"glTF" else "application/octet-stream"
    return {"name": name, "size": entry.file_size, "media_type": media_type}


def describe_archive(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        return {
            "animation": manifest.get("animation"),
            "options": manifest.get("options", {}),
            "items": [_info(archive, name) for name in archive.namelist() if name in ARTIFACT_NAMES],
        }


def artifact_info(path: Path, name: str) -> dict:
    with zipfile.ZipFile(path) as archive:
        return _info(archive, name)


def stream_artifact(path: Path, name: str):
    if name not in ARTIFACT_NAMES:
        raise KeyError(name)
    with zipfile.ZipFile(path) as archive, archive.open(name) as stream:
        while chunk := stream.read(64 * 1024):
            yield chunk

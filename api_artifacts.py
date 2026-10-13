"""Describe final GLB output and its optional UI preview without duplicating it."""
import json
from pathlib import Path


def read_manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def has_preview(directory: Path) -> bool:
    try:
        manifest = read_manifest(directory)
    except FileNotFoundError:
        return False
    return "animatable_preview.glb" in manifest["artifacts"]


def describe_result(directory: Path, *, preview_enabled: bool) -> dict:
    manifest = read_manifest(directory)
    items = []
    for name in manifest["artifacts"]:
        if name == "animatable.glb":
            source, media_type, endpoint = directory / name, "model/gltf-binary", "result"
        elif name == "animatable_preview.glb" and preview_enabled:
            source, media_type, endpoint = directory / "animatable.glb", "model/gltf-binary", "preview"
        elif name == "manifest.json":
            source, media_type, endpoint = directory / name, "application/json", "manifest"
        else:
            continue
        if source.is_file():
            items.append({"name": name, "size": source.stat().st_size, "media_type": media_type, "endpoint": endpoint})
    return {"animation": manifest.get("animation"), "options": manifest["options"], "items": items}

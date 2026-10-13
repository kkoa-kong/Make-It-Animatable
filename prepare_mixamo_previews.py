"""Prepare one integrity-verified upstream MP4 per indexed Mixamo motion.

Run: .venv/bin/python prepare_mixamo_previews.py --destination data/mixamo
The pinned recursive inventory is inspected, then the matching domestic mirror
is preferred only after its render inventory matches upstream hashes and sizes.
Downloads reuse the resumable, bounded downloader from prepare_mixamo.py.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path, PurePosixPath
import re
import sys

from api_animations import AnimationCatalog, DATASET, REVISION
from prepare_mixamo import download, get_inventory, publish_json, safe_path

OFFICIAL = "https://huggingface.co"
MIRROR = "https://hf-mirror.com"


def inventory_signature(assets):
    signature = {}
    for asset in assets:
        if asset.get("type") != "file":
            continue
        path = safe_path(asset["path"])
        if path in signature:
            raise ValueError(f"Duplicate remote preview path: {path}")
        if type(asset.get("size")) is not int or asset["size"] <= 0:
            raise ValueError(f"Invalid remote size: {path}")
        lfs = asset.get("lfs") or {}
        if lfs:
            if lfs.get("size") != asset["size"] or not re.fullmatch(r"[0-9a-f]{64}", lfs.get("oid", "")):
                raise ValueError(f"Invalid remote LFS integrity metadata: {path}")
        elif not re.fullmatch(r"[0-9a-f]{40}", asset.get("oid", "")):
            raise ValueError(f"Invalid remote Git integrity metadata: {path}")
        signature[path] = (asset["size"], asset.get("oid"), lfs.get("oid"))
    return signature


def select_previews(records, files):
    """Map only exact source filename stems; never infer a similar motion."""
    by_directory = {}
    for asset in files:
        path = PurePosixPath(safe_path(asset["path"]))
        if path.suffix.lower() == ".mp4" and str(path).startswith("animation_motion_render/"):
            by_directory.setdefault(str(path.parent), []).append(asset)
    selected = []
    unmapped = []
    used = set()
    for record in records:
        directory = "animation_motion_render/" + str(PurePosixPath(record["filename"]).with_suffix(""))
        candidates = by_directory.get(directory, [])
        if not candidates:
            unmapped.append({"motion_id": record["motion_id"], "filename": record["filename"]})
            continue
        asset = min(candidates, key=lambda value: (PurePosixPath(value["path"]).name != "v000.mp4", value["path"]))
        used.add(directory)
        selected.append((record, asset))
    return selected, unmapped, sorted(set(by_directory) - used)


def prepare(args, report):
    root = args.destination.resolve()
    catalog = AnimationCatalog(root)
    catalog.load()
    records = catalog.search(limit=len(catalog))["items"]
    upstream = get_inventory(REVISION, "", report, recursive=True)
    all_files = [asset for asset in upstream if asset.get("type") == "file"]
    files = [asset for asset in all_files if asset["path"].startswith("animation_motion_render/")]
    upstream_signature = inventory_signature(files)
    report["upstream_inventory"] = {
        "entries": len(upstream), "files": len(all_files),
        "extensions": dict(sorted(Counter(PurePosixPath(a["path"]).suffix.lower() for a in all_files).items())),
        "gif_paths": sorted(a["path"] for a in all_files if a["path"].lower().endswith(".gif")),
        "render_files": len(files),
        "render_extensions": dict(sorted(Counter(PurePosixPath(a["path"]).suffix.lower() for a in files).items())),
    }
    endpoint = OFFICIAL
    mirror_report = {"inventory_pages": {}}
    try:
        mirrored = get_inventory(REVISION, "animation_motion_render", mirror_report, endpoint=MIRROR)
        if inventory_signature(mirrored) != upstream_signature:
            raise ValueError("Pinned mirror render inventory differs from official paths, sizes or hashes")
        endpoint = MIRROR
        report["mirror"] = {"status": "verified", "endpoint": MIRROR, **mirror_report}
    except Exception as exc:
        report["mirror"] = {"status": "unavailable_or_mismatched", "reason": str(exc), **mirror_report}
    report["download_endpoint"] = endpoint
    selected, unmapped, unreferenced = select_previews(records, files)
    report["unmapped_motions"] = unmapped
    report["unreferenced_render_directories"] = unreferenced
    report["counts"] = {"motions": len(records), "mapped": len(selected), "unmapped": len(unmapped),
                        "required_bytes": sum(asset["size"] for _, asset in selected)}
    report["selected_assets"] = [
        {"motion_id": record["motion_id"], "filename": record["filename"], "source_path": asset["path"],
         "relative_path": f"previews/{record['motion_id'].lower()}.mp4", "size": asset["size"],
         "git_blob_sha1": asset.get("oid"), "lfs_sha256": (asset.get("lfs") or {}).get("oid")}
        for record, asset in selected
    ]
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download, root, REVISION, asset, endpoint=endpoint,
                            relative_path=f"previews/{record['motion_id'].lower()}.mp4"): (record, asset)
            for record, asset in selected
        }
        for number, future in enumerate(as_completed(futures), 1):
            record, asset = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"path": f"previews/{record['motion_id'].lower()}.mp4", "error": str(exc)}
            result.update(motion_id=record["motion_id"], source_path=asset["path"])
            results.append(result)
            if number % 100 == 0 or number == len(selected):
                print(f"Verified {number}/{len(selected)} preview transfers", flush=True)
    report["failed_downloads"] = sorted((r for r in results if "error" in r), key=lambda r: r["path"])
    verified = sorted((r for r in results if "error" not in r), key=lambda r: r["path"])
    report["verified_assets"] = verified
    report["counts"].update(verified=len(verified), verified_bytes=sum(r["size"] for r in verified),
                            reused=sum(r["reused"] for r in verified))
    report["integrity_cases"] = [case for result in results for case in result.get("integrity_cases", [])]
    if report["failed_downloads"]:
        raise ValueError("Preview downloads failed transfer or upstream integrity verification")
    report["complete"] = True
    report["integrity_status"] = "passed"
    publish_json(root, "preview_validation_report.json", report)
    print(json.dumps({"destination": str(root), "integrity_status": "passed", "counts": report["counts"],
                      "download_endpoint": endpoint}, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parent / "data" / "mixamo")
    parser.add_argument("--workers", type=int, default=8, help="Bounded concurrent transfers (1..32)")
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32")
    report = {"schema_version": 1, "dataset": DATASET, "revision": REVISION,
              "complete": False, "integrity_status": "failed", "inventory_pages": {},
              "hash_verification": "Pinned upstream LFS SHA256 or Git blob SHA1; local SHA256 for every MP4"}
    try:
        prepare(args, report)
    except Exception as exc:
        report["failure"] = str(exc)
        root = args.destination.resolve()
        root.mkdir(parents=True, exist_ok=True)
        publish_json(root, "preview_validation_report.json", report)
        print(f"Preview preparation failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

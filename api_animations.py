from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path, PurePosixPath
from uuid import UUID
from api_categories import CATEGORY_LABELS, CATEGORY_RULES, CLASSIFICATION, classify


DATASET = "Linzhan/Mixamo-Animations-Characters"
REVISION = "7f134bc734557b1d9516cb476d25ea1f577e45c1"
_INDEX_FIELDS = {"schema_version", "dataset", "revision", "motions"}
_MOTION_FIELDS = {
    "motion_id", "filename", "prompt", "description", "frames", "relative_path", "sha256"
}
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def _motion_key(value: object) -> str:
    if not isinstance(value, str) or not _UUID_PATTERN.fullmatch(value):
        raise ValueError("motion_id must be a hyphenated UUID")
    return str(UUID(value))


def _relative_filename(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("filename must be a safe relative FBX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("filename must be a normalized relative FBX path")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("filename must not contain control characters")
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix.lower() != ".fbx":
        raise ValueError("filename must be a relative FBX path")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


class AnimationCatalog:
    """Validated, deterministic access to the pinned local Mixamo index.

    Only source metadata is exposed: no inferred tags, FPS, bone hierarchy, or
    translated descriptions. File existence and containment are checked at load
    and path resolution; the preparation tool owns FBX SHA-256 verification.
    """

    def __init__(self, root: Path):
        self._root = Path(root).resolve()
        self._records: dict[str, dict] = {}
        self._search_fields: dict[str, tuple[str, str, str, str]] = {}
        self._category_ids: dict[str, str] = {}
        self._loaded = False

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("Animation catalog is not loaded; call load() successfully first")

    @property
    def dataset(self) -> str:
        self._require_loaded()
        return DATASET

    @property
    def revision(self) -> str:
        self._require_loaded()
        return REVISION

    def __len__(self) -> int:
        self._require_loaded()
        return len(self._records)

    def _asset_path(self, relative_path: str) -> Path:
        path = (self._root / relative_path).resolve()
        # Do not resolve this boundary: animation_motion itself may be an
        # escaping symlink, which must not redefine the trusted subtree.
        boundary = self._root / "animation_motion"
        if not path.is_relative_to(boundary):
            raise ValueError(f"Animation asset escapes animation_motion: {relative_path}")
        if not path.is_file():
            raise FileNotFoundError(f"Animation asset is missing or not a file: {relative_path}")
        if path.stat().st_size == 0:
            raise ValueError(f"Animation asset is empty: {relative_path}")
        return path

    def load(self) -> None:
        """Load the complete index, failing explicitly on any invalid entry.

        Failed reloads invalidate the previous catalog instead of silently
        serving stale metadata. FBX files are not parsed or rehashed here.
        """
        self._loaded = False
        self._records = {}
        self._search_fields = {}
        self._category_ids = {}
        index_path = (self._root / "index.json").resolve()
        if not index_path.is_relative_to(self._root):
            raise ValueError("Animation index escapes the catalog root")
        with index_path.open(encoding="utf-8") as stream:
            index = json.load(stream, object_pairs_hook=_unique_object)
        if not isinstance(index, dict) or set(index) != _INDEX_FIELDS:
            raise ValueError("Animation index must contain exactly schema_version, dataset, revision, motions")
        if type(index["schema_version"]) is not int or index["schema_version"] != 1:
            raise ValueError("Unsupported animation index schema_version")
        if index["dataset"] != DATASET or index["revision"] != REVISION:
            raise ValueError("Animation index dataset or revision does not match the pinned Mixamo resource")
        if not isinstance(index["motions"], list) or not index["motions"]:
            raise ValueError("Animation index motions must be a non-empty list")

        records = {}
        filenames = set()
        for position, record in enumerate(index["motions"]):
            try:
                if not isinstance(record, dict) or set(record) != _MOTION_FIELDS:
                    raise ValueError("motion must contain exactly the source metadata and asset fields")
                key = _motion_key(record["motion_id"])
                if key in records:
                    raise ValueError(f"Duplicate motion_id: {record['motion_id']}")
                filename = _relative_filename(record["filename"])
                if filename in filenames:
                    raise ValueError(f"Duplicate filename: {filename}")
                if record["relative_path"] != f"animation_motion/{filename}":
                    raise ValueError("relative_path must equal animation_motion/<filename>")
                if not isinstance(record["prompt"], str) or not isinstance(record["description"], str):
                    raise ValueError("prompt and description must be original strings")
                if type(record["frames"]) is not int or record["frames"] < 0:
                    raise ValueError("frames must be a non-negative integer")
                if not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
                    raise ValueError("sha256 must be a lowercase SHA-256 digest")
                self._asset_path(record["relative_path"])
                records[key] = record.copy()
                filenames.add(filename)
            except ValueError as exc:
                raise ValueError(f"Invalid animation index motion {position}: {exc}") from exc

        records = dict(sorted(records.items(), key=lambda item: (item[1]["filename"], item[1]["motion_id"])))
        search_fields = {}
        for key, record in records.items():
            prompt = _normalize(record["prompt"])
            filename = _normalize(record["filename"])
            stem = _normalize(str(PurePosixPath(record["filename"]).with_suffix("")))
            description = _normalize(record["description"])
            search_fields[key] = (prompt, filename, stem, description)
        self._records = records
        self._search_fields = search_fields
        self._category_ids = {
            key: classify(" ".join(search_fields[key])) for key in records
        }
        self._loaded = True

    def get(self, motion_id: str) -> dict | None:
        self._require_loaded()
        try:
            key = _motion_key(motion_id)
        except ValueError:
            return None
        record = self._records.get(key)
        return record.copy() if record is not None else None

    def path(self, motion_id: str) -> Path:
        record = self.get(motion_id)
        if record is None:
            raise KeyError(motion_id)
        return self._asset_path(record["relative_path"])

    def search(
        self, q: str = "", name: str | None = None, limit: int = 20, offset: int = 0,
        category: str | None = None,
    ) -> dict:
        self._require_loaded()
        if not isinstance(q, str) or (name is not None and not isinstance(name, str)):
            raise ValueError("q and name must be strings")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if category is not None and category not in CATEGORY_LABELS:
            raise ValueError("unknown animation category")
        query = _normalize(q)
        tokens = query.split()
        exact_name = _normalize(name) if name is not None else None
        matches = []
        for key, record in self._records.items():
            if category is not None and self._category_ids[key] != category:
                continue
            prompt, filename, stem, description = self._search_fields[key]
            if exact_name is not None and prompt != exact_name:
                continue
            name_text = f"{prompt} {filename}"
            aggregate = f"{name_text} {description}"
            if not all(token in aggregate for token in tokens):
                continue
            if not tokens or query in (prompt, filename, stem):
                rank = 0
            elif all(token in name_text for token in tokens):
                rank = 1
            else:
                rank = 2
            matches.append((rank, record["filename"], record["motion_id"], record))
        matches.sort(key=lambda item: item[:3])
        return {
            "total": len(matches),
            "limit": limit,
            "offset": offset,
            "items": [item[3].copy() for item in matches[offset:offset + limit]],
        }

    def category(self, motion_id: str) -> dict | None:
        self._require_loaded()
        try:
            key = _motion_key(motion_id)
        except ValueError:
            return None
        category_id = self._category_ids.get(key)
        if category_id is None:
            return None
        return {"id": category_id, "label": CATEGORY_LABELS[category_id]}

    def categories(self) -> dict:
        self._require_loaded()
        counts = dict.fromkeys(CATEGORY_LABELS, 0)
        for category_id in self._category_ids.values():
            counts[category_id] += 1
        return {
            "total": len(self._records),
            "classification": CLASSIFICATION,
            "items": [
                {"id": category_id, "label": label, "count": counts[category_id]}
                for category_id, label, _ in CATEGORY_RULES
            ],
        }

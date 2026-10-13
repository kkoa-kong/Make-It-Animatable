from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping
from uuid import uuid4


logger = logging.getLogger("mia-v2-api.rig-cache")
TTL_SECONDS = 24 * 60 * 60
_SCHEMA = 1
_ENTRY_NAME = re.compile(r"[0-9a-f]{64}\Z")
_STAGE_NAME = re.compile(r"\.stage-[0-9a-f]{64}-[0-9a-f]{32}\Z")
_BINDING_OPTIONS = (
    "input_no_fingers",
    "input_normal",
    "input_bw_fix",
    "input_restore_global",
    "input_reset_to_rest",
    "input_rest_pose",
    "input_rest_parts",
)


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binding_options(options: Mapping) -> dict:
    result = {name: options[name] for name in _BINDING_OPTIONS}
    result["input_rest_parts"] = sorted(set(result["input_rest_parts"]))
    return result


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fingerprint_engine(
    root: Path,
    checkpoint_dir: Path,
    vae_dir: Path,
    template_path: Path,
    blender_version: str,
) -> str:
    """Hash the loaded engine's files once, not once per cache lookup.

    Call after model initialization, using the actual checkpoint locations. Paths
    are labels only: relocating identical weights does not invalidate the cache.
    """
    root, checkpoint_dir, vae_dir = Path(root), Path(checkpoint_dir), Path(vae_dir)
    files = {
        f"source/{name}": root / name
        for name in (
            "app_v2.py", "app_blender.py", "model.py", "models_ae.py",
            "api_rig_cache.py", "api_mesh_input.py", "util/utils.py",
            "util/misc.py", "util/blender_utils.py", "util/dataset_mixamo.py",
            "util/dataset_mixamo_additional.py",
        )
    }
    files.update({f"checkpoint/{name}": checkpoint_dir / name for name in ("bw_joints.pth", "joints_coarse.pth", "pose.pth")})
    files.update({f"vae/{name}": vae_dir / name for name in ("config.yaml", "model.fp16.ckpt")})
    files["template"] = Path(template_path)
    hunyuan_source = root / "util" / "Hunyuan3D_21"
    for path in hunyuan_source.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".json"}:
            files[f"hunyuan/{path.relative_to(hunyuan_source).as_posix()}"] = path
    signature = {
        "schema": _SCHEMA,
        "blender": blender_version,
        "files": {label: content_hash(path) for label, path in sorted(files.items())},
    }
    return hashlib.sha256(_canonical(signature)).hexdigest()


@dataclass(frozen=True)
class RigLease:
    key: str
    hit: bool
    path: Path


@dataclass
class _KeyState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class RigCache:
    """Single-process cache of clean rigs; no multi-worker filesystem sharing.

    Only hash entry names and .stage-<hash>-<uuid> directories are reserved by
    this manager. Leases serialize a key and shield both active rigs and stages
    from cleanup. Successful context exit publishes a miss atomically; failed
    rendering never publishes. Hits never update the publication timestamp.
    """

    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], float] = time.time,
        cleanup_interval: float = 300.0,
    ):
        self.directory = Path(directory)
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._guard = threading.Lock()
        self._keys: dict[str, _KeyState] = {}
        self._stages: set[str] = set()
        self._cleaner: threading.Thread | None = None
        self._last_cleanup: float | None = None
        self._closed = threading.Event()

    def _metadata(self, directory: Path, key: str) -> dict | None:
        if directory.is_symlink() or not directory.is_dir():
            return None
        try:
            metadata_path = directory / "metadata.json"
            blend_path = directory / "character.blend"
            if metadata_path.is_symlink() or blend_path.is_symlink():
                return None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            published = metadata.get("published_at")
            if (
                metadata.get("schema") != _SCHEMA
                or metadata.get("key") != key
                or isinstance(published, bool)
                or not isinstance(published, (int, float))
                or not math.isfinite(published)
                or not blend_path.is_file()
                or blend_path.stat().st_size == 0
            ):
                return None
            return metadata
        except FileNotFoundError:
            return None
        except OSError:
            logger.exception("Unable to read rig-cache metadata in %s", directory)
            return None
        except (ValueError, TypeError, AttributeError):
            return None

    def _owned_entry(self, directory: Path) -> bool:
        if directory.is_symlink() or not directory.is_dir():
            return False
        # Never sweep unrelated content, even if its directory resembles a key.
        return all(path.name in {"character.blend", "metadata.json"} for path in directory.iterdir())

    @contextmanager
    def lease(self, input_path: Path, options: Mapping, engine_signature: str) -> Iterator[RigLease]:
        input_digest = content_hash(input_path)
        identity = {
            "input_sha256": input_digest,
            "options": binding_options(options),
            "engine_signature": engine_signature,
            "schema": _SCHEMA,
        }
        key = hashlib.sha256(_canonical(identity)).hexdigest()
        with self._guard:
            state = self._keys.setdefault(key, _KeyState())
            state.users += 1
        stage: Path | None = None
        publication_blocked = False
        try:
            with state.lock:
                with self._guard:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    entry = self.directory / key
                    metadata = self._metadata(entry, key)
                    hit = metadata is not None and self._clock() < metadata["published_at"] + TTL_SECONDS
                    if not hit:
                        if entry.exists() or entry.is_symlink():
                            if not self._owned_entry(entry):
                                raise RuntimeError(f"refusing to replace unrelated rig-cache entry: {entry}")
                            try:
                                shutil.rmtree(entry)
                            except OSError:
                                logger.exception("Unable to remove stale rig-cache entry %s", entry)
                                publication_blocked = True
                        stage = self.directory / f".stage-{key}-{uuid4().hex}"
                        stage.mkdir()
                        self._stages.add(stage.name)
                lease = RigLease(key, hit, (entry if hit else stage) / "character.blend")
                try:
                    yield lease
                    if not hit and not publication_blocked:
                        if lease.path.is_symlink() or not lease.path.is_file() or lease.path.stat().st_size == 0:
                            raise RuntimeError("MIA v2 pipeline did not produce a clean rig cache")
                        if not self._owned_entry(stage):
                            raise RuntimeError("rig cache staging contains unexpected artifacts")
                        metadata = dict(identity, key=key, published_at=self._clock())
                        (stage / "metadata.json").write_bytes(_canonical(metadata))
                        with self._guard:
                            stage.replace(entry)
                            self._stages.discard(stage.name)
                        stage = None
                finally:
                    if stage is not None:
                        with self._guard:
                            try:
                                shutil.rmtree(stage)
                            except OSError:
                                logger.exception("Unable to remove failed rig-cache staging directory %s", stage)
                            finally:
                                self._stages.discard(stage.name)
        finally:
            with self._guard:
                state.users -= 1
                if state.users == 0:
                    del self._keys[key]

    def cleanup(self) -> None:
        """Remove only expired/incomplete cache-owned entries; leases win races."""
        try:
            directories = list(self.directory.iterdir())
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Unable to list rig cache %s", self.directory)
            return
        for directory in directories:
            if self._closed.is_set():
                break
            is_stage = _STAGE_NAME.fullmatch(directory.name) is not None
            is_entry = _ENTRY_NAME.fullmatch(directory.name) is not None
            if not (is_stage or is_entry):
                continue
            try:
                with self._guard:
                    if directory.is_symlink() or not directory.is_dir():
                        continue
                    if is_stage:
                        if directory.name in self._stages:
                            continue
                        if not self._owned_entry(directory):
                            continue
                    else:
                        if directory.name in self._keys:
                            continue
                        if not self._owned_entry(directory):
                            continue
                        metadata = self._metadata(directory, directory.name)
                        if metadata is not None and self._clock() < metadata["published_at"] + TTL_SECONDS:
                            continue
                    shutil.rmtree(directory)
            except OSError:
                logger.exception("Unable to remove expired/incomplete rig-cache directory %s", directory)

    def schedule_cleanup(self) -> bool:
        """Start at most one throttled daemon; never wait on request threads."""
        if not self._guard.acquire(blocking=False):
            return False
        try:
            if self._closed.is_set() or (self._cleaner is not None and self._cleaner.is_alive()):
                return False
            now = self._clock()
            if self._last_cleanup is not None and now - self._last_cleanup < self._cleanup_interval:
                return False
            self._last_cleanup = now
            self._cleaner = threading.Thread(target=self._background_cleanup, name="mia-v2-rig-cache", daemon=True)
            self._cleaner.start()
            return True
        except Exception:
            self._cleaner = None
            logger.exception("Unable to schedule rig-cache cleanup")
            return False
        finally:
            self._guard.release()

    def _background_cleanup(self) -> None:
        try:
            self.cleanup()
        except Exception:
            logger.exception("Rig-cache background cleanup failed")

    def close(self) -> None:
        self._closed.set()
        with self._guard:
            cleaner = self._cleaner
        if cleaner is not None and cleaner is not threading.current_thread():
            cleaner.join()

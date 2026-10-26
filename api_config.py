"""Compute-free configuration for the HTTP service and its GPU workers.

Priority is explicit environment overrides > TOML > defaults. MIA_CONFIG selects
TOML when no path is passed. TOML paths are relative to the configuration file;
environment paths are relative to the process working directory. Default paths
are anchored at ROOT. Worker IPC uses the flat JSON Settings representation.

MIA_GPU_GROUPS is a JSON array of {physical_gpu, count}; it overrides TOML
workers.gpu_groups. Explicit groups ignore inherited CUDA_VISIBLE_DEVICES and
GPU_ID. Without groups, a single numeric CUDA_VISIBLE_DEVICES selects the physical
GPU, otherwise GPU_ID (default 0) does. Child environments replace CUDA masking
and all BLAS/OpenMP thread limits rather than inheriting them.
"""
from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class GPUGroup:
    physical_gpu: int
    count: int

    def __post_init__(self) -> None:
        _integer("physical_gpu", self.physical_gpu, 0, 65535)
        _integer("count", self.count, 1, 64)


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    physical_gpu: int


def _integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")


def _number(name: str, value: object, minimum: float, maximum: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum < value <= maximum:
        raise ValueError(f"{name} must be finite and in ({minimum}, {maximum}]")


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8081
    jobs_dir: Path = ROOT / ".cache" / "api-jobs"
    rig_cache_dir: Path = ROOT / ".cache" / "rig-cache"
    animations_dir: Path = ROOT / "data" / "mixamo"
    checkpoint_dir: Path = ROOT / "output" / "best" / "v2"
    hf_home: Path = ROOT / ".cache" / "huggingface"
    hy3dgen_models: Path = Path.home() / ".cache" / "hy3dgen"
    cpu_threads_per_worker: int = 1
    queue_poll_interval_ms: int = 250
    shutdown_timeout_seconds: float = 300.0
    max_input_bytes: int = 200 * 1024 * 1024
    download_timeout: float = 30.0
    ui: bool = False
    worker_id: str = "mia-v2-0"
    gpu_groups: tuple[GPUGroup, ...] = (GPUGroup(0, 1),)

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("host must be a nonempty string")
        _integer("port", self.port, 1, 65535)
        _integer("cpu_threads_per_worker", self.cpu_threads_per_worker, 1, 256)
        _integer("queue_poll_interval_ms", self.queue_poll_interval_ms, 10, 60000)
        _integer("max_input_bytes", self.max_input_bytes, 1, 10 * 1024**3)
        _number("shutdown_timeout_seconds", self.shutdown_timeout_seconds, 0, 3600)
        _number("download_timeout", self.download_timeout, 0, 3600)
        if type(self.ui) is not bool:
            raise ValueError("ui must be a boolean")
        if not isinstance(self.worker_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.worker_id):
            raise ValueError("worker_id must be a 1-128 character service identifier")
        for name in _PATH_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if not isinstance(self.gpu_groups, tuple) or not self.gpu_groups or not all(isinstance(group, GPUGroup) for group in self.gpu_groups):
            raise ValueError("gpu_groups must be a nonempty tuple of GPUGroup values")
        if len({group.physical_gpu for group in self.gpu_groups}) != len(self.gpu_groups):
            raise ValueError("gpu_groups must not repeat physical GPUs")
        if sum(group.count for group in self.gpu_groups) > 256:
            raise ValueError("total worker count must not exceed 256")

    def worker_specs(self) -> tuple[WorkerSpec, ...]:
        return tuple(WorkerSpec(f"gpu{group.physical_gpu}-worker{index}", group.physical_gpu)
                     for group in self.gpu_groups for index in range(group.count))

    def to_json(self) -> str:
        data = asdict(self)
        for name in _PATH_FIELDS:
            data[name] = str(getattr(self, name))
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> Settings:
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError("worker settings must be a JSON object")
        _reject_unknown(data, {field.name for field in fields(cls)}, "settings")
        for name in _PATH_FIELDS:
            if name in data:
                data[name] = _path(data[name], Path.cwd(), name)
        if "gpu_groups" in data:
            data["gpu_groups"] = _groups(data["gpu_groups"])
        return cls(**data)


_PATH_FIELDS = ("jobs_dir", "rig_cache_dir", "animations_dir", "checkpoint_dir", "hf_home", "hy3dgen_models")
_SECTIONS = {
    "api": {"host", "port", "ui", "worker_id", "max_input_bytes", "download_timeout"},
    "storage": {"jobs_dir", "rig_cache_dir", "animations_dir"},
    "models": {"checkpoint_dir", "hf_home", "hy3dgen_models"},
    "workers": {"gpu_groups", "cpu_threads_per_worker", "queue_poll_interval_ms", "shutdown_timeout_seconds"},
}
_ENV = {
    "MIA_HOST": "host", "PORT": "port", "MIA_UI": "ui", "WORKER_ID": "worker_id",
    "MIA_JOBS_DIR": "jobs_dir", "MIA_RIG_CACHE_DIR": "rig_cache_dir", "MIA_ANIMATIONS_DIR": "animations_dir",
    "MIA_CHECKPOINT_DIR": "checkpoint_dir", "HF_HOME": "hf_home", "HY3DGEN_MODELS": "hy3dgen_models",
    "MIA_CPU_THREADS_PER_WORKER": "cpu_threads_per_worker", "MIA_QUEUE_POLL_INTERVAL_MS": "queue_poll_interval_ms",
    "MIA_SHUTDOWN_TIMEOUT_SECONDS": "shutdown_timeout_seconds", "MIA_MAX_INPUT_BYTES": "max_input_bytes",
    "MIA_DOWNLOAD_TIMEOUT": "download_timeout", "MIA_GPU_GROUPS": "gpu_groups",
}


def _reject_unknown(data: dict, allowed: set[str], context: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {context} keys: {', '.join(sorted(unknown))}")


def _path(value: object, base: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty path string")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _groups(value: object) -> tuple[GPUGroup, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("gpu_groups must be a nonempty array")
    result = []
    for group in value:
        if not isinstance(group, dict):
            raise ValueError("each GPU group must be an object")
        _reject_unknown(group, {"physical_gpu", "count"}, "GPU group")
        if set(group) != {"physical_gpu", "count"}:
            raise ValueError("each GPU group requires physical_gpu and count")
        result.append(GPUGroup(**group))
    return tuple(result)


def load_config(path: str | Path | None = None) -> Settings:
    """Read TOML and explicit overrides listed in _ENV; never load compute code."""
    if path is None:
        path = os.environ.get("MIA_CONFIG")
    data: dict = {}
    if path is not None:
        config_path = Path(path).expanduser().resolve()
        with config_path.open("rb") as source:
            config = tomllib.load(source)
        _reject_unknown(config, set(_SECTIONS), "configuration sections")
        for section, allowed in _SECTIONS.items():
            values = config.get(section, {})
            if not isinstance(values, dict):
                raise ValueError(f"{section} must be a TOML table")
            _reject_unknown(values, allowed, section)
            data.update(values)
        for name in _PATH_FIELDS:
            if name in data:
                data[name] = _path(data[name], config_path.parent, name)
        if "gpu_groups" in data:
            data["gpu_groups"] = _groups(data["gpu_groups"])
    integers = {"port", "cpu_threads_per_worker", "queue_poll_interval_ms", "max_input_bytes"}
    numbers = {"shutdown_timeout_seconds", "download_timeout"}
    for variable, name in _ENV.items():
        if variable not in os.environ:
            continue
        value = os.environ[variable]
        try:
            if name in _PATH_FIELDS:
                converted = _path(value, Path.cwd(), name)
            elif name in integers:
                converted = int(value)
            elif name in numbers:
                converted = float(value)
            elif name == "gpu_groups":
                converted = _groups(json.loads(value))
            elif name == "ui":
                if value.lower() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                    raise ValueError("expected true/false, yes/no, on/off, or 1/0")
                converted = value.lower() in {"1", "true", "yes", "on"}
            else:
                converted = value
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid {variable}: {exc}") from exc
        data[name] = converted
    if "gpu_groups" not in data:
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
        selected = visible if re.fullmatch(r"[0-9]+", visible) else os.environ.get("GPU_ID", "0")
        try:
            data["gpu_groups"] = (GPUGroup(int(selected), 1),)
        except ValueError as exc:
            raise ValueError(f"invalid default GPU selection: {selected!r}") from exc
    return Settings(**data)

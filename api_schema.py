from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL_NAME = "mia-v2"
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
            raise ValueError("input_format is required when the URL path has no file suffix")
        if suffix and suffix not in MESH_EXTENSIONS:
            raise ValueError("input_url must point to a supported mesh file")
        if self.input_format is not None and suffix and self.input_format != suffix:
            raise ValueError("input_format must match the input_url file suffix")
        return self


class URLGenerateRequest(GenerateRequest):
    input_url: str = Field(min_length=1, description="Public HTTP(S) URL of a mesh file")

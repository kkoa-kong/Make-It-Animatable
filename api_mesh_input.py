from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def prepare_mesh(input_path: Path) -> Path:
    """Convert an FBX character mesh to adjacent character.glb; pass others through."""
    if input_path.suffix.lower() != ".fbx":
        return input_path

    converter = Path(__file__).resolve().parent / "util" / "FBX2glTF"
    if not converter.is_file() or not os.access(converter, os.X_OK):
        raise ValueError(f"FBX mesh converter is missing or not executable: {converter}")
    if not input_path.is_file():
        raise ValueError(f"FBX character mesh does not exist: {input_path}")

    output_path = input_path.with_name("character.glb")
    try:
        # Stage the output with the converter's extracted FBX assets so a stale
        # adjacent GLB cannot be mistaken for the result of this conversion.
        with tempfile.TemporaryDirectory(prefix="mia-fbx-", dir=input_path.parent.resolve()) as working_dir:
            staged_output = Path(working_dir) / "character.glb"
            result = subprocess.run(
                [
                    str(converter),
                    "--binary",
                    "--keep-attribute",
                    "auto",
                    "--fbx-temp-dir",
                    working_dir,
                    "--input",
                    str(input_path.resolve()),
                    "--output",
                    str(staged_output),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
            diagnostics = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())[-4096:]
            if result.returncode != 0:
                raise ValueError(
                    f"FBX character mesh conversion failed (exit {result.returncode}): "
                    f"{diagnostics or 'converter produced no diagnostics'}"
                )
            if not staged_output.is_file() or staged_output.stat().st_size == 0:
                raise ValueError(
                    "FBX character mesh conversion produced no non-empty GLB: "
                    f"{diagnostics or 'converter produced no diagnostics'}"
                )
            staged_output.replace(output_path)
    except OSError as exc:
        raise ValueError(f"FBX character mesh conversion could not complete: {exc}") from exc

    return output_path

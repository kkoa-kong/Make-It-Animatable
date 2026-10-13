#!/usr/bin/env python3
"""Generate the Chinese display index from the pinned source and phrase glossary.

Run from any directory: python scripts/generate_motion_index_zh.py
Use --check to validate the committed output without writing it. Translation
changes belong in motion-glossary.zh-CN.json, not in the generated index.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DATASET = "Linzhan/Mixamo-Animations-Characters"
REVISION = "7f134bc734557b1d9516cb476d25ea1f577e45c1"
MOTION_COUNT = 2317


def generate(source_path: Path, glossary_path: Path) -> dict:
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    for label, resource in (("source", source), ("glossary", glossary)):
        if resource.get("schema_version") != 1:
            raise ValueError(f"Unsupported {label} schema")
        if resource.get("dataset") != DATASET or resource.get("revision") != REVISION:
            raise ValueError(f"The {label} does not match the pinned dataset/revision")
    if hashlib.sha256(source_bytes).hexdigest() != glossary.get("source_sha256"):
        raise ValueError("Source content changed; review all translations before regenerating")

    motions = source["motions"]
    if len(motions) != MOTION_COUNT:
        raise ValueError(f"Expected {MOTION_COUNT} source motions")
    ids = [motion["motion_id"] for motion in motions]
    if len(set(ids)) != MOTION_COUNT or any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("Source motion IDs must be unique nonempty strings")

    translations = glossary["translations"]
    phrases = {motion[field] for motion in motions for field in ("prompt", "description")}
    missing = phrases - translations.keys()
    extra = translations.keys() - phrases
    if missing or extra:
        raise ValueError(f"Glossary mismatch: missing={sorted(missing)!r}, extra={sorted(extra)!r}")
    for english, chinese in translations.items():
        if not isinstance(chinese, str) or not chinese.strip() or chinese != chinese.strip():
            raise ValueError(f"Invalid Chinese translation: {english!r}")
        if not re.search(r"[\u3400-\u9fff]", chinese):
            raise ValueError(f"Chinese text missing: {english!r}")
        # The upstream A/B labels and YMCA dance acronym are meaningful names.
        residue = re.sub(r"YMCA|(?<![A-Za-z])[AB](?![A-Za-z])", "", chinese)
        if re.search(r"[A-Za-z]", residue):
            raise ValueError(f"Untranslated English residue: {english!r}: {chinese!r}")

    return {
        "schema_version": 1,
        "dataset": DATASET,
        "revision": REVISION,
        "motions": [
            {
                "motion_id": motion["motion_id"],
                "prompt_zh": translations[motion["prompt"]],
                "description_zh": translations[motion["description"]],
            }
            for motion in motions
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/mixamo/index.json")
    parser.add_argument("--glossary", type=Path, default=ROOT / "scripts/motion-glossary.zh-CN.json")
    parser.add_argument("--output", type=Path, default=ROOT / "web/motion-index.zh-CN.json")
    parser.add_argument("--check", action="store_true", help="Validate the existing output without writing")
    args = parser.parse_args()
    generated = generate(args.source, args.glossary)
    text = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if args.output.read_text(encoding="utf-8") != text:
            raise ValueError("Chinese index is stale; regenerate it with this script")
        print(f"Validated {len(generated['motions'])} Chinese motions")
    else:
        args.output.write_text(text, encoding="utf-8")
        print(f"Generated {len(generated['motions'])} Chinese motions: {args.output}")


if __name__ == "__main__":
    main()

"""Build small licensed UI font subsets from Ubuntu's Noto Sans CJK package."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font-root", type=Path, default=Path("/usr/share/fonts/opentype/noto"))
    args = parser.parse_args()
    from fontTools.ttLib import TTFont

    corpus = "".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("web/index.html", "web/app.js", "web/styles.css", "api_categories.py")
    )
    codepoints = sorted(set(range(32, 256)) | {ord(char) for char in corpus if ord(char) >= 256})
    unicodes = ",".join(f"U+{codepoint:04X}" for codepoint in codepoints)
    destination = ROOT / "web" / "fonts"
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    copyright_text = []
    for weight in ("Regular", "Bold"):
        source = args.font_root / f"NotoSansCJK-{weight}.ttc"
        font = TTFont(source, fontNumber=2, lazy=True)
        if font["name"].getDebugName(1) != "Noto Sans CJK SC":
            raise ValueError("font collection entry 2 must be Noto Sans CJK SC")
        copyright_text.append(font["name"].getDebugName(0))
        font.close()
        output = destination / f"motion-sans-{weight.lower()}.woff2"
        subprocess.run([
            sys.executable, "-m", "fontTools.subset", str(source), "--font-number=2",
            f"--unicodes={unicodes}", "--flavor=woff2", "--no-hinting", "--desubroutinize",
            f"--output-file={output}",
        ], check=True)
        files.append({"file": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()})
    license_source = Path("/usr/share/doc/fonts-noto-cjk/copyright").read_text(encoding="utf-8")
    # The first SIL occurrence precedes Debian packaging metadata; use the full
    # standalone SIL license section rather than packaging license declarations.
    license_section = license_source.split("License: SIL-1.1\n", 2)[2].split("License: GPL-3+", 1)[0]
    license_section = "\n".join("" if line == " ." else line.removeprefix(" ") for line in license_section.splitlines())
    (destination / "LICENSE").write_text(
        "Noto Sans CJK SC UI subsets (regular and bold).\nSource: Ubuntu fonts-noto-cjk 1:20220127+repack1-1, upstream googlefonts/noto-cjk.\n\n"
        + "\n".join(dict.fromkeys(copyright_text)) + "\n\n" + license_section + "\n", encoding="utf-8",
    )
    (destination / "manifest.json").write_text(json.dumps({
        "family": "Noto Sans CJK SC", "subset": "UI literals plus Latin-1", "unicode_count": len(codepoints), "files": files,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"files": files, "unicode_count": len(codepoints)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

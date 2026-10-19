"""Produce a HACS/manual-install artifact without development or reference files."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
HA_ARCHIVE = "blink-live-homeassistant.zip"


def package_integration(root: Path = ROOT, output: Path | None = None) -> Path:
    output = output or root / "dist" / HA_ARCHIVE
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted((root / "custom_components" / "blink_live").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path, path.relative_to(root))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(package_integration(args.root, args.output))

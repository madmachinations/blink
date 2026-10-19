"""Build version-matched HA/CLI artifacts in a private staging directory, without an sdist."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from package_integration import HA_ARCHIVE, package_integration

ROOT = Path(__file__).resolve().parents[1]


def release_version(now: datetime | None = None) -> str:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("Release timestamps must include a timezone")
    instant = instant.astimezone(UTC)
    return f"{instant:%y%m}.{instant.timetuple().tm_yday}{instant:%H%M}"


def validate_version(version: str) -> str:
    if not re.fullmatch(r"[0-9]{4}\.[1-9][0-9]{4,6}", version):
        raise ValueError("Expected YYMM.day-of-year-HHMM numeric version")
    return version


def stage_project(root: Path, stage: Path, version: str) -> None:
    """Copy only distributable inputs; never copy reference repos, auth, logs or venvs."""
    version = validate_version(version)
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        shutil.copy2(root / name, stage / name)
    shutil.copytree(
        root / "custom_components" / "blink_live",
        stage / "custom_components" / "blink_live",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )
    project_path = stage / "pyproject.toml"
    project, count = re.subn(
        r'^version = "[^"]+"$',
        f'version = "{version}"',
        project_path.read_text(),
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("Could not stamp Python project version")
    project_path.write_text(project)
    manifest_path = stage / "custom_components" / "blink_live" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def build_artifacts(root: Path, output: Path, version: str) -> list[Path]:
    version = validate_version(version)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blink-live-build-") as temporary:
        stage = Path(temporary)
        stage_project(root, stage, version)
        wheel_dir = stage / "wheel-output"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheel_dir),
                str(stage),
            ],
            check=True,
        )
        wheels = list(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one CLI wheel")
        # Only remove obsolete generated distributions, never arbitrary dist files.
        for pattern in (
            "blink_live*.whl",
            "blink_live*.tar.gz",
            "blink_live.zip",
            "blink-live-cli.whl",
            "blink-live-cli.zip",
            HA_ARCHIVE,
        ):
            for previous in output.glob(pattern):
                if previous.is_file():
                    previous.unlink()
        wheel = output / wheels[0].name
        shutil.copy2(wheels[0], wheel)
        integration = package_integration(stage, output / HA_ARCHIVE)
    return [integration, wheel]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    version = args.version or release_version()
    artifacts = build_artifacts(ROOT, args.output, version)
    if github_output := os.environ.get("GITHUB_OUTPUT"):
        with open(github_output, "a") as output:
            output.write(f"version={version}\n")
    print(f"Version: {version}")
    for artifact in artifacts:
        print(artifact)


if __name__ == "__main__":
    main()

"""Publish tested artifacts to a new GitHub release without reusing existing version tags."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from build_artifacts import validate_version
from package_integration import HA_ARCHIVE


def publish_release(version: str, directory: Path, repository: str, commit: str) -> None:
    version = validate_version(version)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected owner/repository")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected the exact tested commit SHA")
    wheels = list(directory.glob("*.whl"))
    integration = directory / HA_ARCHIVE
    if len(wheels) != 1 or not integration.is_file():
        raise ValueError("Expected one Home Assistant ZIP and one CLI wheel")
    assets = [integration, wheels[0]]
    if any(path.is_symlink() for path in assets):
        raise ValueError("Release artifacts must not be symlinks")
    refs = subprocess.run(
        ["gh", "api", f"repos/{repository}/git/matching-refs/tags/{version}"],
        check=True,
        capture_output=True,
        text=True,
    )
    if any(ref["ref"] == f"refs/tags/{version}" for ref in json.loads(refs.stdout)):
        raise ValueError(
            "This minute's version tag already exists; refusing to overwrite or reuse it. "
            "Rerun the full workflow in a later minute."
        )
    # Upload to a draft first: a failed upload must not expose an incomplete release.
    subprocess.run(
        [
            "gh",
            "release",
            "create",
            version,
            f"{integration}#Home Assistant integration",
            f"{wheels[0]}#Standalone CLI (Python 3.13+)",
            "--repo",
            repository,
            "--target",
            commit,
            "--title",
            f"Blink Live {version}",
            "--generate-notes",
            "--draft",
        ],
        check=True,
    )
    subprocess.run(
        ["gh", "release", "edit", version, "--repo", repository, "--draft=false", "--latest"],
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    args = parser.parse_args()
    publish_release(
        args.version, args.directory, os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    )

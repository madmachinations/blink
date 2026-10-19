"""Atomic owner-only local state; file I/O is kept outside the event loop."""

import asyncio
import json
import os
import tempfile
from pathlib import Path


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("State file must not be a symlink")
    with path.open() as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("Invalid state file")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".blink-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(value, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def load_json(path: Path) -> dict:
    return await asyncio.to_thread(read_json, path)


async def save_json(path: Path, value: dict) -> None:
    await asyncio.to_thread(write_json, path, value)

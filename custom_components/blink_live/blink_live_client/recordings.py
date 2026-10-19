"""Persistent recording discovery, optional archives, and lifecycle notifications."""

import asyncio
import contextlib
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .client import BlinkClient
from .errors import BlinkError
from .models import Clip

SaveState = Callable[[dict], Awaitable[None]]
Notify = Callable[[str, dict], Awaitable[None]]


def fingerprint(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BlinkError("Archive entry is not a regular file")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def archive_file(record: dict) -> Path:
    """Only serve/prune the exact file that was indexed after download."""
    path = Path(record["path"])
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise BlinkError("Archive directory changed")
    if fingerprint(path) != record["fingerprint"]:
        raise BlinkError("Archive file has been replaced or modified")
    return path


class Recordings:
    def __init__(
        self,
        client: BlinkClient,
        state: dict,
        save: SaveState,
        notify: Notify,
        directory: Path | None = None,
        automatic: bool = False,
        retention_days: int = 30,
        recording_source: str = "usb",
    ) -> None:
        self.client = client
        self.state = state
        self.save = save
        self.notify = notify
        self.directory = directory
        self.automatic = automatic
        self.retention_days = retention_days
        if recording_source not in ("usb", "cloud", "all"):
            raise BlinkError("Unknown recording source")
        self.recording_source = recording_source
        self.state.setdefault("baselined", [])
        self.state.setdefault("seen", {})
        self.state.setdefault("pending", [])
        self.state.setdefault("archive", {})
        self._lock = asyncio.Lock()

    @property
    def clips(self) -> dict[str, Clip]:
        return {key: Clip(**value) for key, value in self.state["seen"].items()}

    async def refresh(self) -> None:
        async with self._lock:
            failures = []
            sources = []
            if self.recording_source in ("usb", "all"):
                sources.extend(
                    (module.id, module)
                    for module in self.client.modules.values()
                    if module.local_storage
                )
            if self.recording_source in ("cloud", "all"):
                sources.append(("cloud", None))
            for identity, module in sources:
                # Overlap catches delayed updates; stable identities deduplicate them.
                checkpoint = (datetime.now(UTC) - timedelta(days=1)).isoformat()
                try:
                    clips = (
                        await self.client.clips(module)
                        if module is not None
                        else await self.client.cloud_clips(self.state.get("cloud_since"))
                    )
                except BlinkError as error:
                    failures.append(error)
                    continue
                new = []
                baseline = identity not in self.state["baselined"]
                for clip in clips:
                    previous = self.state["seen"].get(clip.key, {})
                    clip = replace(
                        clip,
                        discovered_at=(
                            previous.get("discovered_at") or datetime.now(UTC).isoformat()
                        ),
                    )
                    if clip.key not in self.state["seen"] and not baseline:
                        new.append(clip)
                        if self.automatic:
                            self.state["pending"].append(clip.key)
                    self.state["seen"][clip.key] = clip.to_dict()
                if baseline:
                    self.state["baselined"].append(identity)
                if identity == "cloud":
                    self.state["cloud_since"] = checkpoint
                await self.save(self.state)
                for clip in new:
                    await self.notify("clip_available", clip.event_data())
            if self.automatic and self.directory:
                for key in tuple(self.state["pending"]):
                    if (
                        self.recording_source != "all"
                        and self.clips[key].source != self.recording_source
                    ):
                        continue
                    try:
                        await self._download(key, self.directory)
                    except (BlinkError, OSError) as error:
                        failures.append(error)
                await self._prune()
            if failures:
                # Successful modules remain committed; failed operations retry next poll.
                raise failures[0]

    async def download(self, key: str, directory: Path | None = None) -> Path:
        async with self._lock:
            target = directory or self.directory
            if target is None:
                raise BlinkError("Configure an archive directory or provide a destination")
            return await self._download(key, target)

    async def _download(self, key: str, directory: Path) -> Path:
        clip = self.clips.get(key)
        if clip is None:
            raise BlinkError("Unknown clip ID; refresh clips first")
        if record := self.state["archive"].get(key):
            try:
                existing = await asyncio.to_thread(archive_file, record)
            except (OSError, BlinkError):
                pass
            else:
                await self._clear_pending(key)
                return existing
        directory = await asyncio.to_thread(directory.resolve, True)
        filename = f"blink-live-{key}.mp4"
        path = directory / filename
        fd, temporary = await _temporary_file(directory)
        total = 0
        prefix = bytearray()
        try:
            async for chunk in self.client.clip_chunks(clip):
                total += len(chunk)
                if len(prefix) < 64:
                    prefix.extend(chunk[: 64 - len(prefix)])
                await _disk_call(_write_all, fd, chunk)
            if not total or b"ftyp" not in prefix:
                raise BlinkError("Downloaded recording is not an MP4 file")
            await _disk_call(os.fsync, fd)
            os.close(fd)
            fd = -1
            # Finish local commit + indexing even if a viewer/automation cancels at this point.
            task = asyncio.create_task(self._commit(temporary, path, clip))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            return path
        finally:
            if fd != -1:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(os.unlink, temporary)

    async def _commit(self, temporary: str, path: Path, clip: Clip) -> None:
        # Atomic link refuses to overwrite a replaced or unindexed user file.
        signature = await asyncio.to_thread(fingerprint, Path(temporary))
        await asyncio.to_thread(os.link, temporary, path)
        previous = self.state["archive"].get(clip.key)
        self.state["archive"][clip.key] = {
            "path": str(path),
            "saved_at": time.time(),
            "clip": clip.to_dict(),
            "fingerprint": signature,
        }
        try:
            await self._clear_pending(clip.key)
        except BaseException:
            if previous is None:
                self.state["archive"].pop(clip.key, None)
            else:
                self.state["archive"][clip.key] = previous
            # This new file was never successfully indexed or announced. Leave it retryable.
            if await asyncio.to_thread(fingerprint, path) == signature:
                await asyncio.to_thread(path.unlink)
            raise
        await self.notify("clip_saved", {**clip.event_data(), "path": str(path)})

    async def _clear_pending(self, key: str) -> None:
        previous = list(self.state["pending"])
        if key in self.state["pending"]:
            self.state["pending"].remove(key)
        try:
            await self.save(self.state)
        except BaseException:
            self.state["pending"] = previous
            raise

    async def prune(self) -> None:
        async with self._lock:
            await self._prune()

    async def _prune(self) -> None:
        if self.retention_days <= 0:
            return
        deadline = time.time() - self.retention_days * 86400
        for key, record in tuple(self.state["archive"].items()):
            if record["saved_at"] >= deadline:
                continue
            try:
                path = await asyncio.to_thread(archive_file, record)
                await asyncio.to_thread(path.unlink)
            except FileNotFoundError:
                pass
            except (OSError, BlinkError):
                continue  # Replaced files and symlinks are never deleted.
            del self.state["archive"][key]
        await self.save(self.state)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


async def _disk_call(function, *args):
    # Cancellation must not close an fd while a worker thread is still writing it.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _temporary_file(directory: Path):
    task = asyncio.create_task(
        asyncio.to_thread(
            tempfile.mkstemp,
            prefix=".blink-live-",
            dir=directory,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        fd, temporary = await task
        os.close(fd)
        await asyncio.to_thread(os.unlink, temporary)
        raise

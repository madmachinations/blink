import asyncio
import copy
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from blink_live_client import BlinkError
from blink_live_client.recordings import Recordings, archive_file


class FakeClient:
    def __init__(self, module, clips):
        self.modules = {module.id: module}
        self.listed = clips
        self.downloads = 0
        self.fail = False

    async def clips(self, module):
        return self.listed

    async def clip_chunks(self, clip):
        self.downloads += 1
        yield b"\x00\x00\x00\x18ftypisom" + b"\0" * 16
        if self.fail:
            raise BlinkError("interrupted")
        yield b"media"


async def test_baseline_late_arrival_changed_manifest_and_restart(module, clip, memory):
    client = FakeClient(module, [clip])
    recordings = Recordings(client, {}, memory.save, memory.notify)
    await recordings.refresh()
    assert memory.events == []
    # A delayed clip can have an earlier creation timestamp than the last refresh.
    late = replace(clip, id="7", created_at="2026-10-19T11:59:00+00:00")
    client.listed = [replace(clip, manifest_id="99"), late]
    restarted = Recordings(client, copy.deepcopy(memory.value), memory.save, memory.notify)
    await restarted.refresh()
    assert [kind for kind, _ in memory.events] == ["clip_available"]
    assert memory.events[0][1]["clip_id"] == late.key
    await restarted.refresh()
    assert len(memory.events) == 1


async def test_optional_archive_events_order_and_atomic_save(module, clip, memory, tmp_path):
    client = FakeClient(module, [])
    recordings = Recordings(client, {}, memory.save, memory.notify, tmp_path, automatic=True)
    await recordings.refresh()
    client.listed = [clip]
    await recordings.refresh()
    assert [kind for kind, _ in memory.events] == ["clip_available", "clip_saved"]
    assert memory.events[0][1]["discovered_at"] == memory.events[1][1]["discovered_at"]
    record = recordings.state["archive"][clip.key]
    assert archive_file(record).read_bytes().endswith(b"media")
    assert archive_file(record).stat().st_mode & 0o777 == 0o600
    assert recordings.state["pending"] == []
    await recordings.download(clip.key)
    assert client.downloads == 1
    assert len(memory.events) == 2


async def test_archive_disabled_never_downloads(module, clip, memory, tmp_path):
    client = FakeClient(module, [])
    recordings = Recordings(client, {}, memory.save, memory.notify, tmp_path)
    await recordings.refresh()
    client.listed = [clip]
    await recordings.refresh()
    assert client.downloads == 0
    assert list(tmp_path.iterdir()) == []
    assert memory.events[0][0] == "clip_available"


async def test_interrupted_download_retains_pending_and_emits_no_saved(
    module, clip, memory, tmp_path
):
    client = FakeClient(module, [])
    recordings = Recordings(client, {}, memory.save, memory.notify, tmp_path, automatic=True)
    await recordings.refresh()
    client.listed, client.fail = [clip], True
    with pytest.raises(BlinkError):
        await recordings.refresh()
    assert recordings.state["pending"] == [clip.key]
    assert list(tmp_path.iterdir()) == []
    assert [kind for kind, _ in memory.events] == ["clip_available"]
    client.fail = False
    await recordings.refresh()
    assert [kind for kind, _ in memory.events] == ["clip_available", "clip_saved"]


async def test_retention_only_deletes_indexed_unchanged_files(module, clip, memory, tmp_path):
    client = FakeClient(module, [clip])
    recordings = Recordings(client, {}, memory.save, memory.notify, tmp_path, retention_days=30)
    await recordings.refresh()
    path = await recordings.download(clip.key)
    unrelated = tmp_path / "other.mp4"
    unrelated.write_bytes(b"untouched")
    recordings.state["archive"][clip.key]["saved_at"] = time.time() - 31 * 86400
    await recordings.prune()
    assert not path.exists()
    assert unrelated.read_bytes() == b"untouched"


@pytest.mark.parametrize("replacement", ["modified", "symlink"])
async def test_retention_refuses_replaced_files(replacement, module, clip, memory, tmp_path):
    recordings = Recordings(FakeClient(module, [clip]), {}, memory.save, memory.notify, tmp_path)
    await recordings.refresh()
    path = await recordings.download(clip.key)
    record = recordings.state["archive"][clip.key]
    record["saved_at"] = time.time() - 40 * 86400
    if replacement == "symlink":
        target = tmp_path / "private"
        target.write_bytes(b"private")
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_bytes(b"modified")
    with pytest.raises(BlinkError):
        archive_file(record)
    await recordings.prune()
    assert path.exists()


async def test_zero_retention_keeps_saved_files(module, clip, memory, tmp_path):
    recordings = Recordings(
        FakeClient(module, [clip]),
        {},
        memory.save,
        memory.notify,
        tmp_path,
        retention_days=0,
    )
    await recordings.refresh()
    path = await recordings.download(clip.key)
    recordings.state["archive"][clip.key]["saved_at"] = 0
    await recordings.prune()
    assert path.exists()


async def test_manifest_failure_does_not_baseline(module, memory):
    client = FakeClient(module, [])
    client.clips = AsyncMock(side_effect=BlinkError("unavailable"))
    recordings = Recordings(client, {}, memory.save, memory.notify)
    with pytest.raises(BlinkError):
        await recordings.refresh()
    assert recordings.state["baselined"] == []


async def test_download_refuses_to_overwrite_unindexed_file(module, clip, memory, tmp_path):
    recordings = Recordings(FakeClient(module, [clip]), {}, memory.save, memory.notify, tmp_path)
    await recordings.refresh()
    path = tmp_path / f"blink-live-{clip.key}.mp4"
    path.write_bytes(b"user-owned-file")
    with pytest.raises(FileExistsError):
        await recordings.download(clip.key)
    assert path.read_bytes() == b"user-owned-file"
    assert memory.events == []
    assert list(tmp_path.iterdir()) == [path]


async def test_cancelled_download_removes_partial_file(module, clip, memory, tmp_path):
    client = FakeClient(module, [clip])
    started = asyncio.Event()

    async def chunks(clip):
        yield b"\x00\x00\x00\x18ftypisom"
        started.set()
        await asyncio.Event().wait()

    client.clip_chunks = chunks
    recordings = Recordings(client, {}, memory.save, memory.notify, tmp_path)
    await recordings.refresh()
    task = asyncio.create_task(recordings.download(clip.key))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []
    assert recordings.state["archive"] == {}
    assert memory.events == []


async def test_archive_index_write_failure_leaves_retryable_download(
    module, clip, memory, tmp_path
):
    recordings = Recordings(FakeClient(module, [clip]), {}, memory.save, memory.notify, tmp_path)
    await recordings.refresh()
    recordings.state["pending"] = [clip.key]
    recordings.save = AsyncMock(side_effect=OSError("disk full"))
    with pytest.raises(OSError):
        await recordings.download(clip.key)
    assert recordings.state["pending"] == [clip.key]
    assert recordings.state["archive"] == {}
    assert list(tmp_path.iterdir()) == []
    assert memory.events == []
    recordings.save = memory.save
    assert (await recordings.download(clip.key)).exists()
    assert memory.events[0][0] == "clip_saved"

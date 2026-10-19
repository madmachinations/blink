import copy
import hashlib
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from blink_live_client import BlinkClient, BlinkError
from blink_live_client.errors import ApiError
from blink_live_client.recordings import Recordings


def media_row(**changes):
    return {
        "id": 5,
        "camera_id": 3,
        "network_id": 2,
        "device_type": "doorbell",
        "created_at": "2026-10-19T12:00:00Z",
        "media": "/api/v1/media/5.mp4",
        **changes,
    }


async def test_cloud_pagination_attribution_deleted_and_observations(device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(
            side_effect=[
                {
                    "media": [
                        media_row(),
                        media_row(id=6, deleted=True),
                        media_row(id=7, camera_id=999),
                    ]
                },
                {"media": [media_row(id=8)]},
                {"media": []},
            ]
        )
        observations = []
        client.observer = lambda kind, data: observations.append((kind, data))
        clips = await client.cloud_clips("2026-10-18T00:00:00+00:00")
        assert [clip.id for clip in clips] == ["5", "8"]
        assert all(clip.source == "cloud" and clip.module_id is None for clip in clips)
        assert clips[0].camera_key == device.key
        assert clips[0].created_at.endswith("+00:00")
        assert clips[0].event_data()["sync_module_id"] is None
        assert "media_path" not in clips[0].event_data()
        calls = client.request.call_args_list
        assert all(call.args[0] == "GET" for call in calls)
        assert [parse_qs(urlsplit(call.args[1]).query)["page"] for call in calls] == [
            ["0"],
            ["1"],
            ["2"],
        ]
        assert len(observations) == 2


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [media_row(media="https://evil.example/video")],
        [media_row(created_at="invalid")],
        [media_row(id="../5")],
    ],
)
async def test_cloud_rejects_bad_metadata_and_foreign_media(rows, device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(return_value={"media": rows})
        with pytest.raises(BlinkError):
            await client.cloud_clips()


async def test_cloud_repeated_page_fails_without_partial_success(device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(return_value={"media": [media_row()]})
        with pytest.raises(BlinkError, match="pagination"):
            await client.cloud_clips()
        assert client.request.await_count == 2


async def test_cloud_stream_does_not_need_usb_or_create_commands(clip, monkeypatch):
    cloud = replace(clip, source="cloud", module_id=None, media_path="/api/v1/media/5.mp4")
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.request = AsyncMock(side_effect=AssertionError("No REST mutation needed"))

        async def chunks(path):
            assert client.resource_url(path).startswith(client.root + "/")
            yield b"cloud video"

        monkeypatch.setattr(client, "resource_chunks", chunks)
        assert b"".join([part async for part in client.clip_chunks(cloud)]) == b"cloud video"
        client.request.assert_not_called()
        with pytest.raises(BlinkError, match="another account"):
            _ = [part async for part in client.clip_chunks(replace(cloud, account_id=999))]


def test_cloud_identity_is_separate_and_old_usb_keys_are_unchanged(clip):
    original = f"{clip.account_id}:{clip.network_id}:{clip.module_id}:{clip.id}:{clip.created_at}"
    assert clip.key == hashlib.sha256(original.encode()).hexdigest()[:32]
    assert replace(clip, source="cloud").key != clip.key
    assert (
        replace(clip, source="cloud", media_path="/new.mp4").key
        == replace(clip, source="cloud", media_path="/old.mp4").key
    )


async def test_combined_baselines_checkpoints_restart_and_failure_isolation(module, clip, memory):
    cloud = replace(clip, source="cloud", module_id=None)
    client = SimpleNamespace(
        modules={4: module},
        clips=AsyncMock(return_value=[clip]),
        cloud_clips=AsyncMock(return_value=[cloud]),
    )
    recordings = Recordings(client, {}, memory.save, memory.notify, recording_source="all")
    await recordings.refresh()
    assert memory.events == []
    assert memory.value["baselined"] == [4, "cloud"]
    assert memory.value["cloud_since"]
    assert len(recordings.clips) == 2
    checkpoint = memory.value["cloud_since"]
    client.cloud_clips.return_value = [cloud, replace(cloud, id="8")]
    restarted = Recordings(
        client, copy.deepcopy(memory.value), memory.save, memory.notify, recording_source="all"
    )
    await restarted.refresh()
    client.cloud_clips.assert_awaited_with(checkpoint)
    assert [data["source"] for _, data in memory.events] == ["cloud"]
    await restarted.refresh()
    assert len(memory.events) == 1
    old_checkpoint = memory.value["cloud_since"]
    client.cloud_clips.side_effect = ApiError(503)
    client.clips.return_value = [clip, replace(clip, id="9")]
    with pytest.raises(ApiError):
        await restarted.refresh()
    assert memory.value["cloud_since"] == old_checkpoint
    assert [data["source"] for _, data in memory.events] == ["cloud", "usb"]


async def test_cloud_failed_initial_scan_does_not_baseline_or_checkpoint(memory):
    client = SimpleNamespace(modules={}, cloud_clips=AsyncMock(side_effect=ApiError(403)))
    recordings = Recordings(client, {}, memory.save, memory.notify, recording_source="cloud")
    with pytest.raises(ApiError):
        await recordings.refresh()
    assert recordings.state["baselined"] == []
    assert "cloud_since" not in recordings.state
    assert memory.events == []


async def test_cloud_atomic_download_failure_and_local_only_retention(clip, memory, tmp_path):
    cloud = replace(clip, source="cloud", module_id=None, media_path="/api/v1/media/5.mp4")
    client = SimpleNamespace(modules={}, cloud_clips=AsyncMock(return_value=[cloud]))

    async def chunks(_clip):
        yield b"\x00\x00\x00\x18ftypisom" + b"\0" * 16 + b"video"

    client.clip_chunks = chunks
    recordings = Recordings(
        client, {}, memory.save, memory.notify, tmp_path, recording_source="cloud"
    )
    await recordings.refresh()
    path = await recordings.download(cloud.key)
    assert path.exists()
    assert [kind for kind, _ in memory.events] == ["clip_saved"]
    assert memory.events[0][1]["source"] == "cloud"
    recordings.state["archive"][cloud.key]["saved_at"] = time.time() - 31 * 86400
    client.cloud_clips.reset_mock()
    await recordings.prune()
    assert not path.exists()
    client.cloud_clips.assert_not_called()
    assert len(recordings.clips) == 1  # Identity remains indexed; originals are untouched.

    async def missing(_clip):
        raise ApiError(404)
        yield  # Make this an async generator, like the actual media transport.

    client.clip_chunks = missing
    memory.events.clear()
    with pytest.raises(ApiError):
        await recordings.download(cloud.key)
    assert list(tmp_path.iterdir()) == []
    assert memory.events == []

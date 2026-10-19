import asyncio
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from blink_live_client.errors import BlinkError
from blink_live_client.gateway import LiveSession, StreamGateway


@pytest.fixture(scope="session")
def ffmpeg():
    # imageio's Linux 7.0.2 binary crashes when demuxing MPEG-TS; use system FFmpeg.
    if executable := shutil.which("ffmpeg"):
        return executable
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    return imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(scope="session")
def transport_stream(ffmpeg):
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "15",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return result.stdout


async def test_lazy_gateway_token_and_first_viewer_disconnect(device, monkeypatch):
    class Client:
        devices = {device.key: device}

    started, stopped = asyncio.Event(), asyncio.Event()

    async def run(self):
        started.set()
        try:
            while True:
                for queue in self.consumers:
                    queue.put_nowait(b"\x47" + bytes(187))
                await asyncio.sleep(0.02)
        finally:
            stopped.set()

    monkeypatch.setattr(LiveSession, "run", run)
    gateway = StreamGateway(Client())
    await gateway.start()
    try:
        assert not started.is_set()
        assert gateway.url(device).startswith("http://127.0.0.1:")
        async with aiohttp.ClientSession() as http:
            async with http.get(gateway.url(device) + "invalid") as response:
                assert response.status == 404
            assert not started.is_set()
            async with http.get(gateway.url(device)) as response:
                assert response.status == 200
                assert len(await response.content.readexactly(188)) == 188
            async with asyncio.timeout(2):
                await stopped.wait()
                while gateway._sessions:  # noqa: ASYNC110 -- wait for asynchronous handler cleanup
                    await asyncio.sleep(0)
    finally:
        await gateway.close()


async def test_shared_session_and_cleanup(device, monkeypatch):
    client = type("Client", (), {"devices": {device.key: device}})()
    sessions = []

    async def run(self):
        sessions.append(self)
        try:
            while True:
                for queue in self.consumers:
                    queue.put_nowait(b"packet")
                await asyncio.sleep(0.01)
        finally:
            for queue in self.consumers:
                self.finish(queue)

    monkeypatch.setattr(LiveSession, "run", run)
    gateway = StreamGateway(client)
    first, second = gateway.subscribe(device), gateway.subscribe(device)
    try:
        assert await anext(first) == b"packet"
        assert await anext(second) == b"packet"
        assert len(sessions) == 1
        await first.aclose()
        assert not sessions[0].task.done()
        await second.aclose()
        assert sessions[0].task.done()
        assert gateway._sessions == {}
    finally:
        await gateway.close()


async def test_real_ffmpeg_audio_and_video_pipeline(device, ffmpeg, transport_stream, monkeypatch):
    client = type("Client", (), {"devices": {device.key: device}})()
    client.liveview = AsyncMock(return_value={"server": "fixture"})
    diagnostics = bytearray()
    processes = []

    async def capture_stderr(self, process):
        # This transport contains only synthetic media, so codec errors are safe to show.
        processes.append(process)
        while chunk := await process.stderr.read(4096):
            diagnostics.extend(chunk)

    monkeypatch.setattr(LiveSession, "_stderr", capture_stderr)

    class Transport:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def packets(self):
            for index in range(0, len(transport_stream), 1316):
                yield transport_stream[index : index + 1316]
                await asyncio.sleep(0.001)
            await asyncio.Event().wait()

    monkeypatch.setattr("blink_live_client.gateway.ImmisStream", Transport)
    gateway = StreamGateway(client, ffmpeg, duration=10)
    stream = gateway.subscribe(device)
    captured = bytearray()
    try:
        async with asyncio.timeout(8):
            while len(captured) < 100_000:
                chunk = await anext(stream, None)
                exit_status = processes[0].returncode if processes else "not started"
                assert chunk is not None, (
                    f"FFmpeg exit status: {exit_status}; {diagnostics.decode(errors='replace')}"
                )
                captured.extend(chunk)
        session = gateway._sessions[device.key]
        assert session.first_media_seconds is not None
        assert client.liveview.await_count == 1
    finally:
        await stream.aclose()
        await gateway.close()
    # Decode both outputs independently; invalid MPEG-TS or absent audio must fail the test.
    for mapping in ("0:v:0", "0:a:0"):
        result = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-map",
            mapping,
            "-f",
            "null",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await result.communicate(bytes(captured))
        assert result.returncode == 0, stderr.decode(errors="replace")


async def test_duration_limit_and_missing_ffmpeg_cleanup(device):
    client = type("Client", (), {})()
    client.liveview = AsyncMock(side_effect=RuntimeError("credential-like-data"))
    session = LiveSession(client, device, "missing", 1)
    queue = asyncio.Queue(1)
    session.consumers.add(queue)
    await session.run()
    assert await queue.get() is None
    assert "credential-like-data" not in session.failure


async def test_duration_limit_terminates_actual_normalizer(device, ffmpeg, monkeypatch):
    client = type("Client", (), {})()
    client.liveview = AsyncMock(return_value={"server": "fixture"})

    class Transport:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def packets(self):
            await asyncio.Event().wait()
            yield b"unreachable"

    monkeypatch.setattr("blink_live_client.gateway.ImmisStream", Transport)
    session = LiveSession(client, device, ffmpeg, 1)
    async with asyncio.timeout(5):
        await session.run()
    assert session.failure == "Live view reached its duration limit"


async def test_slow_viewer_cannot_block_other_viewers(device):
    session = LiveSession(None, device, "ffmpeg", 60)
    slow = asyncio.Queue(1)
    fast = asyncio.Queue(128)
    session.consumers.update([slow, fast])
    process = type("Process", (), {})()
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=[b"1", b"2", b"3", b""])
    with pytest.raises(BlinkError):
        await session._read(process)
    assert slow not in session.consumers
    assert await slow.get() is None
    assert [await fast.get() for _ in range(3)] == [b"1", b"2", b"3"]

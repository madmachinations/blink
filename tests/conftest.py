"""Offline fixtures; no test contacts Blink or uses a real account."""

import copy
import shutil
import subprocess

import pytest
from aiohttp import web
from blink_live_client.models import Clip, Device, SyncModule


@pytest.fixture
def device():
    return Device(3, 2, "doorbell", "Front Door", "ABC123456789", "lotus")


@pytest.fixture
def module():
    return SyncModule(4, 2, "Home", True)


@pytest.fixture
def clip(device):
    return Clip(1, 2, 4, "5", "6", device.key, "2026-10-19T12:00:00+00:00", 24)


@pytest.fixture
async def server():
    runners = []

    async def start(handler):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    yield start
    for runner in runners:
        await runner.cleanup()


class MemoryStore:
    def __init__(self):
        self.value = {}
        self.events = []

    async def save(self, value):
        self.value = copy.deepcopy(value)

    async def notify(self, kind, data):
        self.events.append((kind, copy.deepcopy(data)))


@pytest.fixture
def memory():
    return MemoryStore()


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
            "8",
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

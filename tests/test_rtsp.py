"""Protocol and real FFmpeg coverage, using synthetic media only."""

import asyncio
import base64
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from blink_live_client.errors import BlinkError
from blink_live_client.gateway import LiveSession, StreamGateway
from blink_live_client.rtsp import PACKET_QUEUE, RtpSession, RtspServer, _Packets

AUTH = "Basic " + base64.b64encode(b"viewer:example-test-password").decode()


async def request(reader, writer, method, url, seq=1, headers=None):
    fields = {"CSeq": str(seq), "Authorization": AUTH, **(headers or {})}
    writer.write(
        (
            f"{method} {url} RTSP/1.0\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in fields.items())
            + "\r\n"
        ).encode()
    )
    await writer.drain()
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode().split("\r\n")
    response = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    body = await reader.readexactly(int(response.get("Content-Length", 0)))
    return int(lines[0].split()[1]), response, body


@pytest.fixture
async def rtsp(device):
    gateway = StreamGateway(SimpleNamespace(devices={device.key: device}))
    server = RtspServer(gateway, "127.0.0.1", 0, "viewer", "example-test-password")
    await server.start()
    yield server, f"rtsp://127.0.0.1:{server.port}/{device.key}"
    await server.close()
    await gateway.close()


async def test_auth_options_and_unknown_path_are_lazy(rtsp):
    server, url = rtsp
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        code, headers, _ = await request(
            reader, writer, "OPTIONS", url, headers={"Authorization": "bad"}
        )
        assert code == 401 and "Basic" in headers["WWW-Authenticate"]
        code, headers, _ = await request(reader, writer, "OPTIONS", "*")
        assert code == 200 and "DESCRIBE" in headers["Public"]
        assert (await request(reader, writer, "DESCRIBE", url + "-missing"))[0] == 404
        assert not server._sessions and not server.gateway._sessions
    finally:
        writer.close()
        await writer.wait_closed()


async def test_setup_transport_sessions_and_teardown(rtsp, monkeypatch):
    server, url = rtsp
    ended = asyncio.Event()

    async def run(self):
        try:
            self.sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"
            self.ready.set()
            await asyncio.Event().wait()
        finally:
            ended.set()

    monkeypatch.setattr(RtpSession, "run", run)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        code, _, body = await request(reader, writer, "DESCRIBE", url)
        assert code == 200 and b"trackID=0" in body
        track = url + "/trackID=0"
        assert (
            await request(
                reader,
                writer,
                "SETUP",
                track,
                headers={"Transport": "RTP/AVP;unicast;client_port=5000-5001"},
            )
        )[0] == 461
        code, headers, _ = await request(
            reader,
            writer,
            "SETUP",
            track,
            headers={
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1",
            },
        )
        assert code == 200
        session = headers["Session"].split(";")[0]
        assert (await request(reader, writer, "PLAY", url, headers={"Session": "incorrect"}))[
            0
        ] == 454
        assert (await request(reader, writer, "PLAY", url, headers={"Session": session}))[0] == 200
        assert (await request(reader, writer, "GET_PARAMETER", url, headers={"Session": session}))[
            0
        ] == 200
        assert (await request(reader, writer, "TEARDOWN", url, headers={"Session": session}))[
            0
        ] == 200
        async with asyncio.timeout(2):
            await ended.wait()
            while server._sessions:  # noqa: ASYNC110 -- wait for awaited session teardown
                await asyncio.sleep(0)
        assert not server._sessions
    finally:
        writer.close()
        await writer.wait_closed()


async def test_malformed_request_and_conflicting_port(rtsp):
    server, _ = rtsp
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"DESCRIBE x RTSP/1.0\r\nCSeq: 1\r\nContent-Length: -1\r\n\r\n")
    await writer.drain()
    assert b"400 Bad Request" in await reader.read()
    writer.close()
    await writer.wait_closed()
    other = RtspServer(server.gateway, "127.0.0.1", server.port, "user", "password")
    with pytest.raises(BlinkError, match="bind"):
        await other.start()
    await other.close()


async def test_packet_fanout_bounds_and_inactive_descriptions():
    queue = asyncio.Queue(PACKET_QUEUE)
    session = SimpleNamespace(consumers={queue}, playing=set())
    packets = _Packets(session, 0, False)
    packets.datagram_received(b"1234", ("127.0.0.1", 1))
    assert queue.empty()  # DESCRIBE without PLAY does not fill packet buffers.
    session.playing.add(queue)
    for _ in range(PACKET_QUEUE + 1):
        packets.datagram_received(b"1234", ("127.0.0.1", 1))
    assert await queue.get() is None
    assert not session.playing
    assert queue in session.consumers  # Keep its reference until connection cleanup.


async def test_real_ffmpeg_rtsp_video_audio_and_shared_http(
    device, ffmpeg, transport_stream, monkeypatch
):
    started = []

    async def source(self):
        started.append(self)
        try:
            for index in range(0, len(transport_stream), 1316):
                for queue in tuple(self.consumers):
                    queue.put_nowait(transport_stream[index : index + 1316])
                await asyncio.sleep(0.012)
            await asyncio.Event().wait()
        finally:
            for queue in tuple(self.consumers):
                self.finish(queue)

    monkeypatch.setattr(LiveSession, "run", source)
    gateway = StreamGateway(SimpleNamespace(devices={device.key: device}), ffmpeg, 10)
    server = RtspServer(gateway, "127.0.0.1", 0, "viewer", "example-test-password")
    await gateway.start()
    await server.start()
    url = f"rtsp://viewer:example-test-password@127.0.0.1:{server.port}/{device.key}"
    processes = []
    streams = []
    try:
        assert not started
        # Two actual RTSP clients decode both tracks, sharing one packetizer and
        # one upstream session with an independent MPEG-TS/HA consumer.
        for _ in range(2):
            processes.append(
                await asyncio.create_subprocess_exec(
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-rtsp_transport",
                    "tcp",
                    "-probesize",
                    "32768",
                    "-analyzeduration",
                    "1000000",
                    "-i",
                    url,
                    "-t",
                    "1",
                    "-map",
                    "0:v:0",
                    "-f",
                    "null",
                    "-",
                    "-t",
                    "1",
                    "-map",
                    "0:a:0",
                    "-f",
                    "s16le",
                    "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        async with asyncio.timeout(20):
            while not server._sessions:  # noqa: ASYNC110 -- observe actual network setup
                await asyncio.sleep(0.01)
            stream = gateway.subscribe(device)
            streams.append(stream)
            assert await anext(stream)
            results = await asyncio.gather(*(p.communicate() for p in processes))
        for process, (pcm_audio, stderr) in zip(processes, results, strict=True):
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert len(pcm_audio) > 4800  # Decoded audio samples, not just an advertised track.
        assert len(started) == 1
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for stream in streams:
            await stream.aclose()
        await server.close()
        await gateway.close()
    assert not server._clients and not server._sessions and not gateway._sessions


async def test_failed_packetizer_returns_unavailable_and_cleans_up(rtsp):
    server, url = rtsp
    server.gateway.ffmpeg = "/nonexistent/blink-test-ffmpeg"
    server.gateway.client.liveview = AsyncMock(side_effect=AssertionError("must not connect"))
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        assert (await request(reader, writer, "DESCRIBE", url))[0] == 503
        assert await reader.read() == b""
        assert not server._sessions
    finally:
        writer.close()
        await writer.wait_closed()


async def test_disconnect_during_describe_cancels_upstream(rtsp, monkeypatch):
    server, url = rtsp
    started, ended = asyncio.Event(), asyncio.Event()

    async def run(self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    monkeypatch.setattr(RtpSession, "run", run)
    _, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write((f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAuthorization: {AUTH}\r\n\r\n").encode())
    await writer.drain()
    await started.wait()
    writer.close()
    await writer.wait_closed()
    async with asyncio.timeout(2):
        await ended.wait()
        while server._sessions:  # noqa: ASYNC110 -- observe cancellation finishing
            await asyncio.sleep(0)
    assert not server._sessions


async def test_expiry_disconnect_and_new_request_reopens(rtsp, monkeypatch):
    server, url = rtsp
    sessions = []

    async def run(self):
        sessions.append(self)
        try:
            self.sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"
            self.ready.set()
            await asyncio.sleep(0.2)
        finally:
            for queue in self.consumers:
                queue.put_nowait(None)

    monkeypatch.setattr(RtpSession, "run", run)
    for _ in range(2):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert (await request(reader, writer, "DESCRIBE", url))[0] == 200
        _, headers, _ = await request(
            reader,
            writer,
            "SETUP",
            url + "/trackID=0",
            headers={
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1",
            },
        )
        session = headers["Session"].split(";")[0]
        assert (await request(reader, writer, "PLAY", url, headers={"Session": session}))[0] == 200
        async with asyncio.timeout(2):
            assert await reader.read() == b""
            while server._sessions:  # noqa: ASYNC110 -- wait for socket teardown
                await asyncio.sleep(0)
        writer.close()
        await writer.wait_closed()
    assert len(sessions) == 2


async def test_real_video_only_camera_and_last_disconnect(device, ffmpeg, transport_stream):
    result = await asyncio.to_thread(
        subprocess.run,
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-f",
            "mpegts",
            "pipe:1",
        ],
        input=transport_stream,
        capture_output=True,
        check=True,
    )
    video = result.stdout
    stopped = asyncio.Event()

    class Gateway:
        client = SimpleNamespace(devices={device.key: device})

        async def subscribe(self, _device):
            try:
                for index in range(0, len(video), 1316):
                    yield video[index : index + 1316]
                    await asyncio.sleep(0.012)
                await asyncio.Event().wait()
            finally:
                stopped.set()

    gateway = Gateway()
    gateway.ffmpeg = ffmpeg
    server = RtspServer(gateway, "127.0.0.1", 0, "viewer", "example-test-password")
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        url = f"rtsp://127.0.0.1:{server.port}/{device.key}"
        async with asyncio.timeout(8):
            code, _, description = await request(reader, writer, "DESCRIBE", url)
        assert code == 200 and b"m=video" in description and b"m=audio" not in description
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(3):
            await stopped.wait()
    finally:
        writer.close()
        await server.close()
    assert not server._sessions


async def test_camera_client_limit_and_fragmented_request(rtsp, monkeypatch):
    server, url = rtsp

    async def run(self):
        self.sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"
        self.ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RtpSession, "run", run)
    writers = []
    try:
        for i in range(5):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writers.append(writer)
            if i < 4:
                assert (await request(reader, writer, "DESCRIBE", url))[0] == 200
            else:
                writer.write(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\n".encode())
                await writer.drain()
                await asyncio.sleep(0)
                writer.write(f"Authorization: {AUTH}\r\n\r\n".encode())
                await writer.drain()
                assert b"503 Service Unavailable" in await reader.read()
        assert len(server._sessions) == 1
        assert len(next(iter(server._sessions.values())).consumers) == 4
    finally:
        for writer in writers:
            writer.close()
            await writer.wait_closed()

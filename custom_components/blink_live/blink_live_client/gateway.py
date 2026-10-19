"""Lazy, bounded local media fanout for HA Stream, HomeKit FFmpeg, and ffplay."""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator

from aiohttp import web

from .client import BlinkClient
from .errors import BlinkError
from .immis import ImmisStream
from .models import Device

_LOGGER = logging.getLogger(__name__)
CHUNK = 32 * 1024
QUEUE_CHUNKS = 64  # At most 2 MiB per consumer; never block every viewer for a slow one.


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(3):
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class LiveSession:
    def __init__(self, client: BlinkClient, device: Device, ffmpeg: str, duration: int) -> None:
        self.client, self.device = client, device
        self.ffmpeg, self.duration = ffmpeg, duration
        self.consumers: set[asyncio.Queue] = set()
        self.started = time.monotonic()
        self.first_media_seconds: float | None = None
        self.failure: str | None = None
        self.task: asyncio.Task | None = None

    def finish(self, queue: asyncio.Queue) -> None:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    async def _feed(self, transport: ImmisStream, process) -> None:
        try:
            async for payload in transport.packets():
                process.stdin.write(payload)
                async with asyncio.timeout(10):
                    await process.stdin.drain()
        finally:
            process.stdin.close()

    async def _read(self, process) -> None:
        while True:
            async with asyncio.timeout(25):
                chunk = await process.stdout.read(CHUNK)
            if not chunk:
                raise BlinkError("Media normalizer stopped")
            if self.first_media_seconds is None:
                self.first_media_seconds = time.monotonic() - self.started
            for queue in tuple(self.consumers):
                if queue.full():
                    self.consumers.discard(queue)
                    self.finish(queue)
                else:
                    queue.put_nowait(chunk)

    async def _stderr(self, process) -> None:
        # Vendor data can reach codec diagnostics; drain but do not log or retain it.
        while await process.stderr.read(4096):
            pass

    async def _monitor(self, command_id: int, interval: float) -> None:
        while True:
            status = await self.client.command(self.device.network_id, command_id)
            if status.get("complete") or status.get("status") in ("complete", "failed"):
                raise BlinkError("Blink ended the live-view command")
            await asyncio.sleep(interval)

    async def run(self) -> None:
        process = None
        try:
            async with asyncio.timeout(self.duration):
                response = await self.client.liveview(self.device)
                async with ImmisStream(response["server"], self.device.serial) as transport:
                    process = await asyncio.create_subprocess_exec(
                        self.ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-fflags",
                        "+genpts",
                        "-probesize",
                        "32768",
                        "-analyzeduration",
                        "1000000",
                        "-f",
                        "mpegts",
                        "-i",
                        "pipe:0",
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "64k",
                        "-ac",
                        "1",
                        "-mpegts_flags",
                        "+resend_headers",
                        "-pat_period",
                        "0.1",
                        "-flush_packets",
                        "1",
                        "-f",
                        "mpegts",
                        "pipe:1",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    async with asyncio.TaskGroup() as group:
                        group.create_task(self._feed(transport, process))
                        group.create_task(self._read(process))
                        group.create_task(self._stderr(process))
                        command_id = response.get("command_id", response.get("id"))
                        if command_id:
                            group.create_task(
                                self._monitor(
                                    int(command_id),
                                    min(15, max(1, float(response.get("polling_interval", 5)))),
                                )
                            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.failure = "Live view reached its duration limit"
        except BlinkError as error:
            self.failure = str(error)
        except Exception:
            # No raw exception interpolation: nested codec/TLS exceptions can contain URLs.
            self.failure = "Live view ended, failed, or reached its duration limit"
            _LOGGER.debug("Blink live-view session ended")
        finally:
            if process:
                await stop_process(process)
            for queue in tuple(self.consumers):
                self.finish(queue)


class StreamGateway:
    def __init__(self, client: BlinkClient, ffmpeg: str = "ffmpeg", duration: int = 60) -> None:
        if not 1 <= duration <= 300:
            raise ValueError("Stream duration must be between 1 and 300 seconds")
        self.client, self.ffmpeg, self.duration = client, ffmpeg, duration
        self._token = secrets.token_urlsafe(32)
        self._sessions: dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        self._closing = False
        self.results: dict[str, dict] = {}

    async def start(self) -> None:
        app = web.Application(handler_args={"handler_cancellation": True})
        app.router.add_get("/stream/{key}/{token}", self._handle)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
        await self._runner.setup()
        try:
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            self._port = self._runner.addresses[0][1]
        except BaseException:
            await self._runner.cleanup()
            raise

    def url(self, device: Device) -> str:
        if not self._port:
            raise BlinkError("Media gateway has not started")
        return f"http://127.0.0.1:{self._port}/stream/{device.key}/{self._token}"

    async def subscribe(self, device: Device) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(QUEUE_CHUNKS)
        async with self._lock:
            if self._closing:
                raise BlinkError("Media gateway is stopping")
            session = self._sessions.get(device.key)
            if session is None or session.task.done():
                session = LiveSession(self.client, device, self.ffmpeg, self.duration)
                self._sessions[device.key] = session
                session.task = asyncio.create_task(session.run(), name="blink-live-session")
            if len(session.consumers) >= 4:
                raise BlinkError("Camera viewer limit reached")
            session.consumers.add(queue)
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            async with self._lock:
                session.consumers.discard(queue)
                if not session.consumers:
                    session.task.cancel()
                    await asyncio.gather(session.task, return_exceptions=True)
                    self.results[device.key] = {
                        "first_media_seconds": session.first_media_seconds,
                        "failure": session.failure,
                    }
                    if self._sessions.get(device.key) is session:
                        self._sessions.pop(device.key, None)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not secrets.compare_digest(request.match_info["token"], self._token):
            raise web.HTTPNotFound()
        device = self.client.devices.get(request.match_info["key"])
        if device is None:
            raise web.HTTPNotFound()
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        stream = self.subscribe(device)
        try:
            async with asyncio.timeout(35):
                first = await anext(stream)
            response = web.StreamResponse(
                headers={
                    "Content-Type": "video/mp2t",
                    "Cache-Control": "no-store",
                }
            )
            await response.prepare(request)
            async with asyncio.timeout(10):
                await response.write(first)
            async for chunk in stream:
                async with asyncio.timeout(10):
                    await response.write(chunk)
            return response
        except (StopAsyncIteration, BlinkError, TimeoutError):
            raise web.HTTPServiceUnavailable(reason="Live view unavailable") from None
        finally:
            await stream.aclose()

    async def close(self) -> None:
        self._closing = True
        tasks = [session.task for session in self._sessions.values() if session.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._sessions.clear()

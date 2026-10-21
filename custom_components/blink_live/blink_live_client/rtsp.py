"""Read-only RTSP/TCP bridge; FFmpeg supplies RTP, the gateway owns Blink sessions.

There is deliberately no publisher, discovery endpoint, UDP client transport, or
automatic upstream restart. Each camera has one RTP fanout, shared by its clients.
"""

import asyncio
import base64
import contextlib
import logging
import secrets
import struct
from urllib.parse import unquote, urlsplit

from .errors import BlinkError
from .gateway import stop_process

MAX_CLIENTS = 16
MAX_CAMERA_CLIENTS = 4
PACKET_QUEUE = 256
METHODS = "OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN"
_LOGGER = logging.getLogger(__name__)


class _Packets(asyncio.DatagramProtocol):
    def __init__(self, session, track, rtcp):
        self.session, self.track, self.rtcp = session, track, rtcp

    def datagram_received(self, data, addr):
        if addr[0] != "127.0.0.1" or not 4 <= len(data) <= 65535:
            return
        for queue in tuple(self.session.playing):
            if queue.full():
                self.session.playing.discard(queue)
                _finish(queue)
            else:
                queue.put_nowait((self.track, self.rtcp, data))


def _finish(queue):
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait(None)


class RtpSession:
    """One subscription, H.264 copy and AAC-LC RTP muxers per camera."""

    def __init__(self, gateway, device):
        self.gateway, self.device = gateway, device
        self.consumers = set()
        self.playing = set()
        self.sdp = None
        self.ready = asyncio.Event()
        self.task = None

    async def _description(self, process, track):
        data = bytearray()
        async with asyncio.timeout(35):
            while len(data) < 8192:
                try:
                    async with asyncio.timeout(0.1 if data else 35):
                        chunk = await process.stdout.read(8192 - len(data))
                except TimeoutError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
        lines = data.decode("ascii", errors="strict").splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("m=")), None)
        if start is None or not any(line.startswith("a=rtpmap:") for line in lines):
            raise BlinkError("RTP media description unavailable")
        media = [line for line in lines[start:] if not line.startswith(("c=", "a=control:"))]
        # The UDP destination belongs only to our local packetizer, not the client.
        fields = media[0].split()
        fields[1] = "0"
        media[0] = " ".join(fields)
        return "\r\n".join([*media, f"a=control:trackID={track}"]) + "\r\n"

    async def _feed(self, processes):
        stream = self.gateway.subscribe(self.device)
        try:
            async for chunk in stream:
                for index, process in enumerate(processes):
                    if process.returncode is not None:
                        if index == 0:
                            return
                        continue  # Video-only cameras have no audio output.
                    try:
                        process.stdin.write(chunk)
                        async with asyncio.timeout(5):
                            await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        if index == 0:
                            return
        finally:
            await stream.aclose()

    async def run(self):
        processes, transports, tasks = [], [], []
        try:
            loop = asyncio.get_running_loop()
            descriptions = []
            for track, mapping in enumerate(("0:v:0", "0:a:0")):
                ports = []
                for rtcp in (False, True):
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda track=track, rtcp=rtcp: _Packets(self, track, rtcp),
                        local_addr=("127.0.0.1", 0),
                    )
                    transports.append(transport)
                    ports.append(transport.get_extra_info("sockname")[1])
                process = await asyncio.create_subprocess_exec(
                    self.gateway.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "quiet",
                    "-nostdin",
                    "-probesize",
                    "32768",
                    "-analyzeduration",
                    "1000000",
                    "-f",
                    "mpegts",
                    "-i",
                    "pipe:0",
                    "-map",
                    mapping,
                    # AAC from MPEG-TS has no global headers. Recreate AAC-LC
                    # headers for widely supported MPEG4-GENERIC RTP rather
                    # than LATM, which some clients cannot identify as AAC.
                    *(
                        [
                            "-c:a",
                            "aac",
                            "-profile:a",
                            "aac_low",
                            "-b:a",
                            "64k",
                            "-ac",
                            "1",
                            "-flags:a",
                            "+global_header",
                        ]
                        if track
                        else ["-c:v", "copy"]
                    ),
                    "-payload_type",
                    str(96 + track),
                    "-f",
                    "rtp",
                    "-sdp_file",
                    "pipe:1",
                    f"rtp://127.0.0.1:{ports[0]}?rtcpport={ports[1]}&pkt_size=1200",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                processes.append(process)
                descriptions.append(asyncio.create_task(self._description(process, track)))
                tasks.append(descriptions[-1])
            feed = asyncio.create_task(self._feed(processes))
            tasks.append(feed)
            video = await descriptions[0]
            audio = ""
            try:
                # Missing audio must not prevent video playback; do not wait a full
                # camera session if an audio-less input has not finished probing.
                async with asyncio.timeout(2):
                    audio = await descriptions[1]
            except (BlinkError, TimeoutError, UnicodeError):
                pass
            self.sdp = (
                "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Blink Live\r\n"
                "c=IN IP4 0.0.0.0\r\nt=0 0\r\na=control:*\r\n" + video + audio
            )
            self.ready.set()
            ended = asyncio.create_task(processes[0].wait())
            tasks.append(ended)
            await asyncio.wait([feed, ended], return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No exception content: subprocess and transport errors may contain URLs.
            pass
        finally:
            self.ready.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for process in processes:
                await stop_process(process)
            for transport in transports:
                transport.close()
            for queue in tuple(self.consumers):
                _finish(queue)


class RtspServer:
    def __init__(self, gateway, host, port, username, password):
        if (
            not username
            or not password
            or ":" in username
            or any(char in username + password for char in "\r\n")
        ):
            raise BlinkError("RTSP requires a username and password")
        self.gateway, self.host, self.port = gateway, host, port
        self._authorization = (
            "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        )
        self._server = None
        self._clients = set()
        self._sessions = {}
        self._lock = asyncio.Lock()

    async def start(self):
        try:
            self._server = await asyncio.start_server(
                self._accept, self.host, self.port, limit=16384
            )
        except OSError:
            _LOGGER.error(
                "Blink Live RTSP listener failed; check listen address and port %s", self.port
            )
            raise BlinkError("RTSP could not bind its listen address and port") from None
        self.port = self._server.sockets[0].getsockname()[1]

    def _accept(self, reader, writer):
        if len(self._clients) >= MAX_CLIENTS:
            writer.close()
            return
        task = asyncio.create_task(_Connection(self, reader, writer).run())
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def acquire(self, key, queue):
        async with self._lock:
            session = self._sessions.get(key)
            if session is None or session.task.done():
                session = RtpSession(self.gateway, self.gateway.client.devices[key])
                self._sessions[key] = session
                session.task = asyncio.create_task(session.run())
            if len(session.consumers) >= MAX_CAMERA_CLIENTS:
                raise BlinkError("RTSP camera viewer limit reached")
            session.consumers.add(queue)
        return session

    async def release(self, session, queue):
        async with self._lock:
            session.consumers.discard(queue)
            session.playing.discard(queue)
            if not session.consumers:
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
                if self._sessions.get(session.device.key) is session:
                    del self._sessions[session.device.key]

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()


class _Connection:
    def __init__(self, server, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.queue = asyncio.Queue(PACKET_QUEUE)
        self.session = None
        self.key = None
        self.id = secrets.token_hex(16)
        self.channels = {}
        self.write_lock = asyncio.Lock()
        self.sender = None

    async def _write(self, data):
        async with self.write_lock:
            self.writer.write(data)
            async with asyncio.timeout(5):
                await self.writer.drain()

    async def _reply(self, code, seq, headers=None, body=""):
        reasons = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            454: "Session Not Found",
            455: "Method Not Valid in This State",
            461: "Unsupported Transport",
            503: "Service Unavailable",
            505: "RTSP Version Not Supported",
        }
        fields = {"CSeq": seq, "Server": "Blink Live", **(headers or {})}
        encoded = body.encode()
        if encoded:
            fields["Content-Length"] = str(len(encoded))
        head = f"RTSP/1.0 {code} {reasons[code]}\r\n"
        head += "".join(f"{key}: {value}\r\n" for key, value in fields.items())
        await self._write(head.encode() + b"\r\n" + encoded)

    async def _request(self):
        async with asyncio.timeout(60):
            first = await self.reader.readexactly(1)
            while first == b"$":
                # Clients send receiver reports over the negotiated RTCP channels.
                header = await self.reader.readexactly(3)
                size = struct.unpack("!H", header[1:])[0]
                await self.reader.readexactly(size)
                first = await self.reader.readexactly(1)
            raw = first + await self.reader.readuntil(b"\r\n\r\n")
            if len(raw) > 16384:
                raise ValueError
            lines = raw.decode("ascii").split("\r\n")
            method, uri, version = lines[0].split()
            headers = {}
            for line in lines[1:-2]:
                key, value = line.split(":", 1)
                key = key.lower().strip()
                if key in headers:
                    raise ValueError
                headers[key] = value.strip()
            size = int(headers.get("content-length", "0"))
            if not 0 <= size <= 65536 or not headers.get("cseq", "").isdigit():
                raise ValueError
            await self.reader.readexactly(size)
            return method, uri, version, headers

    async def _send_packets(self):
        try:
            while (packet := await self.queue.get()) is not None:
                track, rtcp, data = packet
                if track in self.channels:
                    channel = self.channels[track] + int(rtcp)
                    await self._write(b"$" + bytes([channel]) + struct.pack("!H", len(data)) + data)
        finally:
            self.writer.close()

    async def _watch_disconnect(self, owner):
        # A DESCRIBE can be waiting on a sleeping camera while its client has
        # already gone away. Release that subscription promptly, without reading
        # ahead into pipelined RTSP requests or consuming their interleaved data.
        while True:
            await asyncio.sleep(0.1)
            if self.reader.at_eof() or self.writer.is_closing():
                owner.cancel()
                return
            if self.session and self.session.task.done():
                owner.cancel()
                return

    async def run(self):
        watcher = asyncio.create_task(self._watch_disconnect(asyncio.current_task()))
        seq = "0"
        try:
            while True:
                method, uri, version, headers = await self._request()
                seq = headers["cseq"]
                if version != "RTSP/1.0":
                    await self._reply(505, seq)
                    continue
                if not secrets.compare_digest(
                    headers.get("authorization", ""), self.server._authorization
                ):
                    await self._reply(401, seq, {"WWW-Authenticate": 'Basic realm="Blink Live"'})
                    continue
                if method == "OPTIONS":
                    await self._reply(200, seq, {"Public": METHODS})
                    continue
                path = unquote(urlsplit(uri).path).strip("/")
                key = path.split("/")[0]
                if key not in self.server.gateway.client.devices or (self.key and key != self.key):
                    await self._reply(404, seq)
                    continue
                if method == "DESCRIBE":
                    if path != key:
                        await self._reply(404, seq)
                        continue
                    if self.session is None:
                        self.session = await self.server.acquire(key, self.queue)
                        self.key = key
                    async with asyncio.timeout(38):
                        await self.session.ready.wait()
                    if not self.session.sdp or self.session.task.done():
                        await self._reply(503, seq)
                        return
                    await self._reply(
                        200,
                        seq,
                        {
                            "Content-Type": "application/sdp",
                            "Content-Base": uri.rstrip("/") + "/",
                        },
                        self.session.sdp,
                    )
                elif method == "SETUP":
                    if not self.session or self.sender:
                        await self._reply(455, seq)
                        continue
                    track = next((i for i in (0, 1) if path == f"{key}/trackID={i}"), None)
                    if track is None or f"trackID={track}" not in self.session.sdp:
                        await self._reply(404, seq)
                        continue
                    transport = headers.get("transport", "")
                    parts = transport.split(";")
                    pair = next((p[12:] for p in parts if p.startswith("interleaved=")), "")
                    try:
                        channels = [int(value) for value in pair.split("-")]
                        valid = (
                            len(channels) == 2
                            and 0 <= channels[0] < 255
                            and channels[1] == channels[0] + 1
                            and not any(
                                c in (v, v + 1) for v in self.channels.values() for c in channels
                            )
                        )
                    except ValueError:
                        valid = False
                    if parts[0] != "RTP/AVP/TCP" or "unicast" not in parts or not valid:
                        await self._reply(461, seq)
                        continue
                    if "session" in headers and headers["session"].split(";")[0] != self.id:
                        await self._reply(454, seq)
                        continue
                    self.channels[track] = channels[0]
                    await self._reply(
                        200,
                        seq,
                        {
                            "Transport": f"RTP/AVP/TCP;unicast;interleaved={pair}",
                            "Session": f"{self.id};timeout=60",
                        },
                    )
                elif method in ("PLAY", "GET_PARAMETER", "TEARDOWN"):
                    if not self.session or headers.get("session", "").split(";")[0] != self.id:
                        await self._reply(454, seq)
                        continue
                    if method == "PLAY" and not self.channels:
                        await self._reply(455, seq)
                        continue
                    if method == "PLAY" and self.session.task.done():
                        await self._reply(503, seq)
                        return
                    await self._reply(200, seq, {"Session": self.id})
                    if method == "PLAY" and not self.sender:
                        while not self.queue.empty():
                            self.queue.get_nowait()
                        self.session.playing.add(self.queue)
                        self.sender = asyncio.create_task(self._send_packets())
                    elif method == "TEARDOWN":
                        return
                else:
                    await self._reply(405, seq, {"Public": METHODS})
        except asyncio.CancelledError:
            raise
        except (ValueError, UnicodeError, asyncio.LimitOverrunError):
            with contextlib.suppress(OSError, TimeoutError):
                await self._reply(400, "0")
        except BlinkError:
            with contextlib.suppress(OSError, TimeoutError):
                await self._reply(503, seq)
        except (OSError, EOFError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if self.sender:
                self.sender.cancel()
                await asyncio.gather(self.sender, return_exceptions=True)
            if self.session:
                await self.server.release(self.session, self.queue)
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()

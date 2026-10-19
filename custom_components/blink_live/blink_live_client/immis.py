"""Bounded TLS IMMIS reader. Wire layout adapted from the MIT reference; see NOTICE."""

import asyncio
import contextlib
import errno
import ipaddress
import socket
import ssl
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import BlinkError

MAX_FRAME = 1024 * 1024
HEADER = struct.Struct(">BII")
# Android libwalnut's explicit TLS identity for IMMIS public IPv4 authorities.
# The TCP destination stays the admitted IP; DNS endpoints keep their original name.
IPV4_TLS_IDENTITY = "*.immedia-semi.com"


def connection_failure(error: BaseException) -> str:
    """Fixed diagnostic categories; never echo a credential URL, address or vendor error."""
    if isinstance(error, ssl.SSLCertVerificationError):
        code = getattr(error, "verify_code", None)
        category = "certificate_identity" if code in (62, 64) else "certificate_trust"
        if isinstance(code, int):
            return f"{category}, verify_code={code}"
        return category
    if isinstance(error, socket.gaierror):
        return "dns_resolution"
    if isinstance(error, TimeoutError):
        return "connection_or_handshake_timeout"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(error, ConnectionResetError):
        return "connection_reset"
    if isinstance(error, ssl.SSLError):
        return "tls_protocol"
    if isinstance(error, OSError) and error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return "network_unreachable"
    return "connection_error"


def destination(url: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "immis" or parsed.port not in (None, 443) or parsed.username:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global or address.version != 4:
                raise ValueError
        except ValueError:
            if not host.endswith(".immedia-semi.com"):
                raise ValueError from None
        values = parse_qs(parsed.query)
        clients = values.get("client_id", [])
        if len(clients) != 1 or not clients[0].isascii() or not clients[0].isdigit():
            raise ValueError
        client = int(clients[0])
        connection = parsed.path.rsplit("/", 1)[-1].split("__", 1)[0]
        if not 0 <= client <= 0xFFFFFFFF or not 1 <= len(connection.encode()) <= 16:
            raise ValueError
        if parsed.fragment:
            raise ValueError
        return host, client, connection
    except (ValueError, TypeError):
        raise BlinkError("Unsupported or invalid Blink live-view destination") from None


def auth_header(serial: str, client: int, connection: str) -> bytes:
    if not 1 <= len(serial.encode()) <= 16:
        raise BlinkError("Invalid camera serial for IMMIS")
    return struct.pack(
        ">II16sIBBI64sI16sI",
        0x28,
        16,
        serial.encode(),
        client,
        1,
        8,
        64,
        bytes(64),
        16,
        connection.encode(),
        1,
    )


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(Path(__file__).with_name("immis_ca.pem")))
    return context


class ImmisStream:
    def __init__(self, url: str, serial: str) -> None:
        self.host, self.client, self.connection = destination(url)
        try:
            ipaddress.IPv4Address(self.host)
        except ipaddress.AddressValueError:
            self.servername = self.host
        else:
            self.servername = IPV4_TLS_IDENTITY
        self.serial = serial
        self.writer: asyncio.StreamWriter | None = None
        self._keepalive_task: asyncio.Task | None = None

    async def __aenter__(self):
        try:
            async with asyncio.timeout(15):
                records = await asyncio.get_running_loop().getaddrinfo(
                    self.host,
                    443,
                    type=socket.SOCK_STREAM,
                )
                addresses = list(dict.fromkeys(record[4][0] for record in records))
                if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                    raise BlinkError("Refused non-public IMMIS destination")
                context = await asyncio.to_thread(tls_context)
                last_error = None
                for address in addresses:
                    try:
                        self.reader, self.writer = await asyncio.open_connection(
                            address,
                            443,
                            ssl=context,
                            server_hostname=self.servername,
                            ssl_handshake_timeout=5,
                            limit=MAX_FRAME + 9,
                        )
                        break
                    except (OSError, TimeoutError) as error:
                        last_error = error
                if self.writer is None:
                    raise BlinkError(
                        f"IMMIS TLS connection failed ({connection_failure(last_error)})"
                    ) from None
                self.writer.write(auth_header(self.serial, self.client, self.connection))
                self.writer.write(HEADER.pack(0x0A, 0, 0))
                await self.writer.drain()
                self._keepalive_task = asyncio.create_task(self._keepalive())
                return self
        except (OSError, TimeoutError) as error:
            await self.close()
            raise BlinkError(f"IMMIS TLS connection failed ({connection_failure(error)})") from None
        except BaseException:
            await self.close()
            raise

    async def _keepalive(self) -> None:
        sequence = 0
        try:
            while True:
                await asyncio.sleep(10)
                sequence = (sequence + 1) & 0xFFFFFFFF
                self.writer.write(HEADER.pack(0x0A, sequence, 0))
                async with asyncio.timeout(5):
                    await self.writer.drain()
        except (OSError, TimeoutError):
            self.writer.close()

    async def packets(self) -> AsyncIterator[bytes]:
        authenticated = False
        try:
            while True:
                async with asyncio.timeout(25):
                    kind, _, size = HEADER.unpack(await self.reader.readexactly(HEADER.size))
                    if size > MAX_FRAME:
                        raise BlinkError("Oversized IMMIS packet")
                    payload = await self.reader.readexactly(size)
                if kind == 0x06 and size == 0:
                    authenticated = True
                elif kind == 0 and payload:
                    if not authenticated:
                        raise BlinkError("IMMIS media arrived before authentication acceptance")
                    if payload[0] != 0x47:
                        raise BlinkError("Unsupported IMMIS video framing")
                    yield payload
        except (asyncio.IncompleteReadError, OSError, TimeoutError):
            raise BlinkError("IMMIS stream ended or stalled") from None

    async def close(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(3):
                    await self.writer.wait_closed()

    async def __aexit__(self, *_):
        await self.close()

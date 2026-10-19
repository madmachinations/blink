import asyncio
import contextlib
import socket
import ssl
import struct
import subprocess

import pytest
from blink_live_client.errors import BlinkError
from blink_live_client.immis import (
    HEADER,
    IPV4_TLS_IDENTITY,
    MAX_FRAME,
    ImmisStream,
    auth_header,
    connection_failure,
    destination,
    tls_context,
)

HOST = "test.immedia-semi.com"
URL = f"immis://{HOST}:443/live/connection123?client_id=123"


@pytest.fixture(scope="session")
def certificate(tmp_path_factory):
    folder = tmp_path_factory.mktemp("tls")
    key, cert = folder / "key.pem", folder / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={HOST}",
            "-addext",
            f"subjectAltName=DNS:{HOST}",
        ],
        check=True,
        capture_output=True,
    )
    return key, cert


@pytest.fixture
async def tls_server(certificate, monkeypatch):
    key, cert = certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    servers = []
    original_connect = asyncio.open_connection
    loop = asyncio.get_running_loop()
    original_resolve = loop.getaddrinfo

    async def start(handler, trusted=True, identity=HOST, certificate_override=None):
        active_context = context
        active_cert = cert
        if certificate_override:
            active_key, active_cert = certificate_override
            active_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            active_context.load_cert_chain(active_cert, active_key)
        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=active_context)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]

        async def resolve(host, *args, **kwargs):
            if host == HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
            return await original_resolve(host, *args, **kwargs)

        async def connect(host, requested_port, **kwargs):
            assert host == "1.1.1.1"
            assert requested_port == 443
            assert kwargs["server_hostname"] == identity
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
            assert kwargs["ssl"].check_hostname
            return await original_connect("127.0.0.1", port, **kwargs)

        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        monkeypatch.setattr(asyncio, "open_connection", connect)
        if trusted:
            monkeypatch.setattr(
                "blink_live_client.immis.tls_context",
                lambda: ssl.create_default_context(cafile=str(active_cert)),
            )
        return server

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def test_auth_header_wire_layout():
    data = auth_header("SERIAL123", 123, "connection123")
    assert len(data) == 122
    assert struct.unpack(">II16sIBBI64sI16sI", data) == (
        0x28,
        16,
        b"SERIAL123".ljust(16, b"\0"),
        123,
        1,
        8,
        64,
        bytes(64),
        16,
        b"connection123".ljust(16, b"\0"),
        1,
    )


@pytest.mark.parametrize(
    "url",
    [
        "immis://127.0.0.1/live/x?client_id=1",
        "immis://10.0.0.1/live/x?client_id=1",
        "immis://evil.example/live/x?client_id=1",
        "immis://a.immedia-semi.com:80/x?client_id=1",
        "immis://a.immedia-semi.com/x?client_id=-1",
        "immis://a.immedia-semi.com/x?client_id=4294967296",
        "immis://a.immedia-semi.com/x?client_id=1&client_id=2",
        "rtsps://a.immedia-semi.com/x",
    ],
)
def test_invalid_media_destinations(url):
    with pytest.raises(BlinkError):
        destination(url)


async def test_verified_tls_auth_and_fragmented_packets(tls_server):
    headers = []
    closed = asyncio.Event()

    async def handler(reader, writer):
        try:
            headers.append(await reader.readexactly(122))
            headers.append(await reader.readexactly(9))
            payload = b"\x47" + bytes(187)
            response = HEADER.pack(6, 0, 0) + HEADER.pack(0, 1, len(payload)) + payload
            for index in range(0, len(response), 3):
                writer.write(response[index : index + 3])
                await writer.drain()
                await asyncio.sleep(0)
            await reader.read()
        finally:
            writer.close()
            closed.set()

    await tls_server(handler)
    async with ImmisStream(URL, "SERIAL123") as transport:
        packets = transport.packets()
        assert await anext(packets) == b"\x47" + bytes(187)
        await packets.aclose()
    async with asyncio.timeout(2):
        await closed.wait()
    assert len(headers[0]) == 122
    assert headers[1] == HEADER.pack(0x0A, 0, 0)


@pytest.mark.parametrize(
    "response",
    [
        HEADER.pack(0, 0, MAX_FRAME + 1),
        HEADER.pack(0, 0, 188) + b"partial",
        HEADER.pack(0, 0, 1) + b"\x47",  # Media before authentication.
    ],
)
async def test_invalid_or_truncated_packets_close_transport(tls_server, response):
    async def handler(reader, writer):
        with contextlib.suppress(ConnectionError):
            await reader.readexactly(131)
            writer.write(response)
            await writer.drain()
        writer.close()

    await tls_server(handler)
    with pytest.raises(BlinkError):
        async with ImmisStream(URL, "SERIAL123") as transport:
            await anext(transport.packets())


async def test_untrusted_tls_certificate_refused(tls_server):
    async def handler(reader, writer):
        writer.close()

    await tls_server(handler, trusted=False)
    with pytest.raises(BlinkError, match="TLS"):
        async with ImmisStream(URL, "SERIAL123"):
            pytest.fail("An untrusted TLS server must not be accepted")


@pytest.fixture
def wildcard_certificate(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={IPV4_TLS_IDENTITY}",
            "-addext",
            f"subjectAltName=DNS:{IPV4_TLS_IDENTITY}",
        ],
        check=True,
        capture_output=True,
    )
    return key, cert


async def test_public_ipv4_uses_vendor_identity_with_verified_tls(tls_server, wildcard_certificate):
    closed = asyncio.Event()

    async def handler(reader, writer):
        try:
            await reader.readexactly(131)
            writer.write(HEADER.pack(6, 0, 0) + HEADER.pack(0, 1, 188) + b"\x47" + bytes(187))
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            closed.set()

    await tls_server(handler, identity=IPV4_TLS_IDENTITY, certificate_override=wildcard_certificate)
    async with ImmisStream("immis://1.1.1.1/live/session?client_id=1", "SERIAL123") as stream:
        assert stream.servername == IPV4_TLS_IDENTITY
        packets = stream.packets()
        assert await anext(packets) == b"\x47" + bytes(187)
        await packets.aclose()
    async with asyncio.timeout(2):
        await closed.wait()


async def test_public_ipv4_refuses_wrong_vendor_identity(tls_server):
    async def handler(reader, writer):
        writer.close()

    # The trusted test cert identifies a specific vendor host, not the fixed wildcard identity.
    await tls_server(handler, identity=IPV4_TLS_IDENTITY)
    with pytest.raises(BlinkError, match="certificate_identity"):
        async with ImmisStream("immis://1.1.1.1/live/session?client_id=1", "SERIAL123"):
            pytest.fail("Wrong identity must not be accepted")


async def test_public_ipv4_refuses_untrusted_vendor_wildcard(tls_server, wildcard_certificate):
    async def handler(reader, writer):
        writer.close()

    await tls_server(
        handler,
        identity=IPV4_TLS_IDENTITY,
        certificate_override=wildcard_certificate,
        trusted=False,
    )
    with pytest.raises(BlinkError, match="certificate_trust"):
        async with ImmisStream("immis://1.1.1.1/live/session?client_id=1", "SERIAL123"):
            pytest.fail("Matching wildcard alone does not establish certificate trust")


def test_default_tls_verification_is_not_relaxed():
    context = tls_context()
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.verify_flags & ssl.VERIFY_X509_STRICT


def test_network_error_diagnostics_do_not_echo_endpoints():
    assert connection_failure(ConnectionRefusedError("secret URL")) == "connection_refused"
    assert connection_failure(TimeoutError("secret URL")) == "connection_or_handshake_timeout"
    assert connection_failure(OSError("secret URL")) == "connection_error"

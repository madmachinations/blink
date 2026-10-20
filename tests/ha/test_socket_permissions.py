"""The gateway tests allow local HTTP without opening access to remote services."""

import socket

import pytest

pytest.importorskip("homeassistant")

import aiohttp
import pytest_socket
from aiohttp import web


async def test_gateway_socket_permissions_are_loopback_only(loopback_sockets, server):
    async def handler(request):
        return web.Response(text="local gateway")

    url = await server(handler)
    async with aiohttp.ClientSession() as client, client.get(url) as response:
        assert await response.text() == "local gateway"

    with (
        socket.socket() as remote,
        pytest.warns(UserWarning, match=r"socket\.socket\.connect\(\)"),
        pytest.raises(pytest_socket.SocketConnectBlockedError),
    ):
        remote.connect(("192.0.2.1", 443))

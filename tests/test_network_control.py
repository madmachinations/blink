import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from blink_live_client import BlinkClient, BlinkError, Network
from blink_live_client.errors import ApiError


@pytest.mark.parametrize("armed", [True, False])
@pytest.mark.parametrize("command_id", [None, 9])
async def test_system_command_and_delayed_readback(armed, command_id, monkeypatch):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.networks = {2: Network(2, "Home", not armed)}
        reads = 0

        async def request(method, path):
            nonlocal reads
            if method == "POST":
                return {"id": command_id} if command_id else {}
            reads += 1
            return {
                "networks": [
                    {"id": 2, "name": "Home", "armed": armed if reads > 1 else not armed},
                    {"id": 7, "name": "Garage", "armed": False},
                ],
                "cameras": [{"id": 3, "network_id": 2, "name": "Front", "enabled": False}],
            }

        client.request = AsyncMock(side_effect=request)
        client.wait_command = AsyncMock()
        monkeypatch.setattr("blink_live_client.client.asyncio.sleep", AsyncMock())
        await client.set_network_armed(2, armed)
        posts = [call.args for call in client.request.call_args_list if call.args[0] == "POST"]
        action = "arm" if armed else "disarm"
        assert posts == [("POST", f"/api/v1/accounts/1/networks/2/state/{action}")]
        assert client.networks[2].armed is armed
        assert client.networks[7].armed is False
        assert client.devices["2_camera_3"].network_armed is armed
        assert client.devices["2_camera_3"].motion_enabled is False
        if command_id:
            client.wait_command.assert_awaited_once_with(2, command_id)
        else:
            client.wait_command.assert_not_awaited()


@pytest.mark.parametrize("matches", [True, False])
async def test_system_conflict_requires_confirmed_readback(matches):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.networks = {2: Network(2, "Home", False)}
        client.request = AsyncMock(side_effect=ApiError(409))

        async def discover():
            client.networks[2] = Network(2, "Home", matches)

        client.discover = discover
        if matches:
            await client.set_network_armed(2, True)
        else:
            with pytest.raises(ApiError):
                await client.set_network_armed(2, True)
        client.request.assert_awaited_once()


@pytest.mark.parametrize("removed", [True, False])
async def test_unknown_or_disappeared_system_is_not_confirmed(removed, monkeypatch):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.networks = {2: Network(2, "Home", None)}
        client.request = AsyncMock(return_value={})

        async def discover():
            if removed:
                client.networks.clear()

        client.discover = discover
        monkeypatch.setattr("blink_live_client.client.asyncio.sleep", AsyncMock())
        with pytest.raises(BlinkError, match="did not confirm"):
            await client.set_network_armed(2, True)
        client.request.assert_awaited_once()


async def test_missing_system_cannot_send_command():
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.request = AsyncMock()
        with pytest.raises(BlinkError, match="no longer available"):
            await client.set_network_armed(2, True)
        client.request.assert_not_awaited()


async def test_system_commands_are_serialized_and_cancel_releases_lock():
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.networks = {2: Network(2, "Home", False), 7: Network(7, "Garage", True)}
        started, release = asyncio.Event(), asyncio.Event()
        posts = []

        async def request(method, path):
            posts.append(path)
            started.set()
            await release.wait()
            return {}

        async def discover():
            client.networks[7] = replace(client.networks[7], armed=False)

        client.request = request
        client.discover = discover
        first = asyncio.create_task(client.set_network_armed(2, True))
        await started.wait()
        second = asyncio.create_task(client.set_network_armed(7, False))
        await asyncio.sleep(0)
        assert len(posts) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        await second
        assert len(posts) == 2
        assert posts[-1].endswith("/networks/7/state/disarm")

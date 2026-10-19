import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from blink_live_client import BlinkClient, BlinkError
from blink_live_client.errors import ApiError


@pytest.mark.parametrize("kind", ["camera", "doorbell", "owl"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_camera_motion_requests_wait_and_verify(kind, enabled, device, monkeypatch):
    device = replace(device, kind=kind, motion_enabled=not enabled)
    reads = 0
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}

        async def request(method, path, body=None):
            nonlocal reads
            if method == "POST":
                return {"command_id": 9}
            reads += 1
            group = {"camera": "cameras", "doorbell": "doorbells", "owl": "owls"}[kind]
            return {
                "networks": [{"id": 2, "armed": False}],
                group: [
                    {
                        "id": 3,
                        "network_id": 2,
                        "name": "Front",
                        "enabled": (enabled if reads > 1 else not enabled),
                        "status": "disabled" if not enabled else "online",
                    }
                ],
            }

        client.request = AsyncMock(side_effect=request)
        client.wait_command = AsyncMock()
        monkeypatch.setattr("blink_live_client.client.asyncio.sleep", AsyncMock())
        await client.set_motion_enabled(device, enabled)
        posts = [call.args for call in client.request.call_args_list if call.args[0] == "POST"]
        action = "enable" if enabled else "disable"
        expected = {
            "camera": (f"/accounts/1/networks/2/cameras/3/{action}", None),
            "doorbell": (f"/api/v1/accounts/1/networks/2/doorbells/3/{action}", None),
            "owl": ("/api/v1/accounts/1/networks/2/owls/3/config", {"enabled": enabled}),
        }
        assert posts == [("POST", *expected[kind])]
        client.wait_command.assert_awaited_once_with(2, 9)
        assert reads == 2
        observed = client.devices[device.key]
        assert observed.motion_enabled is enabled
        assert observed.network_armed is False  # Enabling a camera never arms the network.
        assert observed.online


@pytest.mark.parametrize("matches", [True, False])
async def test_motion_conflict_is_only_success_with_matching_readback(device, matches):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(side_effect=ApiError(409))

        async def discover():
            client.devices[device.key] = replace(device, motion_enabled=matches)

        client.discover = discover
        if matches:
            await client.set_motion_enabled(device, True)
        else:
            with pytest.raises(ApiError):
                await client.set_motion_enabled(device, True)
        client.request.assert_awaited_once()


async def test_unconfirmed_setting_fails_without_optimism_or_replaying_post(device, monkeypatch):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}  # Unknown enabled state.
        client.request = AsyncMock(return_value={})
        client.discover = AsyncMock()
        monkeypatch.setattr("blink_live_client.client.asyncio.sleep", AsyncMock())
        with pytest.raises(BlinkError, match="did not confirm"):
            await client.set_motion_enabled(device, True)
        assert client.devices[device.key].motion_enabled is None
        assert client.discover.await_count == 4
        client.request.assert_awaited_once()


async def test_cancelled_motion_command_releases_serialization_lock(device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        started, release = asyncio.Event(), asyncio.Event()
        posts = []

        async def request(method, path, body):
            posts.append(path)
            started.set()
            await release.wait()
            return {}

        async def discover():
            client.devices[device.key] = replace(device, motion_enabled=False)

        client.request = request
        client.discover = discover
        first = asyncio.create_task(client.set_motion_enabled(device, True))
        await started.wait()
        second = asyncio.create_task(client.set_motion_enabled(device, False))
        await asyncio.sleep(0)
        assert len(posts) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        await second
        assert len(posts) == 2
        assert posts[-1].endswith("/disable")


async def test_offline_camera_cannot_send_motion_command(device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: replace(device, online=False)}
        client.request = AsyncMock()
        with pytest.raises(BlinkError, match="unavailable"):
            await client.set_motion_enabled(device, True)
        client.request.assert_not_awaited()

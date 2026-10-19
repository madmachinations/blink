from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from blink_live_client import BlinkClient, BlinkError
from blink_live_client.errors import ApiError


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("/old/thumbnail", "/old/thumbnail.jpg"),
        ("/old/thumbnail.jpg", "/old/thumbnail.jpg"),
        ("/new/thumbnail.jpg?ts=123&ext=", "/new/thumbnail.jpg?ts=123&ext="),
        ("https://rest-prod.immedia-semi.com/old/thumb", "/old/thumb.jpg"),
        ("123", "/api/v3/media/accounts/1/networks/2/lotus/3/thumbnail/thumbnail.jpg?ts=123&ext="),
        (123, "/api/v3/media/accounts/1/networks/2/lotus/3/thumbnail/thumbnail.jpg?ts=123&ext="),
        (None, None),
        (0, None),
    ],
)
async def test_thumbnail_reference_formats(reference, expected, device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        result = client.thumbnail_path(replace(device, thumbnail=reference))
        assert result == (client.root + expected if expected else None)


@pytest.mark.parametrize(
    "reference,model",
    [
        ("https://foreign.example/thumb", "lotus"),
        (123, "../lotus"),
        (True, "lotus"),
    ],
)
async def test_bad_thumbnail_references_are_rejected(reference, model, device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        with pytest.raises(BlinkError):
            client.thumbnail_path(replace(device, thumbnail=reference, model=model))


@pytest.mark.parametrize("kind", ["camera", "doorbell", "owl"])
@pytest.mark.parametrize("command_id", [None, 9])
async def test_snapshot_capture_waits_for_image_without_replaying_command(
    kind,
    command_id,
    device,
    monkeypatch,
):
    device = replace(device, kind=kind, thumbnail="/old/thumbnail")
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(return_value={"id": command_id} if command_id else {})
        client.wait_command = AsyncMock()
        reads = 0

        async def discover():
            nonlocal reads
            reads += 1
            # Without a command ID, wait for metadata to change before fetching.
            client.devices[device.key] = replace(
                device,
                thumbnail=("/new/thumbnail" if command_id or reads > 1 else "/old/thumbnail"),
            )

        client.discover = discover
        client.resource = AsyncMock(side_effect=[ApiError(404), b"\xff\xd8new-image"])
        monkeypatch.setattr("blink_live_client.client.asyncio.sleep", AsyncMock())
        assert await client.capture_snapshot(device) == b"\xff\xd8new-image"
        prefix = "" if kind == "camera" else "/api/v1"
        group = {"camera": "cameras", "doorbell": "doorbells", "owl": "owls"}[kind]
        client.request.assert_awaited_once_with(
            "POST", f"{prefix}/accounts/1/networks/2/{group}/3/thumbnail"
        )
        if command_id:
            client.wait_command.assert_awaited_once_with(2, command_id)
        else:
            client.wait_command.assert_not_awaited()
        assert client.resource.await_count == 2
        client.resource.assert_awaited_with(client.root + "/new/thumbnail.jpg", 4 * 1024 * 1024)


async def test_conflict_does_not_return_an_existing_thumbnail_as_new(device):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(side_effect=ApiError(409))
        client.resource = AsyncMock()
        with pytest.raises(ApiError):
            await client.capture_snapshot(device)
        client.request.assert_awaited_once()
        client.resource.assert_not_awaited()


@pytest.mark.parametrize("command_id", [None, 9])
async def test_old_unconfirmed_or_invalid_image_is_not_success(command_id, device, monkeypatch):
    device = replace(device, thumbnail="/old/thumbnail")
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(return_value={"id": command_id} if command_id else {})
        client.wait_command = AsyncMock()
        client.discover = AsyncMock()
        client.resource = AsyncMock(return_value=b"not an image")
        monkeypatch.setattr("blink_live_client.client.asyncio.sleep", AsyncMock())
        with pytest.raises(BlinkError, match="completed snapshot"):
            await client.capture_snapshot(device)
        assert client.discover.await_count == 6
        if command_id is None:
            client.resource.assert_not_awaited()


async def test_completed_command_can_refresh_a_stable_image_url(device):
    device = replace(device, thumbnail="/stable/thumbnail.jpg")
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "tier": "prod"})
        client.devices = {device.key: device}
        client.request = AsyncMock(return_value={"id": 9})
        client.wait_command = AsyncMock()
        client.discover = AsyncMock()
        client.resource = AsyncMock(return_value=b"\xff\xd8fresh")
        assert await client.capture_snapshot(device) == b"\xff\xd8fresh"
        client.resource.assert_awaited_once()

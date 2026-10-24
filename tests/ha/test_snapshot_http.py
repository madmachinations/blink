"""Snapshot URL persistence, camera-scoped access and cache-only HTTP delivery."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest

pytest.importorskip("homeassistant")

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_live.camera import BlinkLiveCamera
from custom_components.blink_live.const import DOMAIN
from custom_components.blink_live.snapshot import async_setup_snapshots, snapshot_url


def runtime(hass, entry_id, devices):
    return SimpleNamespace(
        hass=hass,
        entry=SimpleNamespace(entry_id=entry_id),
        client=SimpleNamespace(devices={d.key: d for d in devices}),
        snapshots={},
    )


async def test_snapshot_url_survives_reload_and_uses_configured_address(hass, device):
    assert await async_setup_component(hass, "http", {})
    first = runtime(hass, "first", [device])
    await async_setup_snapshots(hass, first)
    with patch(
        "custom_components.blink_live.snapshot.get_url", return_value="https://ha.example:8124"
    ):
        original = snapshot_url(first, device.key)
        assert original.startswith("https://ha.example:8124/api/blink_live/snapshot/first/")
        # A fresh runtime reloads the saved credential rather than generating one.
        second = runtime(hass, "first", [device])
        await async_setup_snapshots(hass, second)
        assert snapshot_url(second, device.key) == original
        other = runtime(hass, "second", [device])
        await async_setup_snapshots(hass, other)
        assert snapshot_url(other, device.key).split("?token=")[1] != original.split("?token=")[1]


async def test_snapshot_http_only_reads_latest_cache(hass, hass_client_no_auth, device):
    assert await async_setup_component(hass, "http", {})
    another = replace(device, id=device.id + 1)
    coordinator = runtime(hass, "entry", [device, another])
    await async_setup_snapshots(hass, coordinator)
    hass.data[DOMAIN] = {"entry": coordinator}
    url = urlsplit(snapshot_url(coordinator, device.key))
    path = f"{url.path}?{url.query}"
    http = await hass_client_no_auth()

    # No fallback capture when there is no cached thumbnail.
    assert (await http.get(path)).status == 404
    coordinator.snapshots[device.key] = b"\xff\xd8cached JPEG"
    for _ in range(3):
        response = await http.get(path)
        assert response.status == 200
        assert response.content_type == "image/jpeg"
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.read() == coordinator.snapshots[device.key]
    coordinator.snapshots[device.key] = b"\xff\xd8updated JPEG"
    assert await (await http.get(path)).read() == b"\xff\xd8updated JPEG"

    assert (await http.get(url.path)).status == 401
    assert (await http.get(url.path + "?token=wrong")).status == 401
    assert (await http.get(url.path + "?token=%C3%A9")).status == 401
    # Copying one camera's token to another camera must not grant access.
    other_path = url.path.rsplit("/", 1)[0] + "/" + another.key
    assert (await http.get(f"{other_path}?{url.query}")).status == 401
    assert (await http.get(url.path + "missing?" + url.query)).status == 404
    coordinator.client.devices.pop(device.key)
    assert (await http.get(path)).status == 404
    hass.data[DOMAIN].clear()
    assert (await http.get(path)).status == 404
    # The runtime has no Blink resource/capture or gateway methods: any attempt
    # to fetch an image or open a stream would fail this test.


async def test_camera_exposes_snapshot_url_and_falls_back_to_placeholder(hass, device):
    from homeassistant.helpers.network import NoURLAvailableError

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.entry = entry
    coordinator.client.devices = {device.key: device}
    coordinator.client.account_id = 1
    coordinator.snapshot_url_secret = "test-secret"
    camera = BlinkLiveCamera(coordinator, device)
    with patch("custom_components.blink_live.snapshot.get_url", side_effect=NoURLAvailableError):
        url = camera.extra_state_attributes["snapshot_url"]
        assert url.startswith("http://HA-HOST:8123/api/blink_live/snapshot/")
        camera.entity_id = "camera.renamed"
        assert camera.extra_state_attributes["snapshot_url"] == url

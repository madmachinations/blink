"""Tests against Home Assistant 2026.10 (skipped by the lightweight client environment)."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant import config_entries
from homeassistant.core import Context, State, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_live import async_setup, async_setup_entry, async_unload_entry
from custom_components.blink_live.blink_live_client import AuthError, VerificationRequired
from custom_components.blink_live.camera import BlinkLiveCamera
from custom_components.blink_live.const import DOMAIN
from custom_components.blink_live.event import BlinkDoorbellPress, BlinkRecordingEvent


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def client(device, module):
    client = MagicMock()
    client.account_id = 1
    client.devices = {device.key: device}
    client.modules = {module.id: module}
    client.auth.state = {
        "account_id": 1,
        "client_id": 9,
        "hardware_id": "separate-identity",
        "access_token": "test",
        "refresh_token": "refresh",
        "tier": "prod",
    }
    client.auth.begin_login.return_value = "https://api.oauth.blink.com/test-only"
    client.auth.complete_login = AsyncMock()
    client.initialize = AsyncMock()
    client.send_pin = AsyncMock()
    client.verify_pin = AsyncMock()
    return client


async def test_config_flow_and_separate_domain(hass, client):
    with (
        patch("custom_components.blink_live.config_flow.BlinkClient", return_value=client),
        patch(
            "custom_components.blink_live.async_setup_entry",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"callback_url": "https://callback.test"},
        )
        await hass.async_block_till_done()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["result"].domain == "blink_live"
        assert result["result"].unique_id == "1"
        assert result["data"]["hardware_id"] == "separate-identity"


async def test_additional_pin_flow(hass, client):
    client.initialize.side_effect = [VerificationRequired("client"), None]
    with (
        patch("custom_components.blink_live.config_flow.BlinkClient", return_value=client),
        patch(
            "custom_components.blink_live.async_setup_entry",
            return_value=True,
        ),
    ):
        start = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            start["flow_id"],
            {"callback_url": "callback"},
        )
        assert result["step_id"] == "verification"
        client.send_pin.assert_awaited_once_with("client")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "123456"}
        )
        await hass.async_block_till_done()
        assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_invalid_auth_regenerates_login(hass, client):
    client.auth.complete_login.side_effect = AuthError("rejected")
    with patch("custom_components.blink_live.config_flow.BlinkClient", return_value=client):
        start = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            start["flow_id"],
            {"callback_url": "bad"},
        )
        assert result["errors"] == {"base": "invalid_auth"}
        assert client.auth.begin_login.call_count == 2


async def test_reauth_preserves_account_and_identity(hass, client):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", data=dict(client.auth.state))
    entry.add_to_hass(hass)
    with (
        patch("custom_components.blink_live.config_flow.BlinkClient", return_value=client),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"callback_url": "callback"},
        )
        await hass.async_block_till_done()
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data["hardware_id"] == "separate-identity"


async def test_entity_identity_cached_images_and_no_liveview(hass, client, device):
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.client = client
    coordinator.last_update_success = True
    coordinator.snapshots = {device.key: b"cached JPEG"}
    coordinator.gateway.url.return_value = "http://127.0.0.1:1234/private"
    camera = BlinkLiveCamera(coordinator, device)
    assert await camera.async_camera_image() == b"cached JPEG"
    coordinator.gateway.url.assert_not_called()
    assert await camera.stream_source() == "http://127.0.0.1:1234/private"
    assert camera.device_info["identifiers"] == {("blink_live", "1_2_doorbell_3")}
    assert camera.available
    press = BlinkDoorbellPress(coordinator, device)
    recording = BlinkRecordingEvent(coordinator, device)
    assert press.device_info == camera.device_info
    assert press.event_types == ["pressed", "ring"]
    assert press.available
    assert recording.event_types == ["clip_available", "clip_saved"]


async def test_coexistence_registry_namespaces(hass):
    registry = dr.async_get(hass)
    official = MockConfigEntry(domain="blink", unique_id="official", data={})
    live = MockConfigEntry(domain=DOMAIN, unique_id="1", data={})
    official.add_to_hass(hass)
    live.add_to_hass(hass)
    first = registry.async_get_or_create(
        config_entry_id=official.entry_id,
        identifiers={("blink", "ABC123")},
        name="Front Door",
    )
    second = registry.async_get_or_create(
        config_entry_id=live.entry_id,
        identifiers={(DOMAIN, "1_2_doorbell_3")},
        name="Blink Live Front Door",
    )
    assert first.id != second.id


async def test_setup_and_unload_release_gateway(hass, client):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", data=dict(client.auth.state))
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.initialize = AsyncMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.close = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    await async_setup(hass, {})
    with (
        patch("custom_components.blink_live.BlinkLiveCoordinator", return_value=coordinator),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=None,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_unload_platforms", return_value=True
        ),
    ):
        assert await async_setup_entry(hass, entry)
        assert entry.runtime_data is coordinator
        assert await async_unload_entry(hass, entry)
        coordinator.close.assert_awaited_once()
        coordinator.async_shutdown.assert_awaited_once()


async def test_setup_auth_failure_releases_gateway(hass, client):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", data=dict(client.auth.state))
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.initialize = AsyncMock(side_effect=ConfigEntryAuthFailed("expired"))
    coordinator.close = AsyncMock()
    with patch("custom_components.blink_live.BlinkLiveCoordinator", return_value=coordinator):
        with pytest.raises(ConfigEntryAuthFailed):
            await async_setup_entry(hass, entry)
        coordinator.close.assert_awaited_once()


async def test_homekit_doorbell_event_mapping_is_press_only():
    from homeassistant.components.homekit.doorbell import HomeDoorbellAccessory

    fake = MagicMock()
    fake.doorbell_is_event = True
    entity_id = "event.blink_live_front_door_press"
    update = HomeDoorbellAccessory.async_update_doorbell_state
    initial = State(entity_id, "unknown")
    unavailable = State(entity_id, "unavailable")
    first = State(entity_id, "2026-10-19T12:00:00+00:00")
    second = State(entity_id, "2026-10-19T12:00:01+00:00")
    update(fake, None, initial)
    update(fake, initial, unavailable)
    update(fake, unavailable, first)  # Reconnection/restoration must not ring.
    fake._char_doorbell_detected.set_value.assert_not_called()
    update(fake, first, second)
    fake._char_doorbell_detected.set_value.assert_called_once_with(0)


async def test_archive_media_source_authentication_and_path_validation(hass, clip, tmp_path):
    from homeassistant.components.media_source import MediaSourceItem, Unresolvable

    from custom_components.blink_live.blink_live_client.recordings import fingerprint
    from custom_components.blink_live.media_source import BlinkArchiveSource, BlinkArchiveView

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4 fixture")
    coordinator = MagicMock()
    coordinator.recordings.state = {
        "archive": {
            clip.key: {
                "path": str(path),
                "fingerprint": fingerprint(path),
                "clip": clip.to_dict(),
            }
        }
    }
    hass.data[DOMAIN] = {"entry": coordinator}
    source = BlinkArchiveSource(hass)
    item = MediaSourceItem(hass, DOMAIN, f"entry/{clip.key}", None)
    resolved = await source.async_resolve_media(item)
    assert resolved.path == path
    assert BlinkArchiveView.requires_auth
    path.write_bytes(b"replaced")
    with pytest.raises(Unresolvable):
        await source.async_resolve_media(item)


@pytest.mark.enable_socket
@pytest.mark.parametrize("recording_source", ["usb", "cloud", "all"])
async def test_real_entry_platforms_recording_events_and_reload(
    hass, device, module, recording_source
):
    """Run actual config-entry/platform setup, coordinator, entity registration and HTTP gateway."""
    from homeassistant.components.camera import async_get_stream_source
    from homeassistant.components.homekit.doorbell import HomeDoorbellAccessory
    from homeassistant.helpers.event import async_track_state_change_event

    from custom_components.blink_live.blink_live_client import BlinkClient

    second = replace(device, id=99, name="Other Door")
    camera = replace(device, id=100, kind="camera", name="Ordinary Camera")

    async def initialize(self):
        self.devices = {item.key: item for item in (device, second, camera)}
        self.modules = {module.id: module}

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1",
        title="Blink Live",
        options={"recording_source": recording_source},
        data={
            "account_id": 1,
            "hardware_id": "test",
            "access_token": "test",
            "refresh_token": "test",
            "expires_at": 9999999999,
            "tier": "prod",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch.object(BlinkClient, "initialize", initialize),
        patch.object(
            BlinkClient,
            "discover",
            AsyncMock(),
        ),
        patch.object(BlinkClient, "clips", AsyncMock(return_value=[])),
        patch.object(BlinkClient, "cloud_clips", AsyncMock(return_value=[])) as cloud_clips,
        patch(
            "homeassistant.components.ffmpeg.FFmpegManager.async_get_version",
            return_value="7.1",
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.recordings.recording_source == recording_source
        assert cloud_clips.await_count == (0 if recording_source == "usb" else 1)
        registry = er.async_get(hass)
        camera_entry = registry.async_get_entity_id("camera", DOMAIN, "1_2_doorbell_3_camera")
        press_entry = registry.async_get_entity_id("event", DOMAIN, "1_2_doorbell_3_press")
        recording_entry = registry.async_get_entity_id("event", DOMAIN, "1_2_doorbell_3_recording")
        button_entry = registry.async_get_entity_id(
            "button", DOMAIN, "1_2_doorbell_3_ring_doorbell"
        )
        assert camera_entry and press_entry and recording_entry
        assert button_entry
        second_press = registry.async_get_entity_id("event", DOMAIN, "1_2_doorbell_99_press")
        assert second_press
        assert (
            registry.async_get_entity_id("button", DOMAIN, "1_2_camera_100_ring_doorbell") is None
        )
        assert hass.states.get(press_entry).state == "unknown"
        assert hass.states.get(recording_entry).state == "unknown"
        assert (await async_get_stream_source(hass, camera_entry)).startswith("http://127.0.0.1:")
        assert entry.runtime_data.gateway._sessions == {}
        await entry.runtime_data._notify(
            "clip_available", {"camera_key": device.key, "clip_id": "1"}
        )
        assert hass.states.get(recording_entry).attributes["event_type"] == "clip_available"
        assert hass.states.get(press_entry).state == "unknown"
        events = []
        homekit = MagicMock()
        homekit.doorbell_is_event = True

        @callback
        def forward(event):
            if event.data["new_state"] is not None:
                HomeDoorbellAccessory.async_update_doorbell_state(
                    homekit,
                    event.data["old_state"],
                    event.data["new_state"],
                )

        @callback
        def collect(event):
            events.append(event.data)

        unlisten = async_track_state_change_event(hass, press_entry, forward)
        unlisten_bus = hass.bus.async_listen("blink_live_doorbell_pressed", collect)
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": button_entry},
            blocking=True,
        )
        first_press = hass.states.get(press_entry)
        assert first_press.attributes["event_type"] == "ring"
        assert first_press.attributes["source"] == "manual"
        assert hass.states.get(second_press).state == "unknown"
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": button_entry},
            blocking=True,
        )
        assert hass.states.get(press_entry).state > first_press.state
        await hass.async_block_till_done()
        assert homekit._char_doorbell_detected.set_value.call_count == 2
        assert len(events) == 2
        assert all(item["camera_key"] == device.key for item in events)
        # Future direct push input uses the exact same reporting path (synthetic test only).
        entry.runtime_data.async_report_doorbell_press(device.key, source="blink_push")
        await hass.async_block_till_done()
        assert hass.states.get(press_entry).attributes["source"] == "blink_push"
        assert homekit._char_doorbell_detected.set_value.call_count == 3
        # Verify Alexa's real SceneController activation handler reaches this button.
        from homeassistant.components.alexa.handlers import async_api_activate

        directive = MagicMock()
        directive.entity = hass.states.get(button_entry)
        await async_api_activate(hass, MagicMock(), directive, Context())
        await hass.async_block_till_done()
        assert homekit._char_doorbell_detected.set_value.call_count == 4
        assert hass.states.get(press_entry).attributes["source"] == "manual"
        entry.runtime_data.last_update_success = False
        entry.runtime_data.client.devices[device.key] = replace(device, online=False)
        entry.runtime_data.async_update_listeners()
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": button_entry},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert homekit._char_doorbell_detected.set_value.call_count == 5
        with pytest.raises(HomeAssistantError, match="doorbell is no longer available"):
            entry.runtime_data.async_report_doorbell_press(camera.key)
        with pytest.raises(HomeAssistantError, match="Unknown Blink Live doorbell event source"):
            entry.runtime_data.async_report_doorbell_press(device.key, source="unverified")
        old_gateway = entry.runtime_data.gateway
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert old_gateway._runner is None
        assert entry.runtime_data.gateway is not old_gateway
        assert hass.states.get(press_entry).state == "unknown"
        assert homekit._char_doorbell_detected.set_value.call_count == 5
        unlisten()
        unlisten_bus()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_options_require_allowed_writable_directory(hass, client, tmp_path):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", data=dict(client.auth.state))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    options = {
        "poll_interval": 60,
        "stream_duration": 60,
        "automatic_downloads": True,
        "archive_directory": str(tmp_path),
        "retention_days": 30,
        "recording_source": "all",
        "motion_timeout": 45,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["errors"] == {"archive_directory": "invalid_directory"}
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["automatic_downloads"] is True
    assert entry.options["recording_source"] == "all"
    assert entry.options["motion_timeout"] == 45


async def test_download_service_checks_camera_and_path(hass, device, clip, tmp_path):
    from homeassistant.exceptions import ServiceValidationError

    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", data={})
    entry.add_to_hass(hass)
    await async_setup(hass, {})
    coordinator = MagicMock()
    coordinator.options = {"archive_directory": str(tmp_path)}
    coordinator.client.account_id = 1
    coordinator.recordings.clips = {clip.key: clip}
    coordinator.recordings.download = AsyncMock(return_value=tmp_path / "clip.mp4")
    hass.data[DOMAIN][entry.entry_id] = coordinator
    entity = er.async_get(hass).async_get_or_create(
        "camera",
        DOMAIN,
        f"1_{device.key}_camera",
        config_entry=entry,
    )
    data = {"entity_id": entity.entity_id, "clip_id": clip.key}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "download_clip", data, blocking=True)
    coordinator.recordings.download.assert_not_awaited()
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    result = await hass.services.async_call(
        DOMAIN,
        "download_clip",
        data,
        blocking=True,
        return_response=True,
    )
    assert result == {"path": str(tmp_path / "clip.mp4"), "clip_id": clip.key}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "download_clip",
            {**data, "clip_id": "another_camera"},
            blocking=True,
        )


@pytest.mark.parametrize("authenticated", [False, True])
async def test_archive_http_requires_auth(
    hass,
    hass_client,
    hass_client_no_auth,
    authenticated,
    clip,
    tmp_path,
):
    from homeassistant.setup import async_setup_component

    from custom_components.blink_live.blink_live_client.recordings import fingerprint
    from custom_components.blink_live.media_source import BlinkArchiveView

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom")
    coordinator = MagicMock()
    coordinator.recordings.state = {
        "archive": {
            clip.key: {
                "path": str(path),
                "fingerprint": fingerprint(path),
                "clip": clip.to_dict(),
            }
        }
    }
    hass.data[DOMAIN] = {"entry": coordinator}
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(BlinkArchiveView())
    factory = hass_client if authenticated else hass_client_no_auth
    http = await factory()
    response = await http.get(f"/api/blink_live/archive/entry/{clip.key}")
    assert response.status == (200 if authenticated else 401)
    if authenticated:
        assert await response.read() == path.read_bytes()

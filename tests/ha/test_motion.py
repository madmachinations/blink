"""Recording-derived motion, opt-in ringing and the future receiver boundary."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.blink_live.blink_live_client import BlinkClient, BlinkError, Network
from custom_components.blink_live.const import DOMAIN


async def test_http_reports_follow_common_event_paths_and_clear_without_replay(
    hass, motion_setup, device, freezer
):
    entry, _, entity, _, _ = motion_setup
    coordinator = entry.runtime_data
    emit = coordinator.http_events.emit = MagicMock()
    coordinator.async_report_motion(device.key, source="recording", event_id="fresh")
    coordinator.async_report_motion(device.key, source="recording", event_id="fresh")
    assert emit.call_count == 1
    assert emit.call_args.args[0] == "motion_detected"
    await hass.services.async_call(
        "button", "press", {"entity_id": entity("button", device, "ring_doorbell")}, blocking=True
    )
    first = emit.call_args.args[1]
    assert emit.call_args.args[0] == "doorbell_pressed" and first["source"] == "manual"
    coordinator.async_report_doorbell_press(device.key)
    assert emit.call_args.args[1]["event_id"] != first["event_id"]
    coordinator.ring_on_motion[device.key] = True
    coordinator.async_report_motion(device.key, source="recording", event_id="next")
    assert [call.args[0] for call in emit.call_args_list[-2:]] == [
        "motion_detected",
        "doorbell_pressed",
    ]
    assert emit.call_args.args[1]["source"] == "motion"
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert emit.call_args.args[0] == "motion_cleared"
    assert device.key not in coordinator.motion_active
    emit.reset_mock()
    coordinator.motion_ready = False
    coordinator.async_report_motion(device.key, source="recording", event_id="baseline")
    emit.assert_not_called()
    await coordinator.close()
    emit.assert_not_called()


async def test_optional_rtsp_lifecycle_and_attribute(hass, motion_setup, device):
    entry, _, entity, _, _ = motion_setup
    coordinator = entry.runtime_data
    assert coordinator.rtsp is None
    assert "rtsp_url" not in hass.states.get(entity("camera", device, "camera")).attributes
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "rtsp_enabled": True,
            "rtsp_host": "127.0.0.1",
            "rtsp_port": 0,
            "rtsp_username": "viewer",
            "rtsp_password": "example-test-password",
        },
    )
    await hass.async_block_till_done()
    server = entry.runtime_data.rtsp
    assert server._server is not None
    assert not server._sessions and not entry.runtime_data.gateway._sessions
    url = hass.states.get(entity("camera", device, "camera")).attributes["rtsp_url"]
    assert device.key in url and "password" not in url
    hass.config_entries.async_update_entry(entry, options={**entry.options, "rtsp_enabled": False})
    await hass.async_block_till_done()
    assert server._server is None and entry.runtime_data.rtsp is None


@pytest.fixture
async def motion_setup(
    hass, device, module, clip, enable_custom_integrations, request, loopback_sockets
):
    other = replace(device, id=99, name="Other Door")
    camera = replace(device, id=100, kind="camera", name="Other Camera")
    source = getattr(request, "param", "usb")
    data = {"usb": [clip], "cloud": [replace(clip, source="cloud", module_id=None)]}

    async def initialize(self):
        self.devices = {d.key: d for d in (device, other, camera)}
        self.modules = {module.id: module}
        self.networks = {2: Network(2, "Home", False), 7: Network(7, "Garage", True)}

    async def usb(_self, _module):
        return data["usb"]

    async def cloud(_self, _since=None):
        return data["cloud"]

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1",
        data={"account_id": 1, "tier": "prod"},
        options={"recording_source": source, "motion_timeout": 10, "live_events": False},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(BlinkClient, "initialize", initialize),
        patch.object(BlinkClient, "discover", AsyncMock()),
        patch.object(BlinkClient, "clips", usb),
        patch.object(BlinkClient, "cloud_clips", cloud),
        patch(
            "homeassistant.components.ffmpeg.FFmpegManager.async_get_version", return_value="7.1"
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)

        def entity(domain, dev, suffix):
            return registry.async_get_entity_id(domain, DOMAIN, f"1_{dev.key}_{suffix}")

        yield entry, data, entity, other, camera
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("motion_setup", ["usb", "cloud", "all"], indirect=True)
async def test_recording_motion_routing_dedup_and_timer(hass, motion_setup, device, freezer):
    entry, data, entity, other, camera = motion_setup
    coordinator = entry.runtime_data
    motion = entity("binary_sensor", device, "motion")
    routing = entity("switch", device, "ring_on_motion")
    press = entity("event", device, "press")
    assert motion and routing
    assert hass.states.get(motion).state == "off"  # Historical baseline is not motion.
    assert hass.states.get(routing).state == "off"
    assert entity("switch", camera, "ring_on_motion") is None
    assert hass.states.get(entity("binary_sensor", camera, "motion")).state == "off"
    assert (
        er.async_get(hass).async_get(motion).device_id
        == er.async_get(hass).async_get(press).device_id
    )
    events, rings = [], []
    hass.bus.async_listen("blink_live_motion_detected", lambda event: events.append(event.data))
    hass.bus.async_listen("blink_live_doorbell_pressed", lambda event: rings.append(event.data))
    source = "cloud" if coordinator.options["recording_source"] == "cloud" else "usb"
    original = data[source][0]
    data[source].append(replace(original, id="7"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == "on"
    assert hass.states.get(motion).attributes["inferred"] is True
    assert hass.states.get(motion).attributes["occurred_at"] == original.created_at
    assert hass.states.get(press).state == "unknown"
    assert len(events) == 1 and not rings

    await hass.services.async_call("switch", "turn_on", {"entity_id": routing}, blocking=True)
    await hass.async_block_till_done()
    assert not rings  # Enabling while motion is already on must not ring.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1
    freezer.tick(timedelta(seconds=5))
    data[source].append(replace(original, id="8"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 2 and len(rings) == 1
    assert rings[0]["source"] == "motion"
    assert rings[0]["motion_source"] == "recording"
    assert rings[0]["inferred"] is True
    assert hass.states.get(press).attributes["event_type"] == "ring"
    assert hass.states.get(entity("event", other, "press")).state == "unknown"

    # Other cameras/doorbells update their own sensors without ringing this one.
    data[source].extend(
        [
            replace(original, id="9", camera_key=other.key),
            replace(original, id="10", camera_key=camera.key),
        ]
    )
    await coordinator.async_refresh()
    await coordinator._notify("clip_saved", {"camera_key": device.key, "clip_id": "saved"})
    await hass.async_block_till_done()
    assert len(events) == 4 and len(rings) == 1
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == "on"  # New recording extended the timer.
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == "off"

    await hass.services.async_call("switch", "turn_off", {"entity_id": routing}, blocking=True)
    data[source].append(replace(original, id="11"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(motion).state == "on"
    assert len(events) == 5 and len(rings) == 1


async def test_backend_can_be_replaced_without_changing_entities(hass, motion_setup, device):
    entry, _, entity, _, _ = motion_setup
    coordinator = entry.runtime_data
    coordinator.motion_backend = "blink_push"  # Future receiver selects its input here.
    coordinator.async_report_motion(device.key, source="recording", event_id="old-clip")
    assert not coordinator.motion_active
    coordinator.async_report_motion(device.key, source="blink_push", event_id="notification-1")
    await hass.async_block_till_done()
    state = hass.states.get(entity("binary_sensor", device, "motion"))
    assert state.state == "on"
    assert state.attributes["detection_source"] == "blink_push"
    assert state.attributes["inferred"] is False
    timer = coordinator._motion_timers[device.key]
    coordinator.async_report_motion(device.key, source="blink_push", event_id="notification-1")
    assert coordinator._motion_timers[device.key] is timer
    with pytest.raises(HomeAssistantError):
        coordinator.async_report_motion("missing", source="blink_push", event_id="2")
    with pytest.raises(HomeAssistantError):
        coordinator.async_report_motion(device.key, source="unknown", event_id="2")


async def test_opt_in_push_routing_status_and_reload(hass, motion_setup, device, freezer):
    import time

    from custom_components.blink_live.blink_live_client.push import PushReceiver
    from custom_components.blink_live.blink_live_client.push_proto import blob, fields, number
    from custom_components.blink_live.diagnostics import async_get_config_entry_diagnostics

    entry, data, entity, _, _ = motion_setup
    assert entry.runtime_data.push is None
    # Exercise a real options reload, suppressing only the external receiver task.
    with patch.object(PushReceiver, "start") as start:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "live_events": True}
        )
        await hass.async_block_till_done()
    start.assert_called_once()
    coordinator = entry.runtime_data
    receiver = coordinator.push
    assert receiver is not None and coordinator.motion_backend == "blink_push"
    registry = er.async_get(hass)
    connection = registry.async_get_entity_id("binary_sensor", DOMAIN, "1_push_connection")
    receiver._status("connected")
    await hass.async_block_till_done()
    assert hass.states.get(connection).state == "on"
    emit = coordinator.http_events.emit = MagicMock()
    rings, motions = [], []
    hass.bus.async_listen("blink_live_doorbell_pressed", lambda event: rings.append(event.data))
    hass.bus.async_listen("blink_live_motion_detected", lambda event: motions.append(event.data))

    def push(kind, identity):
        payload = {"event_type": kind, "category": "media", "camera_id": str(device.id)}
        return fields(
            blob(9, identity)
            + number(18, int(time.time() * 1000))
            + b"".join(blob(7, blob(1, k) + blob(2, v)) for k, v in payload.items())
        )

    data["usb"].append(replace(data["usb"][0], id="7"))
    await coordinator.async_refresh()
    assert not coordinator.motion_active
    await receiver._message(push("button_press", "physical-press"))
    await receiver._message(push("button_press", "physical-press"))
    await receiver._message(push("pir", "physical-motion"))
    await hass.async_block_till_done()
    assert len(rings) == len(motions) == 1
    assert rings[0]["source"] == motions[0]["source"] == "blink_push"
    assert motions[0]["inferred"] is False
    assert len(rings[0]["event_id"]) == 64
    assert hass.states.get(entity("event", device, "press")).attributes["event_type"] == "ring"
    assert hass.states.get(entity("binary_sensor", device, "motion")).state == "on"
    assert [call.args[0] for call in emit.call_args_list] == ["doorbell_pressed", "motion_detected"]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["push"]["accepted_events"] == 2
    assert "fcm_token" not in str(diagnostics) and "physical-press" not in str(diagnostics)

    # A transport outage does not silently re-enable recording inference.
    receiver._status("reconnecting")
    data["usb"].append(replace(data["usb"][0], id="8"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(connection).state == "off" and len(motions) == 1
    freezer.tick(timedelta(seconds=16))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(entity("binary_sensor", device, "motion")).state == "off"

    # The HA Store checkpoint suppresses an old press after reload.
    with patch.object(PushReceiver, "start"):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    await entry.runtime_data.push._message(push("button_press", "physical-press"))
    await hass.async_block_till_done()
    assert len(rings) == 1 and receiver.status == "disabled"
    hass.config_entries.async_update_entry(entry, options={**entry.options, "live_events": False})
    await hass.async_block_till_done()
    assert entry.runtime_data.push is None
    assert entry.runtime_data.motion_backend == "recording"
    data["usb"].append(replace(data["usb"][0], id="9"))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(motions) == 2 and motions[-1]["source"] == "recording"


async def test_live_event_options_preserve_other_settings(hass, motion_setup):
    entry, _, _, _, _ = motion_setup
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        start = await hass.config_entries.options.async_init(entry.entry_id)
        assert "live_events" in start["menu_options"]
        form = await hass.config_entries.options.async_configure(
            start["flow_id"], {"next_step_id": "live_events"}
        )
        assert form["step_id"] == "live_events"
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"live_events": False, "live_motion_timeout": 20}
        )
        assert result["data"]["live_events"] is False
        assert result["data"]["motion_timeout"] == 10
        assert result["data"]["live_motion_timeout"] == 20
        await hass.async_block_till_done()


async def test_live_events_default_on_without_overriding_explicit_opt_out(hass, motion_setup):
    from custom_components.blink_live.blink_live_client.push import PushReceiver

    entry, _, _, _, _ = motion_setup
    options = dict(entry.options)
    options.pop("live_events")
    # Entries without a saved preference, including existing installations,
    # receive the new default on their next load.
    with patch.object(PushReceiver, "start") as start:
        hass.config_entries.async_update_entry(entry, options=options)
        await hass.async_block_till_done()
    start.assert_called_once()
    assert entry.runtime_data.options["live_events"] is True
    assert entry.runtime_data.motion_backend == "blink_push"
    assert entry.runtime_data.push is not None

    with patch.object(PushReceiver, "start") as start:
        hass.config_entries.async_update_entry(entry, options={**options, "live_events": False})
        await hass.async_block_till_done()
    start.assert_not_called()
    assert entry.runtime_data.push is None
    assert entry.runtime_data.motion_backend == "recording"


@pytest.mark.parametrize("latest_kind", ["cv_motion", "cam2sm_motion"])
async def test_live_motion_renews_from_either_signal_and_ignores_unknowns_and_duplicates(
    hass, motion_setup, device, freezer, latest_kind
):
    import time

    from custom_components.blink_live.blink_live_client.push import PushReceiver
    from custom_components.blink_live.blink_live_client.push_proto import blob, fields, number

    entry, _, entity, other, _ = motion_setup
    coordinator = entry.runtime_data
    coordinator.motion_backend = "blink_push"
    emit = coordinator.http_events.emit = MagicMock()
    receiver = PushReceiver(
        coordinator.client, {}, AsyncMock(), coordinator._push_event, MagicMock()
    )

    async def advance(seconds):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    async def notify(dev, identity, *, category="motion", kind="cam2sm_motion"):
        payload = {"category": category, "event_type": kind, "camera_id": str(dev.id)}
        await receiver._message(
            fields(
                blob(9, identity)
                + number(18, int(time.time() * 1000))
                + b"".join(blob(7, blob(1, key) + blob(2, value)) for key, value in payload.items())
            )
        )
        await hass.async_block_till_done()

    def motion(dev):
        return hass.states.get(entity("binary_sensor", dev, "motion")).state

    # Defaults merged into an existing entry: no option migration is needed.
    assert coordinator.options["live_motion_timeout"] == 15
    assert coordinator.options["motion_timeout"] == 10
    if latest_kind == "cam2sm_motion":
        await notify(device, "first", category="media", kind="cv_motion")
    else:
        await notify(device, "first")
    await advance(5)
    await notify(other, "other-camera")
    await advance(5)
    await notify(device, "repeat")
    await advance(6)
    assert motion(device) == motion(other) == "on"
    await advance(4.1)
    assert motion(other) == "off" and motion(device) == "on"
    # Whichever recognized motion signal arrived last owns the expiry.
    await notify(
        device,
        "latest",
        category="media" if latest_kind == "cv_motion" else "motion",
        kind=latest_kind,
    )
    await advance(3.9)
    await notify(device, "repeat")
    assert motion(device) == "on"
    await advance(1.1)
    assert motion(device) == "on"  # The latest report extended the old deadline.
    await advance(9)
    await notify(device, "unknown-media", category="media", kind="unrecognized")
    await advance(1.1)
    assert motion(device) == "off"
    assert [call.args[0] for call in emit.call_args_list].count("motion_detected") == 4
    assert [call.args[0] for call in emit.call_args_list].count("motion_cleared") == 2


async def test_live_motion_timeout_is_from_receipt_not_provider_timestamp(
    hass, motion_setup, device, freezer
):
    entry, _, entity, _, _ = motion_setup
    coordinator = entry.runtime_data
    coordinator.motion_backend = "blink_push"
    coordinator.options["live_motion_timeout"] = 7
    # Provider timestamps describe when Blink detected motion, not our deadline.
    coordinator.async_report_motion(
        device.key,
        source="blink_push",
        event_id="delayed-push",
        occurred_at=(dt_util.utcnow() - timedelta(seconds=30)).isoformat(),
    )
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    sensor = entity("binary_sensor", device, "motion")
    assert hass.states.get(sensor).state == "on"
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "off"


async def test_push_failures_surface_repairs_without_disabling_cameras(hass, motion_setup):
    from homeassistant.helpers import issue_registry as ir

    entry, _, _, _, _ = motion_setup
    coordinator = entry.runtime_data
    coordinator._push_status("error")
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"{entry.entry_id}_live_events") is not None
    assert coordinator.last_update_success
    with patch.object(entry, "async_start_reauth") as reauth:
        coordinator._push_status("auth_failed")
        reauth.assert_called_once_with(hass)
    coordinator._push_status("connected")
    assert issues.async_get_issue(DOMAIN, f"{entry.entry_id}_live_events") is None


async def test_push_store_write_failure_blocks_dispatch(hass, motion_setup, device):
    from homeassistant.util.file import WriteError

    from custom_components.blink_live.blink_live_client.push import PushReceiver
    from custom_components.blink_live.blink_live_client.push_proto import blob, fields

    entry, _, _, _, _ = motion_setup
    coordinator = entry.runtime_data
    receiver = PushReceiver(
        coordinator.client, {}, coordinator.push_store.async_save, MagicMock(), MagicMock()
    )
    # HA's regular Store swallows WriteError. Our checkpoint must propagate it.
    with patch(
        "homeassistant.helpers.storage.Store._async_write_data",
        AsyncMock(side_effect=WriteError),
    ):
        with pytest.raises(OSError, match="checkpoint"):
            await receiver._message(fields(blob(9, "not-durable")))
    assert not receiver.state
    receiver.on_event.assert_not_called()


async def test_switch_restores_without_motion_or_rings_and_timers_are_removed(
    hass,
    motion_setup,
    device,
):
    entry, data, entity, _, _ = motion_setup
    old = entry.runtime_data
    routing = entity("switch", device, "ring_on_motion")
    await hass.services.async_call("switch", "turn_on", {"entity_id": routing}, blocking=True)
    data["usb"].append(replace(data["usb"][0], id="7"))
    await old.async_refresh()
    assert old.motion_active
    rings = []
    hass.bus.async_listen("blink_live_doorbell_pressed", lambda event: rings.append(event.data))
    # A recording discovered during reload is reconciled without generating a ring.
    data["usb"].append(replace(data["usb"][0], id="8"))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not old._motion_timers and not old.motion_active and not old.motion_ready
    assert hass.states.get(routing).state == "on"
    assert hass.states.get(entity("binary_sensor", device, "motion")).state == "off"
    assert not rings
    data["usb"].append(replace(data["usb"][0], id="9"))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(rings) == 1


async def test_camera_motion_switch_uses_observed_state_and_never_rings(hass, motion_setup, device):
    entry, _, entity, other, camera = motion_setup
    coordinator = entry.runtime_data
    control = entity("switch", device, "motion_detection")
    assert control
    for item in (device, other, camera):
        assert entity("switch", item, "motion_detection")
    assert hass.states.get(control).state == "unknown"
    rings, motions = [], []
    hass.bus.async_listen("blink_live_doorbell_pressed", lambda event: rings.append(event.data))
    hass.bus.async_listen("blink_live_motion_detected", lambda event: motions.append(event.data))

    async def set_motion(selected, enabled):
        assert selected.key == device.key
        coordinator.client.devices[selected.key] = replace(
            selected, motion_enabled=enabled, network_armed=False
        )

    coordinator.client.set_motion_enabled = AsyncMock(side_effect=set_motion)
    await hass.services.async_call("switch", "turn_on", {"entity_id": control}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(control).state == "on"
    assert hass.states.get(control).attributes["network_armed"] is False
    assert hass.states.get(entity("switch", device, "ring_on_motion")).state == "off"
    assert hass.states.get(entity("switch", other, "motion_detection")).state == "unknown"
    await hass.services.async_call("switch", "turn_off", {"entity_id": control}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(control).state == "off"
    assert not rings and not motions

    coordinator.client.set_motion_enabled.side_effect = BlinkError("unconfirmed")
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await hass.services.async_call("switch", "turn_on", {"entity_id": control}, blocking=True)
    assert hass.states.get(control).state == "off"

    coordinator.client.devices[device.key] = replace(device, motion_enabled=True)
    coordinator.async_set_updated_data(dict(coordinator.client.devices))
    assert hass.states.get(control).state == "on"
    coordinator.client.devices[device.key] = replace(device, online=False)
    coordinator.async_set_updated_data(dict(coordinator.client.devices))
    assert hass.states.get(control).state == "unavailable"


async def test_snapshot_cache_and_explicit_capture_service(hass, motion_setup, device):
    from homeassistant.components.camera import get_camera_from_entity_id
    from homeassistant.exceptions import ServiceValidationError

    entry, _, entity, _, _ = motion_setup
    coordinator = entry.runtime_data
    camera_id = entity("camera", device, "camera")
    camera = get_camera_from_entity_id(hass, camera_id)
    coordinator.client.devices[device.key] = replace(device, thumbnail=123)
    coordinator.client.resource = AsyncMock(return_value=b"\xff\xd8cached")
    coordinator.client.capture_snapshot = AsyncMock(return_value=b"\xff\xd8fresh")
    coordinator.client.liveview = AsyncMock(side_effect=AssertionError("No live session"))
    await coordinator._refresh_snapshots()
    assert await camera.async_camera_image() == b"\xff\xd8cached"
    assert await camera.async_camera_image() == b"\xff\xd8cached"
    await coordinator._refresh_snapshots()
    coordinator.client.resource.assert_awaited_once()
    coordinator.client.capture_snapshot.assert_not_awaited()
    assert coordinator.gateway._sessions == {}
    events = []
    hass.bus.async_listen("blink_live_motion_detected", lambda event: events.append(event))
    hass.bus.async_listen("blink_live_doorbell_pressed", lambda event: events.append(event))

    await hass.services.async_call(DOMAIN, "take_snapshot", {"entity_id": camera_id}, blocking=True)
    await hass.async_block_till_done()
    coordinator.client.capture_snapshot.assert_awaited_once()
    assert await camera.async_camera_image() == b"\xff\xd8fresh"
    assert hass.states.get(camera_id).attributes["snapshot_cached_at"]
    assert not events
    coordinator.client.liveview.assert_not_awaited()

    # A failed request retains the previous good image and cache timestamp.
    cached_at = coordinator.snapshot_cached_at[device.key]
    coordinator.client.capture_snapshot.side_effect = BlinkError("capture failed")
    with pytest.raises(HomeAssistantError, match="could not capture"):
        await hass.services.async_call(
            DOMAIN, "take_snapshot", {"entity_id": camera_id}, blocking=True
        )
    assert await camera.async_camera_image() == b"\xff\xd8fresh"
    assert coordinator.snapshot_cached_at[device.key] == cached_at
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "take_snapshot", {"entity_id": "camera.not_a_blink_live_camera"}, blocking=True
        )


async def test_cancelled_snapshot_preserves_cache_and_releases_lock(hass, motion_setup, device):
    entry, _, _, _, _ = motion_setup
    coordinator = entry.runtime_data
    coordinator.snapshots[device.key] = b"\xff\xd8old"
    started = asyncio.Event()

    async def capture(_device):
        started.set()
        await asyncio.Event().wait()

    coordinator.client.capture_snapshot = capture
    task = asyncio.create_task(coordinator.async_take_snapshot(device.key))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.snapshots[device.key] == b"\xff\xd8old"
    coordinator.client.capture_snapshot = AsyncMock(return_value=b"\xff\xd8new")
    await coordinator.async_take_snapshot(device.key)
    assert coordinator.snapshots[device.key] == b"\xff\xd8new"


async def test_system_arm_switches_isolate_networks_and_read_state(hass, motion_setup, device):
    entry, _, entity, _, _ = motion_setup
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    home = registry.async_get_entity_id("switch", DOMAIN, "1_network_2_armed")
    garage = registry.async_get_entity_id("switch", DOMAIN, "1_network_7_armed")
    assert home and garage
    assert hass.states.get(home).state == "off"
    assert hass.states.get(garage).state == "on"
    assert registry.async_get(home).device_id != registry.async_get(garage).device_id
    assert (
        registry.async_get(home).device_id
        != registry.async_get(entity("camera", device, "camera")).device_id
    )
    events = []
    hass.bus.async_listen("blink_live_motion_detected", lambda event: events.append(event))
    hass.bus.async_listen("blink_live_doorbell_pressed", lambda event: events.append(event))

    async def set_armed(network_id, armed):
        coordinator.client.networks[network_id] = replace(
            coordinator.client.networks[network_id], armed=armed
        )
        for key, camera in tuple(coordinator.client.devices.items()):
            if camera.network_id == network_id:
                coordinator.client.devices[key] = replace(camera, network_armed=armed)

    coordinator.client.set_network_armed = AsyncMock(side_effect=set_armed)
    await hass.services.async_call("switch", "turn_on", {"entity_id": home}, blocking=True)
    await hass.async_block_till_done()
    coordinator.client.set_network_armed.assert_awaited_once_with(2, True)
    assert hass.states.get(home).state == "on"
    assert hass.states.get(garage).state == "on"
    assert (
        hass.states.get(entity("switch", device, "motion_detection")).attributes["network_armed"]
        is True
    )
    assert not events
    await hass.services.async_call("switch", "turn_off", {"entity_id": garage}, blocking=True)
    assert hass.states.get(garage).state == "off"
    assert hass.states.get(home).state == "on"
    coordinator.client.set_network_armed.side_effect = BlinkError("unconfirmed")
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": home}, blocking=True)
    assert hass.states.get(home).state == "on"

    coordinator.client.networks[2] = Network(2, "Home", None)
    coordinator.async_set_updated_data(dict(coordinator.client.devices))
    assert hass.states.get(home).state == "unknown"
    coordinator.client.networks.pop(2)
    coordinator.async_set_updated_data(dict(coordinator.client.devices))
    assert hass.states.get(home).state == "unavailable"
    coordinator.client.networks[2] = Network(2, "Home", False)
    coordinator.async_set_updated_data(dict(coordinator.client.devices))
    assert hass.states.get(home).state == "off"
    assert not events
    coordinator.client.networks[2] = Network(2, "Home", True)
    coordinator.async_set_updated_data(dict(coordinator.client.devices))
    assert hass.states.get(home).state == "on"
    with patch.object(BlinkClient, "set_network_armed", AsyncMock()) as commands:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        commands.assert_not_awaited()
    # Reload observes Blink's state, without restoring the previous on state.
    assert hass.states.get(home).state == "off"
    assert hass.states.get(garage).state == "on"

"""Generic request rules: real loopback HTTP, templates, ordering and privacy."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_live.const import DEFAULT_OPTIONS, DOMAIN
from custom_components.blink_live.http_events import HttpEventDispatcher, validate_rule


def rule(**changes):
    return {
        "id": "test-rule",
        "name": "Example endpoint",
        "enabled": True,
        "cameras": [],
        "event_type": "doorbell_pressed",
        "method": "POST",
        "url": "http://127.0.0.1/example/{{ camera_key | urlencode }}",
        "headers": '{"Content-Type":"application/json", "X-Source":"{{ source }}"}',
        "body": '{{ {"camera":camera_name,"event":event_type,"id":event_id} | to_json }}',
        **changes,
    }


def data(device, identity="first"):
    return {
        "camera_key": device.key,
        "source": "manual",
        "event_id": identity,
        "occurred_at": "2026-10-20T12:00:00+00:00",
        "observed_at": "2026-10-20T12:00:01+00:00",
    }


async def drain(dispatcher):
    async with asyncio.timeout(5):
        while dispatcher.workers:
            await asyncio.gather(*(task for _, task in tuple(dispatcher.workers.values())))


async def test_http_templates_filtering_order_and_motion_clearing(
    hass, device, server, loopback_sockets
):
    received = []

    async def handler(request):
        received.append((request.path, request.headers["X-Source"], await request.json()))
        return web.Response()

    url = await server(handler)
    rules = [
        rule(url=url + "/{{ camera_key }}"),
        rule(
            id="motion",
            event_type="motion_detected",
            url=url + "/on",
            body='{{ {"active": motion_active} | to_json }}',
        ),
        rule(
            id="clear",
            event_type="motion_cleared",
            url=url + "/off",
            body='{{ {"active": motion_active} | to_json }}',
        ),
        rule(id="filtered", cameras=["another-camera"], url=url + "/wrong"),
    ]
    async with aiohttp.ClientSession() as session:
        dispatcher = HttpEventDispatcher(
            hass,
            SimpleNamespace(),
            SimpleNamespace(account_id=1, devices={device.key: device}),
            session,
            {**DEFAULT_OPTIONS, "http_events_enabled": True, "http_event_rules": rules},
        )
        try:
            dispatcher.emit("doorbell_pressed", data(device))
            dispatcher.emit("doorbell_pressed", data(device, "second"))
            dispatcher.emit("motion_detected", data(device))
            dispatcher.emit("motion_cleared", data(device))
            await drain(dispatcher)
            assert [path for path, _, _ in received] == [
                f"/{device.key}",
                f"/{device.key}",
                "/on",
                "/off",
            ]
            assert received[0][1] == "manual"
            assert received[0][2] == {
                "camera": "Front Door",
                "event": "doorbell_pressed",
                "id": "first",
            }
            assert received[1][2]["id"] == "second"
            assert received[2][2]["active"] is True
            assert received[3][2]["active"] is False
            assert set(dispatcher.results.values()) == {"delivered"}
        finally:
            await dispatcher.close()


async def test_disabled_invalid_requests_and_failures_do_not_leak(hass, device, caplog):
    session = SimpleNamespace(request=AsyncMock())
    client = SimpleNamespace(account_id=1, devices={device.key: device})
    disabled = HttpEventDispatcher(hass, SimpleNamespace(), client, session, DEFAULT_OPTIONS)
    disabled.emit("doorbell_pressed", data(device))
    assert not disabled.workers
    session.request.assert_not_called()
    await disabled.close()

    for changes in (
        {"url": "file:///private/endpoint-secret"},
        {"url": "{{ missing_variable }}"},
        {"headers": '{"X-Test": "{{ \'bad\\r\\nsecret\' }}"}'},
    ):
        dispatcher = HttpEventDispatcher(
            hass,
            SimpleNamespace(),
            client,
            session,
            {
                **DEFAULT_OPTIONS,
                "http_events_enabled": True,
                "http_event_rules": [rule(**changes)],
            },
        )
        dispatcher.emit("doorbell_pressed", data(device))
        await drain(dispatcher)
        assert dispatcher.results["test-rule"] == "invalid_template_or_request"
        await dispatcher.close()
    assert "endpoint-secret" not in caplog.text
    assert "bad\r\nsecret" not in caplog.text
    session.request.assert_not_called()


async def test_http_no_redirects_retries_or_response_body_download(
    hass, device, server, loopback_sockets
):
    received = []

    async def handler(request):
        received.append(request.path)
        if request.path == "/redirect":
            raise web.HTTPFound("/must-not-follow")
        return web.Response(status=503, text="private-response")

    url = await server(handler)
    async with aiohttp.ClientSession() as session:
        dispatcher = HttpEventDispatcher(
            hass,
            SimpleNamespace(),
            SimpleNamespace(account_id=1, devices={device.key: device}),
            session,
            {
                **DEFAULT_OPTIONS,
                "http_events_enabled": True,
                "http_event_rules": [
                    rule(url=url + "/redirect"),
                    rule(id="failed", url=url + "/unavailable"),
                ],
            },
        )
        dispatcher.emit("doorbell_pressed", data(device))
        await drain(dispatcher)
        assert received == ["/redirect", "/unavailable"]
        assert dispatcher.results == {"test-rule": "http_302", "failed": "http_503"}
        await dispatcher.close()


async def test_queue_is_bounded_and_close_cancels_pending_delivery(hass, device, caplog):
    dispatcher = HttpEventDispatcher(
        hass,
        SimpleNamespace(),
        SimpleNamespace(account_id=1, devices={device.key: device}),
        None,
        {**DEFAULT_OPTIONS, "http_events_enabled": True, "http_event_rules": [rule()]},
    )
    started = asyncio.Event()

    async def blocked(*_):
        started.set()
        await asyncio.Event().wait()

    dispatcher._send = blocked
    dispatcher.emit("doorbell_pressed", data(device))
    await started.wait()
    for i in range(40):
        dispatcher.emit("doorbell_pressed", data(device, str(i)))
    assert dispatcher.workers[device.key][0].qsize() == 32
    assert dispatcher.results["queue"] == "full"
    await dispatcher.close()
    assert not dispatcher.workers
    dispatcher.emit("doorbell_pressed", data(device))
    assert not dispatcher.workers


@pytest.mark.parametrize(
    "changes",
    [
        {"headers": "[]"},
        {"headers": '{"X":1}'},
        {"body": "{{ broken"},
        {"url": ""},
        {"name": " "},
        {"headers": '{"Bad\\nHeader":"value"}'},
    ],
)
async def test_rule_validation(hass, changes):
    with pytest.raises((ValueError, TemplateError)):
        validate_rule(hass, rule(**changes))


@pytest.mark.parametrize("body", [None, "", "{{ '' }}"])
async def test_options_allow_empty_body_and_clear_existing_body(
    hass, enable_custom_integrations, body
):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"http_event_rules": [rule()]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "http_events"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"http_events_enabled": True, "action": "edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"rule": "test-rule"}
    )
    # Exercise the actual form schema: an omitted field must not insert the
    # previous body as a default, and the frontend must see it as optional.
    import probatio as vol

    marker = next(key for key in result["data_schema"].schema if key == "body")
    assert isinstance(marker, vol.Optional) and not isinstance(marker, vol.Required)
    submitted = {
        key: value for key, value in rule(method="GET").items() if key not in ("id", "body")
    }
    if body is not None:
        submitted["body"] = body
    submitted = result["data_schema"](submitted)
    result = await hass.config_entries.options.async_configure(result["flow_id"], submitted)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["http_event_rules"][0]["body"] == (body or "")


async def test_empty_template_sends_get_without_body(hass, device):
    from unittest.mock import MagicMock

    response = MagicMock()
    response.status = 200
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.request.return_value = context
    dispatcher = HttpEventDispatcher(
        hass,
        SimpleNamespace(),
        SimpleNamespace(account_id=1, devices={device.key: device}),
        session,
        {
            **DEFAULT_OPTIONS,
            "http_events_enabled": True,
            "http_event_rules": [rule(method="GET", body="{{ '' }}")],
        },
    )
    dispatcher.emit("doorbell_pressed", data(device))
    await drain(dispatcher)
    assert session.request.call_args.args[0] == "GET"
    assert session.request.call_args.kwargs["data"] is None
    await dispatcher.close()


async def test_options_add_edit_delete_and_preserve_existing_settings(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "retention_days": 17,
            "rtsp_password": "private-test-password",
        },
    )
    entry.add_to_hass(hass)

    async def begin(section):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.MENU
        return await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": section}
        )

    result = await begin("http_events")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "http_events_enabled": True,
            "action": "add",
        },
    )
    new_rule = {k: v for k, v in rule().items() if k != "id"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], new_rule)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["http_events_enabled"]
    assert entry.options["retention_days"] == 17
    identity = entry.options["http_event_rules"][0]["id"]
    assert entry.options["rtsp_password"] == "private-test-password"

    result = await begin("http_events")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "http_events_enabled": True,
            "action": "edit",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"rule": identity}
    )
    new_rule["enabled"] = False
    result = await hass.config_entries.options.async_configure(result["flow_id"], new_rule)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["http_event_rules"][0]["id"] == identity
    assert not entry.options["http_event_rules"][0]["enabled"]

    result = await begin("http_events")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "http_events_enabled": False,
            "action": "delete",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"rule": identity}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["http_event_rules"] == []
    assert not entry.options["http_events_enabled"]
    assert entry.options["retention_days"] == 17


async def test_rtsp_options_require_credentials_and_preserve_rules(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"http_event_rules": [rule()]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "rtsp"}
    )
    values = {key: value for key, value in DEFAULT_OPTIONS.items() if key.startswith("rtsp_")}
    values["rtsp_enabled"] = True
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["errors"] == {"base": "rtsp_credentials"}
    values.update(rtsp_username="viewer", rtsp_password="private-test-password")
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["http_event_rules"] == [rule()]


async def test_diagnostics_exclude_request_and_rtsp_secrets(hass):
    from custom_components.blink_live.diagnostics import async_get_config_entry_diagnostics

    options = {
        **DEFAULT_OPTIONS,
        "rtsp_username": "private-test-user",
        "rtsp_password": "private-test-password",
        "archive_directory": "/private/path",
        "http_event_rules": [rule(url="https://secret-token.example.test")],
    }
    coordinator = SimpleNamespace(
        options=options,
        push=None,
        client=SimpleNamespace(devices={}, modules={}),
        clip_error=None,
        recordings=SimpleNamespace(state={"seen": [], "archive": [], "pending": []}),
        http_events=SimpleNamespace(results={}),
    )
    with patch(
        "custom_components.blink_live.diagnostics.async_get_integration",
        AsyncMock(return_value=SimpleNamespace(version="0.1.0")),
    ):
        result = await async_get_config_entry_diagnostics(
            hass, SimpleNamespace(runtime_data=coordinator)
        )
    serialized = json.dumps(result)
    assert "private" not in serialized and "secret-token" not in serialized
    assert result["http_rule_count"] == 1


async def test_template_variables_include_actual_camera_entity(hass, device):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "camera", DOMAIN, f"1_{device.key}_camera", config_entry=entry
    )
    dispatcher = HttpEventDispatcher(
        hass,
        entry,
        SimpleNamespace(account_id=1, devices={device.key: device}),
        None,
        DEFAULT_OPTIONS,
    )
    assert dispatcher._variables("doorbell_pressed", data(device))["entity_id"] == entity.entity_id
    await dispatcher.close()


async def test_http_timeout_is_sanitized_and_not_retried(
    hass, device, server, loopback_sockets, caplog
):
    received = []

    async def handler(request):
        received.append(request.path)
        await asyncio.sleep(0.1)
        return web.Response()

    url = await server(handler)
    async with aiohttp.ClientSession() as session:
        dispatcher = HttpEventDispatcher(
            hass,
            SimpleNamespace(),
            SimpleNamespace(account_id=1, devices={device.key: device}),
            session,
            {
                **DEFAULT_OPTIONS,
                "http_events_enabled": True,
                "http_event_rules": [rule(url=url + "/private-token")],
            },
        )

        # Keep the production five-second timeout assertion, but shorten the
        # synthetic response delay for a deterministic, quick failure test.
        def timeout(**kwargs):
            assert kwargs == {"total": 5}
            return aiohttp.ClientTimeout(total=0.03)

        with patch("custom_components.blink_live.http_events.ClientTimeout", timeout):
            dispatcher.emit("doorbell_pressed", data(device))
            await drain(dispatcher)
        assert dispatcher.results == {"test-rule": "connection_failed"}
        assert received == ["/private-token"]
        assert "private-token" not in caplog.text
        await dispatcher.close()

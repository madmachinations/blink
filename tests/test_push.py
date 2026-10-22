"""Offline push protocol, routing, persistence and connection lifecycle tests."""

import asyncio
import copy
import hashlib
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from blink_live_client import BlinkClient, BlinkError
from blink_live_client.errors import AuthError
from blink_live_client.push import PushLoginRejected, PushReceiver, classify, resolve_device
from blink_live_client.push_proto import (
    MAX_FRAME,
    blob,
    checkin_request,
    fields,
    frame,
    integer,
    login_request,
    number,
    read_frame,
    stream_ack,
    string,
    varint,
)
from blink_live_client.push_registration import (
    ANDROID_PACKAGE,
    FIREBASE_SENDER,
    PushRegistration,
)


@pytest.fixture
def receiver(device):
    client = SimpleNamespace(
        devices={device.key: device},
        account_id=1,
        auth=SimpleNamespace(state={"tier": "prod"}),
        session=None,
        associate_push_token=AsyncMock(),
        acknowledge_push=AsyncMock(),
    )
    return PushReceiver(client, {}, AsyncMock(), MagicMock(), MagicMock())


def message(event="button_press", *, identity="push-1", **overrides):
    payload = {
        "event_type": event,
        "category": "media",
        "account_id": "1",
        "tier": "prod",
        "camera_id": "3",
        "network_id": "2",
        **overrides,
    }
    return fields(
        blob(9, identity)
        + blob(3, FIREBASE_SENDER)
        + blob(5, ANDROID_PACKAGE)
        + number(18, int(time.time() * 1000))
        + b"".join(blob(7, blob(1, key) + blob(2, value)) for key, value in payload.items())
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"category": "media", "event_type": "button_press"}, "doorbell_press"),
        ({"category": "media", "event_type": "pir"}, "camera_motion"),
        ({"category": "motion", "event_type": "cloud_motion"}, "camera_motion"),
        ({"category": "motion", "event_type": "cam2sm_motion"}, "camera_motion"),
        ({"category": "media", "event_type": "unknown"}, None),
        ({"category": [], "event_type": "pir"}, None),
    ],
)
def test_classify(payload, expected):
    assert classify(payload) == expected


def test_device_mapping_is_unambiguous_and_account_scoped(receiver, device):
    client = receiver.client
    duplicate = replace(device, id=4)
    client.devices[duplicate.key] = duplicate
    assert resolve_device(client, {"camera_name": device.name}) is None
    assert resolve_device(client, {"camera_id": "3"}) == device
    assert resolve_device(client, {"camera_id": "99", "camera_name": device.name}) is None
    assert resolve_device(client, {"camera_id": "3", "network_id": "999"}) is None
    assert resolve_device(client, {"camera_id": "3", "account_id": "2"}) is None
    assert resolve_device(client, {"camera_id": "3", "tier": "prde"}) is None
    assert resolve_device(client, {"serial": "unknown"}) is None
    assert resolve_device(client, {}) is None
    duplicate = replace(duplicate, name="Other")
    client.devices[duplicate.key] = duplicate
    assert resolve_device(client, {"camera_name": device.name}) == device


async def test_checkpoint_precedes_dispatch_and_dedup_survives_restart(receiver, device):
    checkpoints = []

    async def save(state):
        checkpoints.append(copy.deepcopy(state))
        receiver.on_event.assert_not_called()

    receiver.save = save
    push = message()
    await receiver._message(push)
    assert len(checkpoints) == 1
    expected_id = hashlib.sha256(b"push-1").hexdigest()
    kind, key, event_id, occurred_at = receiver.on_event.call_args.args
    assert (kind, key, event_id) == ("doorbell_press", device.key, expected_id)
    assert occurred_at.endswith("+00:00")
    receiver.on_event.reset_mock()
    restarted = PushReceiver(
        receiver.client, checkpoints[-1], AsyncMock(), MagicMock(), MagicMock()
    )
    await restarted._message(push)
    restarted.on_event.assert_not_called()
    restarted.save.assert_not_awaited()
    assert restarted.state["persistent_ids"] == ["push-1"]


async def test_failed_checkpoint_allows_redelivery(receiver):
    receiver.save.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        await receiver._message(message())
    receiver.on_event.assert_not_called()
    assert not receiver.state
    receiver.save.side_effect = None
    await receiver._message(message())
    receiver.on_event.assert_called_once()


@pytest.mark.parametrize("invalid", ["stale", "future", "undated", "sender", "package", "unknown"])
async def test_rejects_stale_untrusted_or_unknown_pushes(receiver, invalid):
    push = message()
    if invalid == "stale":
        push[18] = [int((time.time() - 180) * 1000)]
    elif invalid == "future":
        push[18] = [int((time.time() + 180) * 1000)]
    elif invalid == "undated":
        push.pop(18)
    elif invalid == "sender":
        push[3] = [b"other-project"]
    elif invalid == "package":
        push[5] = [b"other-app"]
    else:
        push = message("new-event")
    await receiver._message(push)
    receiver.on_event.assert_not_called()
    assert receiver.rejected_events == 1
    assert receiver.state["persistent_ids"] == ["push-1"]


async def test_nested_payload_and_timestamp_fallback(receiver):
    import json
    from datetime import UTC, datetime

    payload = {
        "event_type": "pir",
        "category": "media",
        "camera_name": "Front Door",
        "created_at": datetime.now(UTC).isoformat(),
    }
    push = fields(blob(9, "nested") + blob(7, blob(1, "data") + blob(2, json.dumps(payload))))
    await receiver._message(push)
    assert receiver.on_event.call_args.args[0] == "camera_motion"


async def test_malformed_app_data_is_checkpointed_without_dispatch(receiver):
    push = message()
    push[7] = [b"\xff"]
    await receiver._message(push)
    receiver.on_event.assert_not_called()
    assert receiver.state["persistent_ids"] == ["push-1"]


async def test_dedup_history_is_bounded(receiver):
    receiver.state["event_ids"] = [str(i) for i in range(1000)]
    receiver.state["persistent_ids"] = [str(i) for i in range(1000)]
    await receiver._message(message())
    assert len(receiver.state["event_ids"]) == len(receiver.state["persistent_ids"]) == 1000


@pytest.mark.parametrize("data", [b"\x00", b"\x0a\x05ab", b"\x0f", b"\x08" + b"\xff" * 10])
def test_protobuf_rejects_invalid_fields(data):
    with pytest.raises(ValueError):
        fields(data)


def test_checkin_login_and_stream_ack_wire_contract():
    state = {"android_id": 123, "security_token": 456, "persistent_ids": ["last"]}
    checkin = fields(checkin_request(state))
    assert integer(fields(checkin[4][0]), 12) == 1
    assert integer(checkin, 13) == 456
    login = fields(login_request(state))
    assert string(login, 5) == "456"
    assert string(login, 6) == "android-7b"
    assert string(login, 10) == "last"
    ack = fields(stream_ack(7))
    assert integer(ack, 10) == 7
    assert integer(fields(ack[7][0]), 1) == 13


async def test_fragmented_frame_is_reassembled():
    reader = asyncio.StreamReader()
    task = asyncio.create_task(read_frame(reader))
    for byte in frame(8, b"x" * 300):
        reader.feed_data(bytes([byte]))
        await asyncio.sleep(0)
    assert await task == (8, b"x" * 300)


@pytest.mark.parametrize("data", [b"\x08" + varint(MAX_FRAME + 1), b"\x08" + b"\x80" * 5])
async def test_oversized_or_invalid_frame_length(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    with pytest.raises(ValueError):
        await read_frame(reader)


async def test_missing_heartbeat_ack_times_out_even_when_writes_succeed(receiver, monkeypatch):
    monkeypatch.setattr("blink_live_client.push.HEARTBEAT_TIMEOUT", 0.01)
    receiver._interval = 0
    receiver._send = AsyncMock()
    with pytest.raises(TimeoutError):
        await receiver._heartbeat()
    assert receiver._send.call_args.args[0][0] == 0


async def test_read_acks_only_after_durable_message_handling(receiver):
    reader = asyncio.StreamReader()
    receiver._reader = reader
    writes = []

    async def send(data):
        if data[0] == 7:
            assert receiver.save.await_count == 1
            assert receiver.on_event.call_count == 1
        writes.append(data)

    receiver._send = send
    payload = message()
    payload[10] = [9]
    encoded = b"".join(
        number(key, value) if isinstance(value, int) else blob(key, value)
        for key, values in payload.items()
        for value in values
    )
    reader.feed_data(frame(0) + frame(8, encoded) + frame(4))
    with pytest.raises(BlinkError):
        await receiver._read()
    assert [data[0] for data in writes] == [1, 7]
    assert integer(fields(writes[-1][2:]), 10) == 9


async def test_close_cancels_reader_heartbeat_and_receipt_tasks(receiver):
    receiver.registration.ensure = AsyncMock()
    receiver.state["fcm_token"] = "test"
    ready = asyncio.Event()

    async def connect():
        receiver._reader = asyncio.StreamReader()
        ready.set()

    receiver._connect = connect
    receiver.start()
    await ready.wait()
    await asyncio.sleep(0)
    task = receiver._task
    await receiver.close()
    assert task.done() and receiver._task is None
    assert receiver.status == "disabled"
    assert not [
        task
        for task in asyncio.all_tasks()
        if any(
            name in task.get_coro().__qualname__
            for name in (
                "PushReceiver._read",
                "PushReceiver._heartbeat",
                "PushReceiver._receipt_worker",
            )
        )
    ]


async def test_google_login_rejection_recovers_android_identity(receiver, monkeypatch):
    receiver.registration.ensure = AsyncMock()
    receiver.state["fcm_token"] = "test"
    receiver._connect = AsyncMock(side_effect=[PushLoginRejected("rejected"), AuthError("expired")])
    # Backoff yields without real time passing.
    sleep = AsyncMock()
    monkeypatch.setattr("blink_live_client.push.asyncio.sleep", sleep)
    await receiver._run()
    assert [call.kwargs for call in receiver.registration.ensure.await_args_list] == [
        {"reset_android": False},
        {"reset_android": True},
    ]
    assert receiver.status == "auth_failed"


async def test_blink_client_uses_header_id_and_existing_session(server):
    calls = []

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer existing-session"
        calls.append(
            (request.method, request.path, await request.json() if request.can_read_body else {})
        )
        if request.path.endswith("homescreen"):
            return web.json_response({}, headers={"Client-Id": "77"})
        assert request.headers["Client-Id"] == "77"
        return web.Response(status=204)

    url = await server(handler)

    class LocalClient(BlinkClient):
        @property
        def root(self):
            return url

    async with aiohttp.ClientSession() as session:
        client = LocalClient(
            session,
            {
                "account_id": 1,
                "tier": "prod",
                "client_id": 99,
                "access_token": "existing-session",
                "expires_at": time.time() + 3600,
            },
        )
        await client.associate_push_token("own-fcm-token")
        await client.acknowledge_push(
            {
                "return_receipt": "true",
                "unique_id": "transaction",
                "account_id": "1",
                "tier": "prod",
            }
        )
        await client.acknowledge_push(
            {
                "return_receipt": "true",
                "unique_id": "foreign",
                "account_id": "2",
                "tier": "evil.example",
            }
        )
    assert calls[0][:2] == ("GET", "/api/v3/accounts/1/homescreen")
    assert calls[1][1] == "/clients/77/update"
    assert calls[1][2]["notification_key"] == "own-fcm-token"
    assert [call[2]["status"] for call in calls[2:]] == ["RECEIVED", "UNWANTED"]


async def test_missing_header_does_not_use_unverified_client_id(receiver):
    async with aiohttp.ClientSession() as session:
        client = BlinkClient(session, {"account_id": 1, "client_id": 99})
        client.request = AsyncMock(return_value={})
        with pytest.raises(BlinkError, match="identifier"):
            await client.associate_push_token("token")
        assert client.request.await_count == 1


async def test_registration_resumes_and_renews_without_process_socket_changes():
    state, saved = {}, []

    async def save(value):
        saved.append(copy.deepcopy(value))

    registration = PushRegistration(None, state, save)
    registration._post = AsyncMock(
        side_effect=[
            b'{"refreshToken":"refresh","authToken":{"token":"fis","expiresIn":"7200s"}}',
            number(7, 123) + number(8, 456),
            b"token=first-fcm",
        ]
    )
    import socket

    before = socket.getdefaulttimeout()
    await registration.ensure()
    assert socket.getdefaulttimeout() == before
    assert len(saved) == 3 and state["fcm_token"] == "first-fcm"
    assert (
        registration._post.call_args.kwargs["data"]["X-Goog-Firebase-Installations-Auth"] == "fis"
    )
    registration._post.reset_mock(side_effect=True)
    await registration.ensure()
    registration._post.assert_not_awaited()
    state["fis_expires_at"] = 0
    state["registered_at"] = 0
    registration._post.side_effect = [
        b'{"token":"renewed-fis","expiresIn":"7200s"}',
        b"token=new-fcm",
    ]
    await registration.ensure()
    first = registration._post.call_args_list[0]
    assert first.args[0].endswith("authTokens:generate")
    assert first.kwargs["headers"]["Authorization"] == "FIS_v2 refresh"
    assert state["fcm_token"] == "new-fcm" and state["fis_auth_token"] == "renewed-fis"


async def test_deleted_firebase_installation_is_recreated():
    from blink_live_client.errors import ApiError

    state = {"fid": "old", "fis_refresh_token": "old-refresh", "fcm_token": "old-fcm"}
    registration = PushRegistration(None, state, AsyncMock())
    registration._post = AsyncMock(
        side_effect=[
            ApiError(404),
            b'{"refreshToken":"new-refresh","authToken":{"token":"new-fis","expiresIn":"7200s"}}',
            number(7, 123) + number(8, 456),
            b"token=new-fcm",
        ]
    )
    await registration.ensure()
    assert state["fid"] != "old" and state["fcm_token"] == "new-fcm"


async def test_registration_retries_transient_error(receiver, monkeypatch):
    registration = receiver.registration
    registration._post = AsyncMock(
        side_effect=[
            b'{"refreshToken":"r","authToken":{"token":"fis","expiresIn":"7200s"}}',
            number(7, 123) + number(8, 456),
            b"Error=PHONE_REGISTRATION_ERROR",
            b"token=eventual",
        ]
    )
    monkeypatch.setattr("blink_live_client.push_registration.asyncio.sleep", AsyncMock())
    await registration.ensure()
    assert receiver.state["fcm_token"] == "eventual"


async def test_mcs_login_negotiates_heartbeat_and_uses_tls(receiver, monkeypatch):
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x29" + frame(3, number(5, 7) + blob(7, number(3, 120000))))
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(reader, writer))
    context = object()
    monkeypatch.setattr("blink_live_client.push.ssl.create_default_context", lambda: context)
    monkeypatch.setattr("blink_live_client.push.asyncio.open_connection", connect)
    receiver.state.update(android_id=123, security_token=456)
    await receiver._connect()
    assert connect.call_args.args == ("mtalk.google.com", 5228)
    assert connect.call_args.kwargs == {"ssl": context, "server_hostname": "mtalk.google.com"}
    assert writer.write.call_args.args[0].startswith(b"\x29\x02")
    assert receiver._interval == 120 and receiver._stream_id == 7
    assert receiver.status == "connected"
    await receiver.close()
    writer.close.assert_called_once()


async def test_eof_reconnects_and_reassociates_instead_of_killing_task(receiver, monkeypatch):
    receiver.registration.ensure = AsyncMock()
    receiver.state["fcm_token"] = "test"
    receiver._connect = AsyncMock(side_effect=[asyncio.IncompleteReadError(b"", 1), AuthError()])
    monkeypatch.setattr("blink_live_client.push.asyncio.sleep", AsyncMock())
    await receiver._run()
    assert receiver.client.associate_push_token.await_count == 2
    assert receiver.status == "auth_failed"


async def test_receipt_retry_failure_is_isolated_from_event_delivery(receiver, monkeypatch):
    receiver.client.acknowledge_push.side_effect = BlinkError("unavailable")
    monkeypatch.setattr("blink_live_client.push.asyncio.sleep", AsyncMock())
    await receiver._message(message(return_receipt="true", unique_id="transaction"))
    worker = asyncio.create_task(receiver._receipt_worker())
    try:
        await receiver._receipts.join()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert receiver.client.acknowledge_push.await_count == 3
    assert receiver.receipt_failures == 1
    receiver.on_event.assert_called_once()
    receipt = receiver.client.acknowledge_push.call_args.args[0]
    assert set(receipt) == {"return_receipt", "unique_id", "account_id", "tier"}


async def test_rejected_gcm_http_auth_invalidates_credentials(receiver):
    from blink_live_client.errors import ApiError

    receiver.state.update(fis_auth_token="old", security_token=1, fcm_token="old")
    receiver.registration._post = AsyncMock(side_effect=ApiError(401))
    with pytest.raises(ApiError):
        await receiver.registration._gcm_post(data={})
    assert not receiver.state
    receiver.save.assert_awaited_once_with({})


@pytest.mark.parametrize("body", [b"[]", b'{"refreshToken":"r","authToken":[]}'])
async def test_malformed_firebase_response_is_safe(receiver, body):
    receiver.registration._post = AsyncMock(return_value=body)
    with pytest.raises(BlinkError):
        await receiver.registration.ensure()


async def test_retry_after_registration_checkpoint_failure_cannot_skip_save(receiver):
    state = receiver.state
    state.update(
        fis_auth_token="fis",
        fis_expires_at=time.time() + 3600,
        android_id=1,
        security_token=2,
        checkin_at=time.time(),
        fcm_token="token",
        registered_at=time.time(),
    )
    receiver.registration.save.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        await receiver.registration.ensure()


@pytest.mark.parametrize("response_type", ["redirect", "oversized"])
async def test_registration_http_rejects_redirect_and_oversized_responses(server, response_type):
    calls = []

    async def handler(request):
        calls.append(request.path)
        if response_type == "redirect":
            return web.Response(status=302, headers={"Location": "/unwanted"})
        return web.Response(body=b"x" * (MAX_FRAME + 1))

    root = await server(handler)
    async with aiohttp.ClientSession() as session:
        registration = PushRegistration(session, {}, AsyncMock())
        with pytest.raises(BlinkError):
            await registration._post(root)
    assert calls == ["/"]

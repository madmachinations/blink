"""Development log captures signals before production filtering, without HA."""

import asyncio
import base64
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from blink_live_client import cli
from blink_live_client.errors import BlinkError
from blink_live_client.notification_log import NotificationLog, log_notifications
from blink_live_client.push import PushReceiver
from blink_live_client.push_proto import blob, fields, number


def message(event_type="pir", identity="test-message", **overrides):
    payload = {"category": "media", "event_type": event_type, "camera_id": "3", **overrides}
    return fields(
        blob(9, identity)
        + number(18, int(time.time() * 1000))
        + b"".join(blob(7, blob(1, key) + blob(2, value)) for key, value in payload.items())
    )


@pytest.fixture
def client(device):
    return SimpleNamespace(
        session=None,
        devices={device.key: device},
        account_id=1,
        auth=SimpleNamespace(state={"tier": "prod"}),
        acknowledge_push=AsyncMock(),
    )


def rows(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_unknown_duplicate_and_stale_notifications_are_visible(client):
    stream = io.StringIO()
    log = NotificationLog(stream)
    emit = MagicMock()
    receiver = PushReceiver(client, {}, AsyncMock(), emit, MagicMock(), log.observe)
    unknown = message("unfamiliar_event")
    await receiver._message(unknown)
    await receiver._message(unknown)
    stale = message(identity="old")
    stale[18] = [int((time.time() - 180) * 1000)]
    await receiver._message(stale)
    results = [row for row in rows(stream) if row["event"] == "notification_result"]
    assert [row["reason"] for row in results] == ["unknown_event_type", "duplicate", "stale"]
    received = [row for row in rows(stream) if row["event"] == "notification_received"]
    assert len(received) == 3 and received[0]["payload"]["event_type"] == "unfamiliar_event"
    assert not any(row["accepted"] for row in results)
    emit.assert_not_called()


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"account_id": "other"}, "account_mismatch"),
        ({"tier": "prde"}, "region_mismatch"),
        ({"camera_id": "999"}, "device_not_found"),
    ],
)
async def test_rejection_reason_includes_mapping_failures(client, payload, reason):
    observer = MagicMock()
    receiver = PushReceiver(client, {}, AsyncMock(), MagicMock(), MagicMock(), observer)
    await receiver._message(message(**payload))
    assert observer.call_args.args[1]["reason"] == reason


async def test_raw_capture_is_explicit_and_preserves_original_bytes(client):
    secret = "private-token-value"
    push = message(token=secret, unknown_data="device-specific-value")
    normal, raw = io.StringIO(), io.StringIO()
    for stream, enabled in ((normal, False), (raw, True)):
        log = NotificationLog(stream, raw=enabled)
        receiver = PushReceiver(client, {}, AsyncMock(), MagicMock(), MagicMock(), log.observe)
        await receiver._message(push)
    assert secret not in normal.getvalue() and "device-specific-value" not in normal.getvalue()
    received = rows(normal)[0]
    assert "unknown_data" in received["payload_keys"]
    assert "raw_message" not in received
    captured = rows(raw)[0]
    assert captured["raw_payload"]["token"] == secret
    assert base64.b64decode(captured["raw_message"]["7"][-1]["base64"]) == push[7][-1]


async def test_malformed_payload_and_missing_identity_still_captured(client):
    stream = io.StringIO()
    log = NotificationLog(stream, raw=True)
    receiver = PushReceiver(client, {}, AsyncMock(), MagicMock(), MagicMock(), log.observe)
    await receiver._message({7: [b"\xff"]})
    assert rows(stream)[0]["raw_message"]["7"] == [{"base64": "/w=="}]
    assert rows(stream)[1]["reason"] == "invalid_event_identity"


def test_markers_are_timestamped_human_observations():
    stream = io.StringIO()
    log = NotificationLog(stream)
    for marker in ("d\n", "m\n", "b\n", "invalid"):
        log.marker(marker)
    assert [row["label"] for row in rows(stream)] == ["doorbell_press", "motion", "baseline"]
    assert all(row["source"] == "human_observation" and row["observed_at"] for row in rows(stream))


async def test_cancel_flushes_log_persists_separate_push_state_and_closes(
    tmp_path, monkeypatch, client
):
    ready = asyncio.Event()
    receivers = []

    async def run(receiver):
        receivers.append(receiver)
        receiver._status("connected")
        await receiver._message(message("new-device-event"))
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(PushReceiver, "_run", run)
    output, state = tmp_path / "notifications.jsonl", tmp_path / "push.json"
    task = asyncio.create_task(log_notifications(client, output, state, interactive=False))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert output.stat().st_mode & 0o777 == state.stat().st_mode & 0o777 == 0o600
    captured = [json.loads(line) for line in output.read_text().splitlines()]
    assert captured[0]["mode"] == "push" and captured[0]["push_receiver"] is True
    assert captured[-1]["event"] == "session_stopped"
    assert captured[-1]["reason"] == "cancelled"
    assert receivers[0].status == "disabled" and receivers[0]._task is None
    assert json.loads(state.read_text())["persistent_ids"] == ["test-message"]
    with pytest.raises(FileExistsError):
        await log_notifications(client, output, state, interactive=False)


async def test_duration_stops_receiver_without_real_connection(tmp_path, monkeypatch, client):
    async def run(receiver):
        receiver._status("reconnecting")
        await asyncio.Event().wait()

    monkeypatch.setattr(PushReceiver, "_run", run)
    output = tmp_path / "timed.jsonl"
    await log_notifications(client, output, tmp_path / "push.json", duration=1, interactive=False)
    captured = [json.loads(line) for line in output.read_text().splitlines()]
    assert captured[-1]["reason"] == "duration_expired"


async def test_log_path_cannot_overwrite_push_state_or_follow_symlinks(tmp_path, client):
    output = tmp_path / "capture"
    with pytest.raises(BlinkError):
        await log_notifications(client, output, output)
    assert not output.exists()
    target = tmp_path / "existing"
    target.write_text("unchanged")
    output.symlink_to(target)
    with pytest.raises(OSError):
        await log_notifications(client, output, tmp_path / "push.json")
    assert target.read_text() == "unchanged"


async def test_invalid_duration_does_not_create_log(tmp_path, client):
    output = tmp_path / "invalid.jsonl"
    with pytest.raises(BlinkError):
        await log_notifications(client, output, tmp_path / "push.json", duration=0)
    assert not output.exists()


def test_cli_renames_command_and_reuses_default_cli_identity(tmp_path, monkeypatch):
    started = []

    async def run(args):
        started.append(args)

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(
        "sys.argv", ["blink-live", "notification-log", "--output", "test.jsonl", "--raw"]
    )
    cli.main()
    assert started[0].state == tmp_path / ".blink-live" / "auth.json"
    assert started[0].raw is True
    assert not hasattr(started[0], "interval") and not hasattr(started[0], "source")
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["event-log", "--output", "old.jsonl"])


async def test_cli_uses_notification_log_without_polling_recordings(tmp_path, monkeypatch):
    from pathlib import Path

    auth = tmp_path / "custom-auth.json"
    monkeypatch.setattr(cli, "load_json", AsyncMock(return_value={"refresh_token": "test"}))
    initialize = AsyncMock()
    monkeypatch.setattr(cli, "initialize", initialize)
    logger = AsyncMock()
    monkeypatch.setattr(cli, "log_notifications", logger)
    args = cli.parser().parse_args(
        ["--state", str(auth), "notification-log", "--output", "capture.jsonl", "--no-interactive"]
    )
    await cli.run(args)
    initialize.assert_awaited_once()
    assert logger.call_args.args[1:5] == (
        Path("capture.jsonl"),
        tmp_path / "custom-auth-push.json",
        None,
        False,
    )

import asyncio
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from blink_live_client.errors import BlinkError
from blink_live_client.event_log import EventLog, log_events


def test_observations_are_filtered_deduplicated_and_separate_from_markers():
    stream = io.StringIO()
    log = EventLog(stream)
    row = {
        "id": 1,
        "armed": False,
        "token": "secret",
        "thumbnail": "https://secret",
        "source": "https://secret",
        "trigger": {"token": "secret"},
    }
    log.observe("homescreen", {"networks": [row]})
    log.observe("homescreen", {"networks": [row]})
    log.marker("d\n")
    log.marker("m\n")
    log.marker("invalid\n")
    log.observe("homescreen", {"networks": [{**row, "armed": True}]})
    log.observe(
        "usb_manifest",
        {
            "module_id": 4,
            "clips": [{"id": "42", "created_at": "2026-10-19T10:00:00Z", "event_type": "pir"}],
        },
    )
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(rows) == 5
    assert rows[0]["fields"] == {"id": 1, "armed": False}
    assert rows[1]["event"] == "manual_marker"
    assert rows[1]["label"] == "doorbell_press"
    assert rows[2]["label"] == "motion"
    assert rows[3]["previous"]["armed"] is False
    assert rows[4]["fields"]["event_type"] == "pir"
    assert "secret" not in stream.getvalue()
    assert all("observed_at" in row and "elapsed_seconds" in row for row in rows)


async def test_cancel_flushes_private_log_and_detaches_observer(tmp_path):
    polled = asyncio.Event()

    async def discover():
        client.observer("homescreen", {"doorbells": [{"id": 3, "status": "online"}]})
        polled.set()

    client = SimpleNamespace(observer=None, discover=discover, modules={})
    output = tmp_path / "test.jsonl"
    task = asyncio.create_task(log_events(client, output, 30, None, False))
    await polled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.observer is None
    assert output.stat().st_mode & 0o777 == 0o600
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["push_receiver"] is False
    assert rows[-1]["event"] == "session_stopped"
    assert rows[-1]["reason"] == "cancelled"
    with pytest.raises(FileExistsError):
        await log_events(client, output, 30, None, False)


async def test_errors_are_sanitized_and_duration_stops_retry_wait(tmp_path):
    calls = 0

    async def discover():
        nonlocal calls
        calls += 1
        raise BlinkError("secret server response")

    client = SimpleNamespace(observer=None, discover=discover, modules={})
    output = tmp_path / "error.jsonl"
    await log_events(client, output, 30, 1, False)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert calls == 1
    assert rows[1]["event"] == "poll_failed"
    assert rows[1]["retry_seconds"] == 60
    assert rows[-1]["reason"] == "duration_expired"
    assert "secret" not in output.read_text()
    assert client.observer is None


async def test_existing_clips_baselined_and_only_new_clip_emits(
    tmp_path, monkeypatch, clip, module
):
    from blink_live_client import event_log

    polls = 0
    finished = asyncio.Event()
    real_sleep = asyncio.sleep
    new_clip = replace(clip, id="7")

    async def discover():
        pass

    async def clips(_module):
        nonlocal polls
        polls += 1
        return [clip] if polls == 1 else [clip, new_clip]

    async def sleep(_delay):
        if polls == 1:
            return
        finished.set()
        await real_sleep(3600)

    monkeypatch.setattr(event_log.asyncio, "sleep", sleep)
    client = SimpleNamespace(observer=None, discover=discover, clips=clips, modules={4: module})
    output = tmp_path / "clips.jsonl"
    task = asyncio.create_task(log_events(client, output, 30, None, False))
    await finished.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    available = [row for row in rows if row["event"] == "blink_live_clip_available"]
    assert len(available) == 1
    assert available[0]["clip_id"] == new_clip.key
    assert available[0]["recorded_at"] == new_clip.created_at
    assert available[0]["discovered_at"]
    assert all(row["event"] != "blink_live_doorbell_pressed" for row in rows)
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("interval,duration", [(29, None), (60, 0)])
async def test_invalid_settings_do_not_create_log(tmp_path, interval, duration):
    output = tmp_path / "invalid.jsonl"
    with pytest.raises(BlinkError):
        await log_events(None, output, interval, duration, False)
    assert not output.exists()

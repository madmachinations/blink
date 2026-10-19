"""Development observations, explicitly separate from verified push events."""

import asyncio
import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import BlinkError
from .recordings import Recordings

# Never dump raw responses: these can contain thumbnails, URLs and credentials.
FIELDS = (
    "id",
    "network_id",
    "camera_id",
    "device_id",
    "deviceId",
    "created_at",
    "updated_at",
    "status",
    "armed",
    "enabled",
    "motion_enabled",
    "motion_detected",
    "event_type",
    "source",
    "trigger",
    "type",
    "local_storage_status",
)


def safe_fields(row: dict) -> dict:
    """Keep only bounded scalar diagnostic fields; omit URLs and arbitrary objects."""
    result = {}
    for key in FIELDS:
        value = row.get(key)
        if value is None or type(value) in (bool, int):
            if key in row:
                result[key] = value
        elif isinstance(value, str) and len(value) <= 100:
            if all(c.isalnum() or c in " _-.:+" for c in value):
                result[key] = value
    return result


class EventLog:
    def __init__(self, stream):
        self.stream = stream
        self.started = time.monotonic()
        self.previous: dict[tuple, dict] = {}

    def write(self, event: str, **data) -> None:
        self.stream.write(
            json.dumps(
                {
                    "observed_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": round(time.monotonic() - self.started, 3),
                    "event": event,
                    **data,
                }
            )
            + "\n"
        )
        self.stream.flush()

    def observe(self, kind: str, payload: dict) -> None:
        groups = ("networks", "cameras", "doorbells", "owls", "sync_modules")
        for group in groups if kind == "homescreen" else ("clips",):
            for row in payload.get(group, []):
                fields = safe_fields(row)
                identity = (
                    group,
                    payload.get("module_id"),
                    fields.get("network_id"),
                    fields.get("id"),
                    fields.get("created_at"),
                )
                previous = self.previous.get(identity)
                if previous != fields:
                    self.write(
                        "api_observation",
                        source=kind,
                        group=group,
                        module_id=payload.get("module_id"),
                        fields=fields,
                        previous=previous,
                    )
                    self.previous[identity] = fields

    def marker(self, line: str) -> None:
        label = {"m": "motion", "d": "doorbell_press", "b": "baseline"}.get(line.strip())
        if label:
            self.write("manual_marker", label=label, source="human_observation")
            print(f"Marked {label}", flush=True)


async def log_events(
    client,
    output: Path,
    interval: int,
    duration: int | None,
    interactive: bool = True,
    recording_source: str = "usb",
) -> None:
    """Observe polling without changing production recording checkpoints or archiving."""
    if interval < 30 or (duration is not None and duration < 1):
        raise BlinkError("Minimum poll interval is 30 seconds; duration must be positive")
    # Exclusive creation avoids replacing old evidence or following an existing symlink.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        log = EventLog(stream)
        log.write(
            "session_started",
            interval_seconds=interval,
            push_receiver=False,
            mode="polling",
            schema_version=1,
            recording_source=recording_source,
        )
        client.observer = log.observe

        async def save(_state):
            pass  # Experiments must not consume the normal watch command's checkpoints.

        async def notify(kind, data):
            log.write(f"blink_live_{kind}", **data)

        recordings = Recordings(client, {}, save, notify, recording_source=recording_source)
        loop = asyncio.get_running_loop()
        reader_added = False
        reason = "failed"
        deadline = asyncio.timeout(duration)
        try:
            if interactive and sys.stdin.isatty():
                loop.add_reader(sys.stdin.fileno(), lambda: log.marker(sys.stdin.readline()))
                reader_added = True
                print("Markers: m + Enter = motion, d = doorbell press, b = baseline.")
            print(f"Logging polling observations to {output}. No push receiver. Ctrl-C to stop.")
            async with deadline:
                delay = interval
                while True:
                    try:
                        await client.discover()
                        await recordings.refresh()
                        log.write("poll_completed", clip_count=len(recordings.clips))
                        delay = interval
                    except BlinkError as error:
                        delay = min(900, delay * 2)
                        # Exception messages can contain server responses; log the class only.
                        log.write(
                            "poll_failed", error_type=type(error).__name__, retry_seconds=delay
                        )
                    await asyncio.sleep(delay)
        except TimeoutError:
            if not deadline.expired():
                raise
            reason = "duration_expired"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            if reader_added:
                loop.remove_reader(sys.stdin.fileno())
            client.observer = None
            with contextlib.suppress(OSError):
                log.write("session_stopped", reason=reason)

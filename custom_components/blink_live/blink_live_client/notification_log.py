"""Development capture of live notifications, including rejected and unknown pushes."""

import asyncio
import base64
import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import BlinkError
from .push import PushReceiver
from .storage import load_json, save_json

# Normal capture exposes useful protocol fields, never arbitrary payload values.
PAYLOAD_FIELDS = {
    "category",
    "event_type",
    "camera_id",
    "device_id",
    "network_id",
    "account_id",
    "camera_name",
    "serial",
    "tier",
    "created_at",
    "updated_at",
    "return_receipt",
}


class NotificationLog:
    def __init__(self, stream, raw=False):
        self.stream = stream
        self.raw = raw
        self.started = time.monotonic()

    def write(self, event, **data):
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

    def observe(self, kind, data):
        if kind == "notification_received":
            payload, message = data["payload"], data["message"]
            values = {}
            for key in PAYLOAD_FIELDS:
                value = payload.get(key)
                if key in payload and (
                    value is None
                    or type(value) in (bool, int, float)
                    or (
                        isinstance(value, str)
                        and len(value) <= 256
                        and "://" not in value
                        and "\n" not in value
                        and "\r" not in value
                    )
                ):
                    values[key] = value
            record = {
                "payload": values,
                "payload_keys": sorted(str(key)[:100] for key in payload)[:128],
                "provider_sent_ms": next(
                    (v for v in message.get(18, []) if isinstance(v, int)), None
                ),
                "stream_id": next((v for v in message.get(10, []) if isinstance(v, int)), None),
            }
            if self.raw:
                record["raw_payload"] = payload
                # Preserve the original decoded protobuf fields losslessly,
                # including nested app_data and unparsed binary raw_data.
                record["raw_message"] = {
                    str(key): [
                        {"base64": base64.b64encode(value).decode()}
                        if isinstance(value, bytes)
                        else value
                        for value in values
                    ]
                    for key, values in message.items()
                }
            self.write(kind, **record)
        else:
            self.write(kind, **data)

    def marker(self, line):
        label = {"m": "motion", "d": "doorbell_press", "b": "baseline"}.get(line.strip())
        if label:
            self.write("manual_marker", label=label, source="human_observation")
            print(f"Marked {label}", flush=True)


async def log_notifications(
    client,
    output: Path,
    state_path: Path,
    duration: int | None = None,
    interactive: bool = True,
    raw: bool = False,
):
    """Observe the shared live receiver without HA dispatch or recording polling."""
    if duration is not None and duration < 1:
        raise BlinkError("Notification log duration must be positive")
    output_path, push_path = await asyncio.gather(
        asyncio.to_thread(output.resolve), asyncio.to_thread(state_path.resolve)
    )
    if output_path == push_path:
        raise BlinkError("Notification output and push state must use different paths")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        log = NotificationLog(stream, raw)
        log.write("session_started", schema_version=1, mode="push", push_receiver=True, raw=raw)

        async def save(value):
            await save_json(state_path, value)

        def accepted(kind, camera_key, event_id, occurred_at):
            log.write(
                "accepted_event",
                classification=kind,
                camera_key=camera_key,
                event_id=event_id,
                occurred_at=occurred_at,
            )

        def status(value):
            log.write("receiver_status", status=value)
            print(f"Notification receiver: {value}", flush=True)

        receiver = None
        loop = asyncio.get_running_loop()
        reader_added = False
        reason = "failed"
        deadline = asyncio.timeout(duration)
        try:
            receiver = PushReceiver(
                client,
                await load_json(state_path),
                save,
                accepted,
                status,
                on_notification=log.observe,
            )
            if interactive and sys.stdin.isatty():
                loop.add_reader(sys.stdin.fileno(), lambda: log.marker(sys.stdin.readline()))
                reader_added = True
                print("Markers: m + Enter = motion, d = doorbell press, b = baseline.")
            print(f"Logging live notifications to {output}. Ctrl-C to stop.", flush=True)
            async with deadline:
                receiver.start()
                await receiver.wait()
                if receiver.status == "auth_failed":
                    raise BlinkError("Notification receiver requires Blink sign-in again")
                raise BlinkError("Notification receiver stopped unexpectedly")
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
            if receiver:
                await receiver.close()
            with contextlib.suppress(OSError):
                log.write("session_stopped", reason=reason)

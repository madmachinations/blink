"""In-process Blink FCM receiver. No threads, subprocesses or HA dependencies."""

import asyncio
import hashlib
import json
import ssl
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .errors import AuthError, BlinkError
from .push_proto import (
    fields,
    frame,
    integer,
    login_request,
    number,
    read_frame,
    stream_ack,
    string,
)
from .push_registration import ANDROID_PACKAGE, FIREBASE_SENDER, PushRegistration

MAX_RECENT = 1000
HEARTBEAT_SECONDS = 600
HEARTBEAT_TIMEOUT = 60
MAX_EVENT_AGE = 120


def payload_fields(message: dict) -> dict:
    """Flatten the JSON wrappers used by Blink, retaining original app fields."""
    app = {}
    for entry in message.get(7, []):
        data = fields(entry)
        app[string(data, 1)] = string(data, 2)
    payload = {}
    for value in [
        *message.get(21, []),
        *(app.get(key) for key in ("data", "payload", "message", "notification")),
    ]:
        if not value:
            continue
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            payload.update(data)
    payload.update(app)
    envelope_category = string(message, 5)
    if envelope_category in ("media", "motion"):
        payload.setdefault("category", envelope_category)
    return payload


def classify(payload: dict) -> str | None:
    category = payload.get("category", "")
    event_type = payload.get("event_type", "")
    if not isinstance(category, str) or not isinstance(event_type, str):
        return None
    category, event_type = category.strip().lower(), event_type.strip().lower()
    if category == "media" and event_type == "button_press":
        return "doorbell_press"
    if event_type == "pir" or (
        category == "motion" and event_type in ("cloud_motion", "cam2sm_motion")
    ):
        return "camera_motion"
    return None


def resolve_device(client, payload: dict):
    """Prefer IDs; never fall back to a name after a supplied ID fails to match."""
    if "account_id" in payload and str(payload["account_id"]) != str(client.account_id):
        return None
    if "tier" in payload and payload["tier"] != client.auth.state["tier"]:
        return None
    devices = list(client.devices.values())
    for field, attribute in (
        ("network_id", "network_id"),
        ("camera_id", "id"),
        ("device_id", "id"),
    ):
        if field in payload:
            devices = [d for d in devices if str(getattr(d, attribute)) == str(payload[field])]
    if "serial" in payload:
        devices = [d for d in devices if d.serial == payload["serial"]]
    if not any(key in payload for key in ("camera_id", "device_id", "serial")):
        name = payload.get("camera_name")
        if not isinstance(name, str) or not name:
            return None
        devices = [d for d in devices if d.name == name]
    if classify(payload) == "doorbell_press":
        devices = [d for d in devices if d.kind == "doorbell"]
    return devices[0] if len(devices) == 1 else None


class PushLoginRejected(BlinkError):
    """Google rejected the Android transport identity."""


class PushReceiver:
    def __init__(
        self,
        client,
        state: dict,
        save: Callable[[dict], Awaitable[None]],
        on_event: Callable[[str, str, str, str | None], None],
        on_status: Callable[[str], None],
    ):
        self.client = client
        self.state = dict(state)
        self.save = save
        self.on_event = on_event
        self.on_status = on_status
        self.registration = PushRegistration(client.session, self.state, save)
        self.status = "disabled"
        self.last_received_at: str | None = None
        self.accepted_events = 0
        self.rejected_events = 0
        self.receipt_failures = 0
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._heartbeat_ack = asyncio.Event()
        self._receipts: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._stream_id = 0
        self._interval = HEARTBEAT_SECONDS

    def start(self, create_task=asyncio.create_task):
        if self._task is None:
            self._task = create_task(self._run())

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._disconnect()
        self._status("disabled")

    def _status(self, value):
        self.status = value
        self.on_status(value)

    async def _run(self):
        failures = 0
        reset_android = False
        receipts = asyncio.create_task(self._receipt_worker())
        try:
            while True:
                connected_at = None
                try:
                    self._status("registering")
                    await self.registration.ensure(reset_android=reset_android)
                    reset_android = False
                    await self.client.associate_push_token(self.state["fcm_token"])
                    self._status("connecting")
                    await self._connect()
                    connected_at = time.monotonic()
                    # Renew registration daily even when the socket remains healthy.
                    async with asyncio.timeout(86400):
                        await self._listen()
                except AuthError:
                    self._status("auth_failed")
                    return
                except PushLoginRejected:
                    reset_android = True
                    failures += 1
                except (
                    BlinkError,
                    OSError,
                    EOFError,
                    TimeoutError,
                    ValueError,
                    TypeError,
                    KeyError,
                ):
                    if connected_at is not None and time.monotonic() - connected_at >= 60:
                        failures = 0
                    failures += 1
                finally:
                    await self._disconnect()
                self._status("error" if failures >= 5 else "reconnecting")
                await asyncio.sleep(min(300, 2 ** min(failures, 8)))
        finally:
            receipts.cancel()
            await asyncio.gather(receipts, return_exceptions=True)

    async def _connect(self):
        context = await asyncio.to_thread(ssl.create_default_context)
        async with asyncio.timeout(30):
            reader, self._writer = await asyncio.open_connection(
                "mtalk.google.com", 5228, ssl=context, server_hostname="mtalk.google.com"
            )
            self._reader = reader
            await self._send(b"\x29" + frame(2, login_request(self.state)))
            if (await reader.readexactly(1))[0] not in (38, 41):
                raise ValueError("Unsupported MCS version")
            tag, data = await read_frame(reader)
            if tag != 3:
                raise ValueError("Missing MCS login response")
            response = fields(data)
            if response.get(3) and integer(fields(response[3][-1]), 1):
                raise PushLoginRejected("MCS login rejected")
            self._stream_id = integer(response, 5)
            self._interval = HEARTBEAT_SECONDS
            if response.get(7):
                interval = integer(fields(response[7][-1]), 3)
                if interval > 0:
                    self._interval = max(1, min(HEARTBEAT_SECONDS, interval / 1000))
            self._heartbeat_ack.clear()
            self._status("connected")

    async def _disconnect(self):
        writer, self._writer = self._writer, None
        if writer:
            writer.close()
            try:
                async with asyncio.timeout(5):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def _send(self, data: bytes):
        async with self._write_lock:
            if self._writer is None:
                raise BlinkError("MCS disconnected")
            self._writer.write(data)
            async with asyncio.timeout(30):
                await self._writer.drain()

    async def _listen(self):
        tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._heartbeat())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self._interval)
            self._heartbeat_ack.clear()
            await self._send(frame(0, number(2, self._stream_id)))
            # Only an inbound heartbeat acknowledgement satisfies this deadline.
            async with asyncio.timeout(HEARTBEAT_TIMEOUT):
                await self._heartbeat_ack.wait()

    async def _read(self):
        while True:
            # A peer cannot keep a partial frame alive indefinitely.
            async with asyncio.timeout(self._interval + HEARTBEAT_TIMEOUT + 30):
                tag, data = await read_frame(self._reader)
            if tag == 0:
                await self._send(frame(1, number(2, self._stream_id)))
            elif tag == 1:
                self._heartbeat_ack.set()
            elif tag in (4, 10):
                raise BlinkError("MCS closed the connection")
            elif tag == 8:
                message = fields(data)
                await self._message(message)
                self._stream_id = max(self._stream_id, integer(message, 10))
                await self._send(frame(7, stream_ack(self._stream_id)))

    async def _message(self, message: dict):
        """Persist deduplication before publishing or acknowledging an event.

        This intentionally favors no replay over exactly-once delivery: a process
        crash between the durable checkpoint and HA dispatch can lose one event.
        """
        try:
            payload = payload_fields(message)
        except (ValueError, TypeError, UnicodeError):
            payload = {}
        identity = string(message, 9) or string(message, 2) or payload.get("unique_id")
        if not isinstance(identity, str) or not identity or len(identity) > 4096:
            self.rejected_events += 1
            return
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        recent = self.state.get("event_ids", [])
        duplicate = event_id in recent
        persistent_id = string(message, 9)
        if len(persistent_id) > 4096:
            self.rejected_events += 1
            return
        ids = self.state.get("persistent_ids", [])
        # Work on a copy so failed persistence cannot suppress a redelivery.
        updated = {
            **self.state,
            "event_ids": list(dict.fromkeys([*recent, event_id]))[-MAX_RECENT:],
            "persistent_ids": list(
                dict.fromkeys([*ids, *([persistent_id] if persistent_id else [])])
            )[-MAX_RECENT:],
        }
        if not duplicate or (persistent_id and persistent_id not in ids):
            await self.save(updated)
            self.state.update(updated)
        if not self._receipts.full():
            self._receipts.put_nowait(
                {
                    key: payload[key]
                    for key in ("return_receipt", "unique_id", "account_id", "tier")
                    if key in payload
                }
            )
        if duplicate:
            return
        now = time.time()
        sent = integer(message, 18) / 1000
        occurred_at = None
        if not sent and isinstance(payload.get("created_at"), str):
            try:
                parsed = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
                if parsed.tzinfo:
                    sent = parsed.timestamp()
            except ValueError:
                pass
        classification = classify(payload)
        device = resolve_device(self.client, payload)
        sender = string(message, 3)
        package = string(message, 5)
        if (
            not classification
            or device is None
            or (sender and sender != FIREBASE_SENDER)
            or (package and package not in (ANDROID_PACKAGE, "media", "motion"))
            or not sent
            or not -30 <= now - sent <= MAX_EVENT_AGE
        ):
            self.rejected_events += 1
            return
        occurred_at = datetime.fromtimestamp(sent, UTC).isoformat()
        self.last_received_at = datetime.fromtimestamp(now, UTC).isoformat()
        self.accepted_events += 1
        self.on_event(classification, device.key, event_id, occurred_at)

    async def _receipt_worker(self):
        while True:
            payload = await self._receipts.get()
            try:
                for attempt in range(3):
                    try:
                        await self.client.acknowledge_push(payload)
                        break
                    except AuthError:
                        self._status("auth_failed")
                        break
                    except (BlinkError, OSError, ValueError, TypeError, KeyError):
                        if attempt == 2:
                            self.receipt_failures += 1
                        else:
                            await asyncio.sleep(2**attempt)
            finally:
                self._receipts.task_done()

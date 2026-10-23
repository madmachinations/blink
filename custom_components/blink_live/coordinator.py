"""HA adapter for discovery, recording state, snapshots, and event dispatch."""

import asyncio
import logging
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import CoreState, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util import json as json_util
from homeassistant.util.file import WriteError

from .blink_live_client import AuthError, BlinkClient, BlinkError, VerificationRequired
from .blink_live_client.gateway import StreamGateway
from .blink_live_client.push import PushReceiver
from .blink_live_client.recordings import Recordings
from .blink_live_client.rtsp import RtspServer
from .const import DEFAULT_OPTIONS, DOMAIN, doorbell_press_signal, motion_signal, push_signal
from .http_events import HttpEventDispatcher

_LOGGER = logging.getLogger(__name__)


class PushStore(Store):
    """Unlike ordinary HA state, a push checkpoint must fail on a failed write."""

    async def async_save(self, data):
        if self.hass.state is CoreState.stopping:
            raise OSError("Push checkpoint refused during shutdown")
        await super().async_save(data)

    async def _async_write_data(self, data):
        try:
            await super()._async_write_data(data)
        except (WriteError, json_util.SerializationError):
            # Store normally logs and swallows these errors. Propagate a fixed
            # error so the receiver cannot publish/ack an unpersisted event.
            raise OSError("Could not persist Blink push checkpoint") from None


class BlinkLiveCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self.options = {**DEFAULT_OPTIONS, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.options["poll_interval"]),
        )
        self.client = BlinkClient(async_get_clientsession(hass), dict(entry.data), self._save_auth)
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.recordings")
        self.recordings: Recordings | None = None
        self.gateway = StreamGateway(
            self.client,
            get_ffmpeg_manager(hass).binary,
            self.options["stream_duration"],
        )
        self.rtsp = None
        self.http_events = HttpEventDispatcher(
            hass, entry, self.client, async_get_clientsession(hass), self.options
        )
        self.snapshots: dict[str, bytes] = {}
        self.snapshot_cached_at: dict[str, str] = {}
        self._thumbnail_urls: dict[str, str] = {}
        self._snapshot_locks: dict[str, asyncio.Lock] = {}
        self._failures = 0
        self.clip_error: str | None = None
        self.motion_backend = "blink_push" if self.options["live_events"] else "recording"
        self.push: PushReceiver | None = None
        self.push_store = PushStore(
            hass, 1, f"{DOMAIN}.{entry.entry_id}.push", private=True, atomic_writes=True
        )
        self.motion_ready = False
        self.motion_active: set[str] = set()
        self.motion_last: dict[str, dict] = {}
        self.ring_on_motion: dict[str, bool] = {}
        self._motion_timers: dict = {}
        self._motion_seen: OrderedDict = OrderedDict()

    async def _save_auth(self, state):
        self.hass.config_entries.async_update_entry(self.entry, data=dict(state))

    async def start_push(self):
        """Start only after entities and their event listeners are ready."""
        if not self.options["live_events"]:
            ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_live_events")
            return
        self.push = PushReceiver(
            self.client,
            await self.push_store.async_load() or {},
            self.push_store.async_save,
            self._push_event,
            self._push_status,
        )
        self.push.start(
            lambda coro: self.entry.async_create_background_task(
                self.hass, coro, "Blink live events"
            )
        )

    @callback
    def _push_status(self, status):
        issue_id = f"{self.entry.entry_id}_live_events"
        if status in ("error", "auth_failed"):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="live_events_failed",
                translation_placeholders={"status": status},
            )
            if status == "auth_failed":
                self.entry.async_start_reauth(self.hass)
        elif status in ("connected", "disabled"):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        async_dispatcher_send(self.hass, push_signal(self.entry.entry_id))

    @callback
    def _push_event(self, classification, camera_key, event_id, occurred_at):
        if not self.motion_ready:
            return
        if classification == "camera_motion":
            self.async_report_motion(
                camera_key, source="blink_push", event_id=event_id, occurred_at=occurred_at
            )
        elif classification == "doorbell_press":
            self.async_report_doorbell_press(
                camera_key, source="blink_push", event_id=event_id, occurred_at=occurred_at
            )
        async_dispatcher_send(self.hass, push_signal(self.entry.entry_id))

    async def initialize(self):
        try:
            await self.client.initialize()
        except (AuthError, VerificationRequired):
            raise ConfigEntryAuthFailed("Blink Live requires sign-in or verification") from None
        directory = self.options["archive_directory"]
        if directory and not self.hass.config.is_allowed_path(directory):
            raise BlinkError("Archive directory is not allowed by Home Assistant")
        self.recordings = Recordings(
            self.client,
            await self.store.async_load() or {},
            self.store.async_save,
            self._notify,
            Path(directory) if directory else None,
            self.options["automatic_downloads"],
            self.options["retention_days"],
            self.options["recording_source"],
        )
        await self.gateway.start()
        if self.options["rtsp_enabled"]:
            self.rtsp = RtspServer(
                self.gateway,
                self.options["rtsp_host"],
                self.options["rtsp_port"],
                self.options["rtsp_username"],
                self.options["rtsp_password"],
            )
            try:
                await self.rtsp.start()
            except BlinkError:
                raise ConfigEntryNotReady(
                    "Blink Live RTSP listener failed. Check the listen address and "
                    "choose an unused TCP port in the RTSP options, or disable RTSP."
                ) from None

    async def _notify(self, kind, data):
        device = self.client.devices.get(data["camera_key"])
        data = {
            **data,
            "entry_id": self.entry.entry_id,
            "camera_id": device.id if device else None,
            "camera_name": device.name if device else None,
        }
        self.hass.bus.async_fire(f"{DOMAIN}_{kind}", data)
        async_dispatcher_send(self.hass, f"{DOMAIN}_{self.entry.entry_id}_{kind}", data)
        if kind == "clip_available":
            self.async_report_motion(
                data["camera_key"],
                source="recording",
                event_id=f"{data.get('source', 'usb')}:{data['clip_id']}",
                occurred_at=data.get("recorded_at"),
            )

    @callback
    def async_report_motion(
        self,
        camera_key: str,
        *,
        source: str,
        event_id: str,
        occurred_at: str | None = None,
    ) -> None:
        """Common motion input for recording inference and direct Blink push.

        Each backend supplies a stable event ID. Push receivers must additionally
        persist deduplication across restarts before enabling live delivery.
        """
        device = self.client.devices.get(camera_key)
        if device is None:
            raise HomeAssistantError("Blink Live camera is no longer available")
        if source not in ("recording", "blink_push") or not event_id:
            raise HomeAssistantError("Invalid Blink Live motion report")
        if not self.motion_ready or source != self.motion_backend:
            return
        identity = (camera_key, source, event_id)
        if identity in self._motion_seen:
            return
        self._motion_seen[identity] = None
        if len(self._motion_seen) > 1024:
            self._motion_seen.popitem(last=False)
        data = {
            "entry_id": self.entry.entry_id,
            "account_id": self.client.account_id,
            "network_id": device.network_id,
            "camera_id": device.id,
            "camera_key": camera_key,
            "source": source,
            "event_id": event_id,
            "inferred": source == "recording",
            "occurred_at": occurred_at,
            "observed_at": dt_util.utcnow().isoformat(),
        }
        self.motion_last[camera_key] = data
        self.motion_active.add(camera_key)
        if cancel := self._motion_timers.pop(camera_key, None):
            cancel()

        @callback
        def clear(_now):
            self._motion_timers.pop(camera_key, None)
            self.motion_active.discard(camera_key)
            async_dispatcher_send(self.hass, motion_signal(self.entry.entry_id), camera_key)
            cleared = {
                **self.motion_last[camera_key],
                "event_id": uuid4().hex,
                "observed_at": dt_util.utcnow().isoformat(),
            }
            self.hass.bus.async_fire(f"{DOMAIN}_motion_cleared", cleared)
            self.http_events.emit("motion_cleared", cleared)

        self._motion_timers[camera_key] = async_call_later(
            self.hass,
            self.options["live_motion_timeout" if source == "blink_push" else "motion_timeout"],
            clear,
        )
        async_dispatcher_send(self.hass, motion_signal(self.entry.entry_id), camera_key)
        self.hass.bus.async_fire(f"{DOMAIN}_motion_detected", data)
        self.http_events.emit("motion_detected", data)
        if device.kind == "doorbell" and self.ring_on_motion.get(camera_key, False):
            self.async_report_doorbell_press(camera_key, source="motion", motion_data=data)

    @callback
    def async_report_doorbell_press(
        self,
        camera_key: str,
        *,
        source: str = "manual",
        motion_data: dict | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> None:
        """Common input for the local button and the direct push receiver."""
        device = self.client.devices.get(camera_key)
        if device is None or device.kind != "doorbell":
            raise HomeAssistantError("Blink Live doorbell is no longer available")
        if source not in ("manual", "blink_push", "motion"):
            raise HomeAssistantError("Unknown Blink Live doorbell event source")
        data = {
            "entry_id": self.entry.entry_id,
            "account_id": self.client.account_id,
            "network_id": device.network_id,
            "camera_id": device.id,
            "camera_key": camera_key,
            "source": source,
            "event_id": event_id or uuid4().hex,
            "occurred_at": occurred_at or dt_util.utcnow().isoformat(),
            "observed_at": dt_util.utcnow().isoformat(),
        }
        if source == "motion" and motion_data:
            data.update(
                motion_source=motion_data["source"],
                motion_event_id=motion_data["event_id"],
                inferred=motion_data["inferred"],
                motion_occurred_at=motion_data["occurred_at"],
                observed_at=motion_data["observed_at"],
            )
        async_dispatcher_send(
            self.hass, doorbell_press_signal(self.entry.entry_id), camera_key, data
        )
        self.hass.bus.async_fire(f"{DOMAIN}_doorbell_pressed", data)
        self.http_events.emit("doorbell_pressed", data)

    async def _async_update_data(self):
        try:
            await self.client.discover()
            await self._refresh_snapshots()
        except AuthError:
            raise ConfigEntryAuthFailed("Blink Live authorization expired") from None
        except (BlinkError, ValueError, TypeError, KeyError):
            raise UpdateFailed("Blink device refresh failed") from None
        try:
            await self.recordings.refresh()
            self.clip_error = None
            self._failures = 0
        except AuthError:
            raise ConfigEntryAuthFailed("Blink Live authorization expired") from None
        except (BlinkError, OSError, ValueError, TypeError, KeyError):
            # Recording unavailability must not disable live cameras.
            self.clip_error = "Recording refresh or archive operation failed"
            self._failures += 1
            if self._failures == 1:
                _LOGGER.warning("Blink Live recording refresh or archive operation failed")
        self.update_interval = timedelta(
            seconds=max(
                self.options["poll_interval"],
                min(
                    900,
                    self.options["poll_interval"] * 2 ** min(self._failures, 4),
                ),
            ),
        )
        return dict(self.client.devices)

    async def _refresh_snapshots(self):
        for camera_key in tuple(self.client.devices):
            async with self._snapshot_locks.setdefault(camera_key, asyncio.Lock()):
                device = self.client.devices.get(camera_key)
                if device is None:
                    continue
                try:
                    url = self.client.thumbnail_path(device)
                    if not url or self._thumbnail_urls.get(device.key) == url:
                        continue
                    image = await self.client.resource(url, 4 * 1024 * 1024)
                    if image.startswith(b"\xff\xd8"):
                        self.snapshots[device.key] = image
                        self._thumbnail_urls[device.key] = url
                        self.snapshot_cached_at[device.key] = dt_util.utcnow().isoformat()
                except AuthError:
                    raise
                except BlinkError:
                    continue

    async def async_take_snapshot(self, camera_key: str):
        async with self._snapshot_locks.setdefault(camera_key, asyncio.Lock()):
            device = self.client.devices.get(camera_key)
            if device is None:
                raise BlinkError("Camera is no longer available")
            image = await self.client.capture_snapshot(device)
            self.snapshots[camera_key] = image
            self.snapshot_cached_at[camera_key] = dt_util.utcnow().isoformat()
            # Reconcile once more on the next poll in case metadata changed mid-download.
            self._thumbnail_urls.pop(camera_key, None)
            self.async_set_updated_data(dict(self.client.devices))

    async def close(self):
        self.motion_ready = False
        if self.push:
            await self.push.close()
        for cancel in self._motion_timers.values():
            cancel()
        self._motion_timers.clear()
        self.motion_active.clear()
        await self.http_events.close()
        if self.rtsp:
            await self.rtsp.close()
        await self.gateway.close()

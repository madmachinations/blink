"""Separate recording and doorbell press events, including explicitly requested local rings."""

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, doorbell_press_signal
from .entity import BlinkLiveEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()

    @callback
    def add_devices():
        entities = []
        for key, device in coordinator.client.devices.items():
            if key in known:
                continue
            known.add(key)
            entities.append(BlinkRecordingEvent(coordinator, device))
            if device.kind == "doorbell":
                entities.append(BlinkDoorbellPress(coordinator, device))
        async_add_entities(entities)

    add_devices()
    entry.async_on_unload(coordinator.async_add_listener(add_devices))


class BlinkRecordingEvent(BlinkLiveEntity, EventEntity):
    _attr_name = "Recording"
    _attr_event_types = ["clip_available", "clip_saved"]

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "recording")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        for kind in self._attr_event_types:
            signal = f"{DOMAIN}_{self.coordinator.entry.entry_id}_{kind}"
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal,
                    self._handler(kind),
                )
            )

    def _handler(self, kind):
        @callback
        def handle(data):
            if data["camera_key"] == self.device_key:
                self._trigger_event(kind, data)
                self.async_write_ha_state()

        return handle


class BlinkDoorbellPress(BlinkLiveEntity, EventEntity):
    """Doorbell reports from a local button or explicitly enabled motion routing.

    No timestamp is restored or generated on startup. A future verified push
    receiver can use the same per-account signal with its own source metadata.
    """

    _attr_name = "Press"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["pressed", "ring"]

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "press")

    async def async_get_last_event_data(self):
        # Never restore a historical physical press as a current event.
        return None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                doorbell_press_signal(self.coordinator.entry.entry_id),
                self._handle_press,
            )
        )

    @callback
    def _handle_press(self, camera_key, attributes):
        if camera_key != self.device_key or self.device is None:
            return
        self._trigger_event("ring", attributes)
        self.async_write_ha_state()

    @property
    def available(self):
        return self.device is not None

    @property
    def extra_state_attributes(self):
        return {"physical_press_detection": False}

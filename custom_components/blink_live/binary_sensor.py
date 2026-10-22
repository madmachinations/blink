"""Motion sensors backed by the shared motion reporter, independently of its input."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import motion_signal, push_signal
from .entity import BlinkLiveEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()
    if coordinator.options["live_events"]:
        async_add_entities([BlinkPushConnection(coordinator)])

    @callback
    def add_devices():
        entities = []
        for key, device in coordinator.client.devices.items():
            if key not in known:
                known.add(key)
                entities.append(BlinkMotionSensor(coordinator, device))
        async_add_entities(entities)

    add_devices()
    entry.async_on_unload(coordinator.async_add_listener(add_devices))


class BlinkMotionSensor(BlinkLiveEntity, BinarySensorEntity):
    _attr_name = "Motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "motion")

    @property
    def is_on(self):
        return self.device_key in self.coordinator.motion_active

    @property
    def extra_state_attributes(self):
        last = self.coordinator.motion_last.get(self.device_key, {})
        return {
            "detection_source": last.get("source", self.coordinator.motion_backend),
            "inferred": last.get("inferred", self.coordinator.motion_backend == "recording"),
            "occurred_at": last.get("occurred_at"),
            "observed_at": last.get("observed_at"),
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        @callback
        def update(camera_key):
            if camera_key == self.device_key:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, motion_signal(self.coordinator.entry.entry_id), update
            )
        )


class BlinkPushConnection(BinarySensorEntity):
    """Account-level status independent of device polling and camera availability."""

    _attr_name = "Blink Live events connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.client.account_id}_push_connection"

    @property
    def is_on(self):
        return bool(self.coordinator.push and self.coordinator.push.status == "connected")

    @property
    def extra_state_attributes(self):
        receiver = self.coordinator.push
        return {
            "status": receiver.status if receiver else "starting",
            "last_received_at": receiver.last_received_at if receiver else None,
        }

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                push_signal(self.coordinator.entry.entry_id),
                self.async_write_ha_state,
            )
        )

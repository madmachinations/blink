"""Local doorbell event buttons for dashboards, automations and exposed scene actions."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .entity import BlinkLiveEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()

    @callback
    def add_devices():
        entities = []
        for key, device in coordinator.client.devices.items():
            if key not in known and device.kind == "doorbell":
                known.add(key)
                entities.append(BlinkRingDoorbellButton(coordinator, device))
        async_add_entities(entities)

    add_devices()
    entry.async_on_unload(coordinator.async_add_listener(add_devices))


class BlinkRingDoorbellButton(BlinkLiveEntity, ButtonEntity):
    """Report a press locally; do not operate the physical Blink hardware."""

    _attr_name = "Ring doorbell"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "ring_doorbell")

    @property
    def available(self):
        # Local event forwarding also works during Blink connectivity/USB failures.
        return self.device is not None

    async def async_press(self):
        if self.device is None:
            raise HomeAssistantError("Blink Live doorbell is no longer available")
        self.coordinator.async_report_doorbell_press(self.device_key, source="manual")

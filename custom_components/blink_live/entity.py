"""Common namespaced entity/device identity."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class BlinkLiveEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, device, suffix):
        super().__init__(coordinator)
        self.device_key = device.key
        self._attr_unique_id = f"{coordinator.client.account_id}_{device.key}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.client.account_id}_{device.key}")},
            name=f"Blink Live {device.name}",
            manufacturer="Blink",
            model=device.model,
            serial_number=device.serial,
        )

    @property
    def device(self):
        return self.coordinator.client.devices.get(self.device_key)

    @property
    def available(self):
        return super().available and self.device is not None and self.device.online

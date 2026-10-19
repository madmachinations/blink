"""Blink system arming, per-camera motion settings and optional local ring routing."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .blink_live_client import AuthError, BlinkError
from .const import DOMAIN
from .entity import BlinkLiveEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()
    known_networks = set()

    @callback
    def add_devices():
        entities = []
        for key, device in coordinator.client.devices.items():
            if key not in known:
                known.add(key)
                entities.append(BlinkMotionDetectionSwitch(coordinator, device))
                if device.kind == "doorbell":
                    entities.append(BlinkRingOnMotionSwitch(coordinator, device))
        for network_id, network in coordinator.client.networks.items():
            if network_id not in known_networks:
                known_networks.add(network_id)
                entities.append(BlinkSystemArmedSwitch(coordinator, network))
        async_add_entities(entities)

    add_devices()
    entry.async_on_unload(coordinator.async_add_listener(add_devices))


class BlinkSystemArmedSwitch(CoordinatorEntity, SwitchEntity):
    """One observed arm/disarm control per Blink network; never restored on startup."""

    _attr_has_entity_name = True
    _attr_name = "Armed"
    _attr_icon = "mdi:shield-home"

    def __init__(self, coordinator, network):
        super().__init__(coordinator)
        self.network_id = network.id
        identity = f"{coordinator.client.account_id}_network_{network.id}"
        self._attr_unique_id = f"{identity}_armed"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=f"Blink Live System {network.name}",
            manufacturer="Blink",
            model="Blink system",
        )

    @property
    def network(self):
        return self.coordinator.client.networks.get(self.network_id)

    @property
    def extra_state_attributes(self):
        return {"network_id": self.network_id}

    @property
    def available(self):
        return super().available and self.network is not None

    @property
    def is_on(self):
        return self.network.armed if self.network else None

    async def async_turn_on(self, **kwargs):
        await self._set_armed(True)

    async def async_turn_off(self, **kwargs):
        await self._set_armed(False)

    async def _set_armed(self, armed):
        try:
            await self.coordinator.client.set_network_armed(self.network_id, armed)
        except AuthError:
            self.coordinator.entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Blink Live requires sign-in again") from None
        except (BlinkError, TimeoutError, OSError, ValueError, KeyError, TypeError):
            raise HomeAssistantError("Blink did not confirm the system arm state") from None
        finally:
            self.coordinator.async_set_updated_data(dict(self.coordinator.client.devices))


class BlinkMotionDetectionSwitch(BlinkLiveEntity, SwitchEntity):
    """Blink's actual per-camera enable state, never restored or changed optimistically."""

    _attr_name = "Motion detection"
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "motion_detection")

    @property
    def is_on(self):
        return self.device.motion_enabled if self.device else None

    @property
    def extra_state_attributes(self):
        return {"network_armed": self.device.network_armed if self.device else None}

    async def async_turn_on(self, **kwargs):
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_enabled(False)

    async def _set_enabled(self, enabled):
        if self.device is None:
            raise HomeAssistantError("Blink Live camera is no longer available")
        try:
            await self.coordinator.client.set_motion_enabled(self.device, enabled)
        except AuthError:
            self.coordinator.entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Blink Live requires sign-in again") from None
        except (BlinkError, TimeoutError, OSError, ValueError, KeyError, TypeError):
            raise HomeAssistantError("Blink did not confirm the camera motion setting") from None
        finally:
            # Publish only state observed from Blink; do not trigger clip/motion reconciliation.
            self.coordinator.async_set_updated_data(dict(self.coordinator.client.devices))


class BlinkRingOnMotionSwitch(BlinkLiveEntity, SwitchEntity, RestoreEntity):
    _attr_name = "Ring on motion"
    _attr_icon = "mdi:bell-cog"

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device, "ring_on_motion")

    @property
    def available(self):
        return self.device is not None

    @property
    def is_on(self):
        return self.coordinator.ring_on_motion.get(self.device_key, False)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        self.coordinator.ring_on_motion[self.device_key] = (
            previous is not None and previous.state == STATE_ON
        )

    async def async_turn_on(self, **kwargs):
        self.coordinator.ring_on_motion[self.device_key] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self.coordinator.ring_on_motion[self.device_key] = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        await super().async_will_remove_from_hass()
        self.coordinator.ring_on_motion.pop(self.device_key, None)

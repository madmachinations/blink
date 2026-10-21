"""Live camera entities with cached snapshots and a reusable lazy stream source."""

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import callback

from .entity import BlinkLiveEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()

    @callback
    def add_devices():
        entities = []
        for key, device in coordinator.client.devices.items():
            if key not in known:
                known.add(key)
                entities.append(BlinkLiveCamera(coordinator, device))
        async_add_entities(entities)

    add_devices()
    entry.async_on_unload(coordinator.async_add_listener(add_devices))


class BlinkLiveCamera(BlinkLiveEntity, Camera):
    _attr_name = None
    _attr_brand = "Blink"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, device):
        Camera.__init__(self)
        BlinkLiveEntity.__init__(self, coordinator, device, "camera")
        self._attr_model = device.model

    async def stream_source(self):
        if self.device is None or not self.device.online:
            return None
        return self.coordinator.gateway.url(self.device)

    async def async_camera_image(self, width=None, height=None):
        # HomeKit thumbnails must not wake battery-powered cameras.
        return self.coordinator.snapshots.get(self.device_key)

    @property
    def extra_state_attributes(self):
        attributes = {
            "snapshot_cached_at": self.coordinator.snapshot_cached_at.get(self.device_key)
        }
        if self.coordinator.options.get("rtsp_enabled"):
            # The host is deliberately a placeholder: a wildcard/container bind
            # address is not necessarily reachable by the RTSP consumer.
            port = self.coordinator.options["rtsp_port"]
            attributes["rtsp_url"] = f"rtsp://HA-HOST:{port}/{self.device_key}"
        return attributes

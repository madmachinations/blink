"""Redacted, aggregate diagnostics; no account names, URLs, tokens or filesystem paths."""

from homeassistant.loader import async_get_integration

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    return {
        "version": (await async_get_integration(hass, DOMAIN)).version,
        "device_count": len(coordinator.client.devices),
        "device_kinds": sorted({device.kind for device in coordinator.client.devices.values()}),
        "sync_module_count": len(coordinator.client.modules),
        "usb_active_count": sum(
            module.local_storage for module in coordinator.client.modules.values()
        ),
        "known_clip_count": len(coordinator.recordings.state["seen"]),
        "archive_count": len(coordinator.recordings.state["archive"]),
        "pending_download_count": len(coordinator.recordings.state["pending"]),
        "clip_error": coordinator.clip_error,
        "push_supported": False,
        "options": {
            key: value for key, value in coordinator.options.items() if key != "archive_directory"
        },
    }

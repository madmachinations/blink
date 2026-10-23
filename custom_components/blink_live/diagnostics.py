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
        "push_supported": True,
        "push_hardware_validation_required": True,
        "push": {
            "status": coordinator.push.status if coordinator.push else "disabled",
            "last_received_at": coordinator.push.last_received_at if coordinator.push else None,
            "accepted_events": coordinator.push.accepted_events if coordinator.push else 0,
            "rejected_events": coordinator.push.rejected_events if coordinator.push else 0,
            "receipt_failures": coordinator.push.receipt_failures if coordinator.push else 0,
        },
        "options": {
            key: value
            for key, value in coordinator.options.items()
            if key
            in {
                "poll_interval",
                "stream_duration",
                "automatic_downloads",
                "retention_days",
                "recording_source",
                "motion_timeout",
                "live_events",
                "live_motion_timeout",
                "rtsp_enabled",
                "rtsp_port",
                "http_events_enabled",
            }
        },
        "http_rule_count": len(coordinator.options.get("http_event_rules", [])),
        "http_delivery_status": dict(coordinator.http_events.results),
    }

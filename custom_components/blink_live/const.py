"""Blink Live constants; keep the domain distinct from the built-in Blink integration."""

from homeassistant.const import Platform

DOMAIN = "blink_live"
PLATFORMS = [
    Platform.CAMERA,
    Platform.EVENT,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]


def doorbell_press_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_doorbell_press"


def motion_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_motion"


def push_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_push"


DEFAULT_OPTIONS = {
    "live_events": True,
    "live_motion_timeout": 15,
    "poll_interval": 60,
    "stream_duration": 60,
    "automatic_downloads": False,
    "archive_directory": "",
    "retention_days": 30,
    "recording_source": "usb",
    "motion_timeout": 60,
    "rtsp_enabled": False,
    "rtsp_host": "0.0.0.0",
    "rtsp_port": 8554,
    "rtsp_username": "",
    "rtsp_password": "",
    "http_events_enabled": False,
    "http_event_rules": [],
}

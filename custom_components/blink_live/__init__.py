"""Blink Live uses separate identity, authentication and services from official Blink."""

from pathlib import Path

import probatio as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .blink_live_client import AuthError, BlinkError
from .const import DOMAIN, PLATFORMS
from .coordinator import BlinkLiveCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {})
    if not hass.services.has_service(DOMAIN, "refresh_clips"):

        async def refresh(call):
            runtimes = hass.data[DOMAIN]
            entry_id = call.data.get("entry_id")
            targets = [runtimes[entry_id]] if entry_id in runtimes else list(runtimes.values())
            if entry_id and entry_id not in runtimes:
                raise ServiceValidationError("Unknown Blink Live configuration entry")
            for coordinator in targets:
                await coordinator.async_request_refresh()
                if not coordinator.last_update_success:
                    raise HomeAssistantError("Blink Live refresh failed")
                if coordinator.clip_error:
                    raise HomeAssistantError(coordinator.clip_error)

        async def download(call):
            entity = er.async_get(hass).async_get(call.data["entity_id"])
            if not entity or entity.platform != DOMAIN or entity.domain != "camera":
                raise ServiceValidationError("Select a Blink Live camera entity")
            coordinator = hass.data[DOMAIN].get(entity.config_entry_id)
            if coordinator is None:
                raise ServiceValidationError("Blink Live camera is not loaded")
            key = call.data["clip_id"]
            clip = coordinator.recordings.clips.get(key)
            if not clip or entity.unique_id != (
                f"{coordinator.client.account_id}_{clip.camera_key}_camera"
            ):
                raise ServiceValidationError("Clip does not belong to the selected camera")
            directory = call.data.get("directory") or coordinator.options["archive_directory"]
            if (
                not directory
                or not Path(directory).is_absolute()
                or not hass.config.is_allowed_path(directory)
            ):
                raise ServiceValidationError("Provide an allowed absolute archive directory")
            try:
                path = await coordinator.recordings.download(key, Path(directory))
            except (BlinkError, OSError):
                raise HomeAssistantError("Blink recording download failed") from None
            return {"path": str(path), "clip_id": key}

        async def take_snapshot(call):
            entity = er.async_get(hass).async_get(call.data["entity_id"])
            if not entity or entity.platform != DOMAIN or entity.domain != "camera":
                raise ServiceValidationError("Select a Blink Live camera entity")
            coordinator = hass.data[DOMAIN].get(entity.config_entry_id)
            if coordinator is None:
                raise ServiceValidationError("Blink Live camera is not loaded")
            matches = [
                device.key
                for device in coordinator.client.devices.values()
                if entity.unique_id == f"{coordinator.client.account_id}_{device.key}_camera"
            ]
            if len(matches) != 1:
                raise ServiceValidationError("Blink Live camera is no longer available")
            try:
                await coordinator.async_take_snapshot(matches[0])
            except AuthError:
                coordinator.entry.async_start_reauth(hass)
                raise HomeAssistantError("Blink Live requires sign-in again") from None
            except (BlinkError, TimeoutError, OSError, ValueError, KeyError, TypeError):
                raise HomeAssistantError(
                    "Blink could not capture and retrieve a snapshot"
                ) from None

        hass.services.async_register(
            DOMAIN,
            "refresh_clips",
            refresh,
            schema=vol.Schema({vol.Optional("entry_id"): str}),
        )
        hass.services.async_register(
            DOMAIN,
            "take_snapshot",
            take_snapshot,
            schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}),
        )
        hass.services.async_register(
            DOMAIN,
            "download_clip",
            download,
            schema=vol.Schema(
                {
                    vol.Required("entity_id"): cv.entity_id,
                    vol.Required("clip_id"): str,
                    vol.Optional("directory"): str,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    return True


async def async_setup_entry(hass, entry):
    coordinator = BlinkLiveCoordinator(hass, entry)
    try:
        await coordinator.initialize()
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # Initial reconciliation and restored switch state must never replay rings.
        coordinator.motion_ready = True
    except BlinkError:
        await coordinator.close()
        raise ConfigEntryNotReady("Blink Live service unavailable") from None
    except BaseException:
        await coordinator.close()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_options_updated))

    async def stop(event):
        await coordinator.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def _options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()
    await coordinator.close()
    return True

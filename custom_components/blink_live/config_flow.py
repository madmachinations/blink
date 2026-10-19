"""Hosted sign-in, REST verification, and reauthentication."""

import os
from pathlib import Path

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .blink_live_client import AuthError, BlinkClient, BlinkError, VerificationRequired
from .const import DEFAULT_OPTIONS, DOMAIN


class BlinkLiveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.client: BlinkClient | None = None
        self.authorization_url: str | None = None
        self.verification_kind: str | None = None

    async def async_step_user(self, user_input=None):
        errors = {}
        if self.client is None:
            initial = {}
            if self.source == config_entries.SOURCE_REAUTH:
                initial["hardware_id"] = self._get_reauth_entry().data["hardware_id"]
            self.client = BlinkClient(async_get_clientsession(self.hass), initial)
        if self.authorization_url is None:
            self.authorization_url = self.client.auth.begin_login()
        if user_input is not None:
            try:
                await self.client.auth.complete_login(user_input["callback_url"].strip())
                return await self._finish()
            except AuthError:
                errors["base"] = "invalid_auth"
                self.authorization_url = self.client.auth.begin_login()
            except BlinkError:
                errors["base"] = "cannot_connect"
                self.authorization_url = self.client.auth.begin_login()
            except (ValueError, KeyError, TypeError):
                errors["base"] = "invalid_response"
                self.authorization_url = self.client.auth.begin_login()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("callback_url"): str}),
            errors=errors,
            description_placeholders={"authorization_url": self.authorization_url},
        )

    async def _finish(self):
        try:
            await self.client.initialize()
        except VerificationRequired as error:
            self.verification_kind = error.kind
            await self.client.send_pin(error.kind)
            return await self.async_step_verification()
        account_id = str(self.client.account_id)
        await self.async_set_unique_id(account_id)
        if self.source == config_entries.SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=dict(self.client.auth.state),
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Blink Live ({account_id})",
            data=dict(self.client.auth.state),
        )

    async def async_step_verification(self, user_input=None):
        errors = {}
        if user_input:
            try:
                await self.client.verify_pin(self.verification_kind, user_input["pin"])
                return await self._finish()
            except VerificationRequired:
                errors["base"] = "invalid_pin"
            except AuthError:
                return self.async_abort(reason="invalid_auth")
            except BlinkError:
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="verification",
            data_schema=vol.Schema({vol.Required("pin"): str}),
            errors=errors,
            description_placeholders={"kind": self.verification_kind},
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BlinkLiveOptionsFlow()


class BlinkLiveOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        options = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            directory = user_input["archive_directory"].strip()
            if user_input["automatic_downloads"] or directory:
                valid = (
                    bool(directory)
                    and Path(directory).is_absolute()
                    and self.hass.config.is_allowed_path(directory)
                    and await self.hass.async_add_executor_job(_writable_directory, directory)
                )
                if not valid:
                    errors["archive_directory"] = "invalid_directory"
            if not errors:
                return self.async_create_entry(
                    title="", data={**user_input, "archive_directory": directory}
                )
            options.update(user_input)
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("poll_interval", default=options["poll_interval"]): vol.All(
                        vol.Coerce(int), vol.Range(min=30, max=3600)
                    ),
                    vol.Optional("recording_source", default=options["recording_source"]): vol.In(
                        ["usb", "cloud", "all"]
                    ),
                    vol.Optional("motion_timeout", default=options["motion_timeout"]): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=3600)
                    ),
                    vol.Required("stream_duration", default=options["stream_duration"]): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=300)
                    ),
                    vol.Required(
                        "automatic_downloads", default=options["automatic_downloads"]
                    ): bool,
                    vol.Required("archive_directory", default=options["archive_directory"]): str,
                    vol.Required("retention_days", default=options["retention_days"]): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=36500)
                    ),
                }
            ),
        )


def _writable_directory(directory):
    path = Path(directory)
    return path.is_dir() and os.access(path, os.W_OK)

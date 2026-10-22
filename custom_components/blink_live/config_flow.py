"""Hosted sign-in, REST verification, and reauthentication."""

import os
from pathlib import Path
from uuid import uuid4

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .blink_live_client import AuthError, BlinkClient, BlinkError, VerificationRequired
from .blink_live_client.auth import REDIRECT
from .const import DEFAULT_OPTIONS, DOMAIN
from .http_events import EVENT_TYPES, METHODS, validate_rule


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
            description_placeholders={
                "authorization_url": self.authorization_url,
                "callback_url": REDIRECT,
            },
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
        self._pending = {**DEFAULT_OPTIONS, **self.config_entry.options}
        return self.async_show_menu(step_id="init", menu_options=["general", "rtsp", "http_events"])

    def _save(self, changes):
        return self.async_create_entry(title="", data={**self._pending, **changes})

    async def async_step_general(self, user_input=None):
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
                return self._save({**user_input, "archive_directory": directory})
            options.update(user_input)
        return self.async_show_form(
            step_id="general",
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

    async def async_step_rtsp(self, user_input=None):
        options = {**self._pending, **(user_input or {})}
        errors = {}
        if user_input is not None:
            username, password = options["rtsp_username"], options["rtsp_password"]
            if options["rtsp_enabled"] and (
                not username
                or not password
                or ":" in username
                or any(c in username + password for c in "\r\n")
            ):
                errors["base"] = "rtsp_credentials"
            if not options["rtsp_host"].strip():
                errors["rtsp_host"] = "rtsp_host"
            if not errors:
                return self._save({**user_input, "rtsp_host": options["rtsp_host"].strip()})
        return self.async_show_form(
            step_id="rtsp",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("rtsp_enabled", default=options["rtsp_enabled"]): bool,
                    vol.Required("rtsp_host", default=options["rtsp_host"]): str,
                    vol.Required("rtsp_port", default=options["rtsp_port"]): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                    vol.Required("rtsp_username", default=options["rtsp_username"]): str,
                    vol.Required(
                        "rtsp_password", default=options["rtsp_password"]
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )

    async def async_step_http_events(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._pending["http_events_enabled"] = user_input["http_events_enabled"]
            action = user_input["action"]
            if action == "save":
                return self._save({})
            if action == "add":
                if len(self._pending["http_event_rules"]) >= 32:
                    errors["base"] = "rule_limit"
                else:
                    self._editing = None
                    return await self.async_step_http_rule()
            elif not self._pending["http_event_rules"]:
                errors["base"] = "no_rules"
            else:
                self._rule_action = action
                return await self.async_step_choose_rule()
        return self.async_show_form(
            step_id="http_events",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "http_events_enabled", default=self._pending["http_events_enabled"]
                    ): bool,
                    vol.Required("action", default="save"): vol.In(
                        {
                            "save": "Save",
                            "add": "Add rule",
                            "edit": "Edit rule",
                            "delete": "Delete rule",
                        }
                    ),
                }
            ),
        )

    async def async_step_choose_rule(self, user_input=None):
        rules = self._pending["http_event_rules"]
        if user_input is not None:
            self._editing = user_input["rule"]
            if self._rule_action == "delete":
                return self._save(
                    {"http_event_rules": [rule for rule in rules if rule["id"] != self._editing]}
                )
            return await self.async_step_http_rule()
        return self.async_show_form(
            step_id="choose_rule",
            data_schema=vol.Schema(
                {vol.Required("rule"): vol.In({rule["id"]: rule["name"] for rule in rules})}
            ),
        )

    async def async_step_http_rule(self, user_input=None):
        rules = self._pending["http_event_rules"]
        current = next((rule for rule in rules if rule["id"] == self._editing), {})
        values = {
            "name": "",
            "enabled": True,
            "event_type": "doorbell_pressed",
            "cameras": [],
            "method": "POST",
            "url": "",
            "headers": "{}",
            "body": "",
            **current,
            **(user_input or {}),
        }
        errors = {}
        if user_input is not None:
            # Optional fields are omitted when cleared in the HA form. Do not
            # retain a previous body when the user removes it while editing.
            values["body"] = user_input.get("body", "")
            try:
                validate_rule(self.hass, values)
            except (ValueError, TypeError, TemplateError):
                errors["base"] = "invalid_http_rule"
            else:
                saved = {**values, "id": self._editing or uuid4().hex}
                return self._save(
                    {
                        "http_event_rules": [rule for rule in rules if rule["id"] != self._editing]
                        + [saved]
                    }
                )
        runtime = getattr(self.config_entry, "runtime_data", None)
        devices = runtime.client.devices if runtime else {}
        cameras = {device.key: device.name for device in devices.values()}
        cameras.update({key: cameras.get(key, key) for key in values["cameras"]})
        multiline = selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        return self.async_show_form(
            step_id="http_rule",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=values["name"]): str,
                    vol.Required("enabled", default=values["enabled"]): bool,
                    vol.Required("event_type", default=values["event_type"]): vol.In(EVENT_TYPES),
                    vol.Optional("cameras", default=values["cameras"]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            multiple=True,
                            options=[
                                {"value": key, "label": name} for key, name in cameras.items()
                            ],
                        )
                    ),
                    vol.Required("method", default=values["method"]): vol.In(METHODS),
                    vol.Required("url", default=values["url"]): str,
                    vol.Required("headers", default=values["headers"]): multiline,
                    vol.Optional(
                        "body", description={"suggested_value": values["body"]}
                    ): multiline,
                }
            ),
        )


def _writable_directory(directory):
    path = Path(directory)
    return path.is_dir() and os.access(path, os.W_OK)

"""Generic, best-effort HTTP event rules; no vendor-specific delivery logic."""

import asyncio
import json
import logging
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
EVENT_TYPES = ("doorbell_pressed", "motion_detected", "motion_cleared")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
QUEUE_SIZE = 32
MAX_WORKERS = 32


def validate_rule(hass, rule):
    """Validate editable configuration without rendering or sending a request."""
    if (
        not rule.get("name", "").strip()
        or rule.get("event_type") not in EVENT_TYPES
        or rule.get("method") not in METHODS
        or not isinstance(rule.get("cameras", []), list)
        or not rule.get("url", "").strip()
    ):
        raise ValueError("Invalid HTTP rule")
    headers = json.loads(rule.get("headers", "{}"))
    if not isinstance(headers, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or any(c in key for c in "\r\n:")
        for key, value in headers.items()
    ):
        raise ValueError("Headers must be an object of string values")
    for value in [rule["url"], rule.get("body", ""), *headers.values()]:
        Template(value, hass).ensure_valid()
    return headers


class HttpEventDispatcher:
    def __init__(self, hass, entry, client, session, options):
        self.hass, self.entry, self.client, self.session = hass, entry, client, session
        self.enabled = options["http_events_enabled"]
        self.rules = []
        if self.enabled:
            for rule in options["http_event_rules"]:
                self.rules.append((dict(rule), validate_rule(hass, rule)))
        self.workers = {}
        self.closed = False
        self.results = {}

    def emit(self, kind, data):
        if not self.enabled or self.closed:
            return
        key = data["camera_key"]
        matches = [
            (rule, headers)
            for rule, headers in self.rules
            if rule.get("enabled", True)
            and rule["event_type"] == kind
            and (not rule.get("cameras") or key in rule["cameras"])
        ]
        if not matches:
            return
        if key not in self.workers:
            if len(self.workers) >= MAX_WORKERS:
                self._result("queue", "worker_limit")
                return
            queue = asyncio.Queue(QUEUE_SIZE)
            task = asyncio.create_task(self._run(key, queue))
            self.workers[key] = (queue, task)
        queue, _ = self.workers[key]
        if queue.full():
            self._result("queue", "full")
            return
        # One queue item contains the entire matching rule set, preventing partial
        # admission of an event when the bounded queue has only one slot remaining.
        queue.put_nowait((kind, dict(data), matches))

    def _result(self, rule_id, status):
        self.results[rule_id] = status
        if status != "delivered":
            # IDs/status only: user-defined names, templates and exception strings
            # can all contain endpoint credentials.
            _LOGGER.warning("Blink Live HTTP event delivery failed (%s)", status)

    async def _run(self, key, queue):
        try:
            while True:
                kind, data, matches = await queue.get()
                try:
                    variables = self._variables(kind, data)
                    for rule, headers in matches:
                        await self._send(rule, headers, variables)
                finally:
                    queue.task_done()
                if queue.empty():
                    return
        finally:
            self.workers.pop(key, None)

    def _variables(self, kind, data):
        device = self.client.devices.get(data["camera_key"])
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "camera", DOMAIN, f"{self.client.account_id}_{data['camera_key']}_camera"
        )
        return {
            **data,
            "event": dict(data),
            "event_type": kind,
            "camera_name": device.name if device else "",
            "entity_id": entity_id,
            "motion_active": kind == "motion_detected" if kind.startswith("motion_") else None,
        }

    async def _send(self, rule, headers, variables):
        def render(value):
            return Template(value, self.hass).async_render(
                variables,
                parse_result=False,
                strict=True,
                log_fn=lambda _level, _message: None,
            )

        rule_id = rule["id"]
        try:
            url = render(rule["url"]).strip()
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or any(c in url for c in "\r\n")
            ):
                raise ValueError
            rendered_headers = {key: render(value) for key, value in headers.items()}
            if any(c in value for value in rendered_headers.values() for c in "\r\n"):
                raise ValueError
            body = render(rule.get("body", ""))
            if len(body.encode()) > 65536 or len(url) > 8192:
                raise ValueError
            async with self.session.request(
                rule["method"],
                url,
                headers=rendered_headers,
                data=body.encode() if body else None,
                timeout=ClientTimeout(total=5),
                allow_redirects=False,
            ) as response:
                self._result(
                    rule_id,
                    "delivered" if 200 <= response.status < 300 else f"http_{response.status}",
                )
                # Do not consume or log arbitrary-sized responses.
        except (TemplateError, ValueError, TypeError):
            self._result(rule_id, "invalid_template_or_request")
        except (ClientError, OSError, TimeoutError):
            self._result(rule_id, "connection_failed")

    async def close(self):
        self.closed = True
        tasks = [task for _, task in self.workers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()

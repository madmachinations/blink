"""Stable, camera-scoped URLs serving only the current in-memory thumbnail."""

import hashlib
import hmac
import secrets

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .const import DOMAIN


async def async_setup_snapshots(hass, coordinator):
    """Keep URL credentials separate from Blink's rotating authentication state."""
    store = Store(hass, 1, f"{DOMAIN}.{coordinator.entry.entry_id}.snapshot_url", private=True)
    secret = await store.async_load()
    if secret is None:
        secret = secrets.token_hex(32)
        await store.async_save(secret)
    coordinator.snapshot_url_secret = secret
    if not hass.data.get(f"{DOMAIN}_snapshot_view"):
        hass.http.register_view(BlinkSnapshotView())
        hass.data[f"{DOMAIN}_snapshot_view"] = True


def snapshot_token(coordinator, camera_key):
    """Authorize one camera, without exposing the secret used for other cameras."""
    return hmac.new(
        coordinator.snapshot_url_secret.encode(), camera_key.encode(), hashlib.sha256
    ).hexdigest()


def snapshot_url(coordinator, camera_key):
    """Use HA's configured address, with a replaceable host when unavailable."""
    try:
        base = get_url(coordinator.hass, prefer_external=False)
    except NoURLAvailableError:
        base = "http://HA-HOST:8123"
    return (
        f"{base}/api/blink_live/snapshot/{coordinator.entry.entry_id}/{camera_key}"
        f"?token={snapshot_token(coordinator, camera_key)}"
    )


class BlinkSnapshotView(HomeAssistantView):
    url = "/api/blink_live/snapshot/{entry_id}/{camera_key}"
    name = "api:blink_live:snapshot"
    # URL-only consumers authenticate using the persistent camera-scoped token.
    requires_auth = False

    async def get(self, request, entry_id, camera_key):
        coordinator = request.app["hass"].data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None or camera_key not in coordinator.client.devices:
            raise web.HTTPNotFound()
        token = request.query.get("token", "")
        expected = snapshot_token(coordinator, camera_key)
        if not hmac.compare_digest(token.encode(), expected.encode()):
            raise web.HTTPUnauthorized()
        image = coordinator.snapshots.get(camera_key)
        if not image:
            raise web.HTTPNotFound()
        return web.Response(
            body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

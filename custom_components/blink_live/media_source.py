"""Authenticated playback of indexed local recording copies."""

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)

from .blink_live_client import BlinkError
from .blink_live_client.recordings import archive_file
from .const import DOMAIN


async def async_get_media_source(hass):
    if not hass.data.get(f"{DOMAIN}_media_view"):
        hass.http.register_view(BlinkArchiveView())
        hass.data[f"{DOMAIN}_media_view"] = True
    return BlinkArchiveSource(hass)


class BlinkArchiveSource(MediaSource):
    name = "Blink Live recordings"

    def __init__(self, hass):
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_browse_media(self, item: MediaSourceItem):
        children = []
        for entry_id, coordinator in self.hass.data.get(DOMAIN, {}).items():
            for key, record in coordinator.recordings.state["archive"].items():
                clip = record["clip"]
                device = coordinator.client.devices.get(clip["camera_key"])
                name = device.name if device else clip["camera_key"]
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{entry_id}/{key}",
                        media_class=MediaClass.VIDEO,
                        media_content_type="video/mp4",
                        title=f"{clip['created_at']} — {name}",
                        can_play=True,
                        can_expand=False,
                    )
                )
        children.sort(key=lambda child: child.title, reverse=True)
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            title=self.name,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def async_resolve_media(self, item: MediaSourceItem):
        try:
            entry_id, key = item.identifier.split("/")
            coordinator = self.hass.data[DOMAIN][entry_id]
            record = coordinator.recordings.state["archive"][key]
            path = await self.hass.async_add_executor_job(archive_file, record)
        except (KeyError, ValueError, OSError, BlinkError):
            raise Unresolvable("Blink recording is no longer available") from None
        return PlayMedia(f"/api/blink_live/archive/{entry_id}/{key}", "video/mp4", path=path)


class BlinkArchiveView(HomeAssistantView):
    url = "/api/blink_live/archive/{entry_id}/{key}"
    name = "api:blink_live:archive"
    requires_auth = True

    async def get(self, request, entry_id, key):
        hass = request.app["hass"]
        try:
            record = hass.data[DOMAIN][entry_id].recordings.state["archive"][key]
            path = await hass.async_add_executor_job(archive_file, record)
        except (KeyError, OSError, BlinkError):
            raise web.HTTPNotFound() from None
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

"""Blink REST operations. No Home Assistant or blinkpy dependency."""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

from .auth import USER_AGENT, Auth, SaveTokens
from .errors import ApiError, AuthError, BlinkError, VerificationRequired
from .models import Clip, Device, Network, SyncModule

MAX_JSON = 8 * 1024 * 1024


def remote_id(value) -> str:
    text = str(value)
    if not re.fullmatch(r"[0-9]+", text) or len(text) > 20:
        raise BlinkError("Invalid remote identifier")
    return text


def alphanumeric(value: str) -> str:
    return "".join(c for c in value if c.isalnum()).lower()


class BlinkClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        state: dict | None = None,
        save: SaveTokens | None = None,
    ) -> None:
        self.session = session
        self.auth = Auth(session, state, save)
        self.devices: dict[str, Device] = {}
        self.modules: dict[int, SyncModule] = {}
        self.networks: dict[int, Network] = {}
        self._module_locks: dict[int, asyncio.Lock] = {}
        self._verification_info: dict = {}
        self._device_command_lock = asyncio.Lock()
        # Development diagnostics only; the CLI filters fields before writing them.
        self.observer: Callable[[str, dict], None] | None = None

    @property
    def account_id(self) -> int:
        return int(self.auth.state["account_id"])

    @property
    def root(self) -> str:
        tier = self.auth.state.get("tier", "prod")
        if not re.fullmatch(r"[a-z0-9]{4}", tier):
            raise BlinkError("Invalid Blink region")
        return f"https://rest-{tier}.immedia-semi.com"

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Retry reads on outages; never blindly replay remote command creation."""
        token = await self.auth.token()
        for attempt in range(3):
            try:
                async with self.session.request(
                    method,
                    self.root + path,
                    json=body if method == "POST" else None,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": USER_AGENT,
                        "APP-BUILD": "ANDROID_29715642",
                        "LOCALE": "en_US",
                        "Accept": "application/json",
                        "X-Blink-Time-Zone": "UTC",
                    },
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 401 and attempt == 0:
                        token = await self.auth.token(rejected=token)
                        continue
                    if response.status in (401, 403):
                        raise AuthError("Blink rejected authorization; sign in again")
                    if response.status >= 300:
                        try:
                            retry = min(300, max(0, float(response.headers.get("Retry-After", 0))))
                        except ValueError:
                            retry = 0
                        raise ApiError(response.status, retry)
                    data = await response.content.read(MAX_JSON + 1)
                    if len(data) > MAX_JSON:
                        raise BlinkError("Blink JSON response exceeds size limit")
                    value = json.loads(data) if data else {}
                    if not isinstance(value, dict):
                        raise BlinkError("Invalid Blink response shape")
                    return value
            except ApiError as error:
                if method != "GET" or error.status not in (429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise
                await asyncio.sleep(max(error.retry_after, 2**attempt))
            except (aiohttp.ClientError, TimeoutError):
                if method != "GET" or attempt == 2:
                    raise BlinkError("Blink service unavailable") from None
                await asyncio.sleep(2**attempt)
            except (ValueError, TypeError):
                raise BlinkError("Invalid Blink JSON response") from None
        raise BlinkError("Blink request exhausted retries")

    async def initialize(self) -> dict:
        """Resolve account region, then perform any required REST verification."""
        if not self.auth.state.get("account_id"):
            for tier in ("prod", "prde", "prsg", "a001"):
                self.auth.state["tier"] = tier
                try:
                    info = await self.request("GET", "/api/v1/users/tier_info")
                    self.auth.state.update(
                        tier=info.get("tier", tier),
                        account_id=int(info["account_id"]),
                    )
                    break
                except ApiError as error:
                    if error.status != 406:
                        raise
            else:
                raise BlinkError("Could not discover Blink account region")
        info = await self.request("GET", "/api/v2/users/info")
        self._verification_info = info
        for field in ("account_id", "client_id", "email"):
            if field in info:
                self.auth.state[field] = info[field]
        await self.auth.persist()
        if info.get("client_verification_required"):
            raise VerificationRequired("client")
        if info.get("phone_verification_required") or info.get("account_verification_required"):
            raise VerificationRequired("account")
        await self.discover()
        return info

    async def send_pin(self, kind: str) -> None:
        if kind == "client":
            client_id = remote_id(self.auth.state["client_id"])
            path = f"/api/v5/clients/{client_id}/client_verification/pin/resend"
        elif kind == "account":
            path = "/api/v4/users/pin/resend"
        else:
            raise BlinkError("Unknown verification kind")
        await self.request("POST", path, {})

    async def verify_pin(self, kind: str, pin: str) -> None:
        if not re.fullmatch(r"[0-9]{4,10}", pin):
            raise BlinkError("Invalid verification PIN")
        body = {
            "pin": pin,
            "email": self.auth.state.get("email"),
            "device_identifier": self.auth.state["hardware_id"],
            "client_name": "Blink Live",
        }
        if kind == "client":
            client_id = remote_id(self.auth.state["client_id"])
            if self._verification_info.get("trust_device_enabled", True):
                path = f"/api/v5/clients/{client_id}/client_verification/pin/verify"
                body = {"pin": pin, "trusted": True}
            else:
                path = f"/api/v4/clients/{client_id}/pin/verify"
        elif kind == "account":
            path = "/api/v4/users/pin/verify"
        else:
            raise BlinkError("Unknown verification kind")
        result = await self.request("POST", path, body)
        if result.get("valid") is False or result.get("require_new_pin"):
            raise VerificationRequired(kind)

    async def discover(self) -> None:
        data = await self.request("GET", f"/api/v4/accounts/{self.account_id}/homescreen")
        if self.observer:
            self.observer("homescreen", data)
        devices = {}
        networks = {
            int(remote_id(row["id"])): Network(
                int(remote_id(row["id"])),
                str(row.get("name", f"System {row['id']}")),
                row.get("armed") if type(row.get("armed")) is bool else None,
            )
            for row in data.get("networks", [])
        }
        for group, kind in (("cameras", "camera"), ("doorbells", "doorbell"), ("owls", "owl")):
            for row in data.get(group, []):
                device = Device(
                    id=int(remote_id(row["id"])),
                    network_id=int(remote_id(row["network_id"])),
                    kind=kind,
                    name=str(row["name"]),
                    serial=str(row.get("serial", "")),
                    model=str(row.get("type", kind)),
                    thumbnail=row.get("thumbnail"),
                    # Disabling motion must not make a connected camera unavailable.
                    online=row.get("status") != "offline",
                    motion_enabled=row.get("enabled") if type(row.get("enabled")) is bool else None,
                    network_armed=(
                        networks[int(row["network_id"])].armed
                        if int(row["network_id"]) in networks
                        else None
                    ),
                )
                devices[device.key] = device
        self.devices = devices
        self.networks = networks
        self.modules = {
            int(row["id"]): SyncModule(
                int(remote_id(row["id"])),
                int(remote_id(row["network_id"])),
                str(row.get("name", "Sync Module")),
                row.get("local_storage_status") == "active",
            )
            for row in data.get("sync_modules", [])
        }

    async def set_network_armed(self, network_id: int, armed: bool) -> None:
        """Arm/disarm one Blink system, waiting for command and observed state."""
        network_id = int(remote_id(network_id))
        if type(armed) is not bool:
            raise BlinkError("Invalid Blink system arm state")
        async with self._device_command_lock, asyncio.timeout(120):
            if network_id not in self.networks:
                raise BlinkError("Blink system is no longer available")
            action = "arm" if armed else "disarm"
            path = (
                f"/api/v1/accounts/{remote_id(self.account_id)}"
                f"/networks/{network_id}/state/{action}"
            )
            try:
                response = await self.request("POST", path)
            except ApiError as error:
                if error.status != 409:
                    raise
                await self.discover()
                observed = self.networks.get(network_id)
                if observed is not None and observed.armed is armed:
                    return
                raise
            command_id = response.get("id", response.get("command_id"))
            if command_id is not None:
                await self.wait_command(network_id, int(remote_id(command_id)))
            for attempt in range(4):
                await self.discover()
                observed = self.networks.get(network_id)
                if observed is not None and observed.armed is armed:
                    return
                if attempt < 3:
                    await asyncio.sleep(2)
            raise BlinkError("Blink did not confirm the system arm state")

    async def set_motion_enabled(self, device: Device, enabled: bool) -> None:
        """Set one camera's motion configuration, then verify Blink's reported state.

        This never arms the entire network. Commands are serialized; an ambiguous
        result is not blindly replayed. Missing readback is a failure, not success.
        """
        if type(enabled) is not bool or device.kind not in ("camera", "doorbell", "owl"):
            raise BlinkError("Unsupported camera motion command")
        async with self._device_command_lock, asyncio.timeout(120):
            current = self.devices.get(device.key)
            if current is None or not current.online:
                raise BlinkError("Camera is unavailable")
            network, identifier = remote_id(device.network_id), remote_id(device.id)
            account = remote_id(self.account_id)
            action = "enable" if enabled else "disable"
            if device.kind == "camera":
                path = f"/accounts/{account}/networks/{network}/cameras/{identifier}/{action}"
                body = None
            elif device.kind == "doorbell":
                path = (
                    f"/api/v1/accounts/{account}/networks/{network}/doorbells/{identifier}/{action}"
                )
                body = None
            else:
                path = f"/api/v1/accounts/{account}/networks/{network}/owls/{identifier}/config"
                body = {"enabled": enabled}
            try:
                response = await self.request("POST", path, body)
            except ApiError as error:
                if error.status != 409:
                    raise
                # Blink may return a conflict when the requested state already holds.
                await self.discover()
                observed = self.devices.get(device.key)
                if observed is not None and observed.motion_enabled is enabled:
                    return
                raise
            command_id = response.get("id", response.get("command_id"))
            if command_id is not None:
                await self.wait_command(device.network_id, int(remote_id(command_id)))
            for attempt in range(4):
                await self.discover()
                observed = self.devices.get(device.key)
                if observed is not None and observed.motion_enabled is enabled:
                    return
                if attempt < 3:
                    await asyncio.sleep(2)
            raise BlinkError("Blink did not confirm the camera motion setting")

    def thumbnail_path(self, device: Device) -> str | None:
        """Resolve Blink's timestamp, extensionless and full image references."""
        reference = device.thumbnail
        if reference in (None, "", 0, "0"):
            return None
        if not isinstance(reference, (str, int)) or isinstance(reference, bool):
            raise BlinkError("Invalid Blink thumbnail reference")
        reference = str(reference)
        if reference.isdecimal():
            stamp = remote_id(reference)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device.model):
                raise BlinkError("Invalid Blink thumbnail device type")
            path = (
                f"/api/v3/media/accounts/{self.account_id}/networks/"
                f"{remote_id(device.network_id)}/{device.model}/{remote_id(device.id)}"
                f"/thumbnail/thumbnail.jpg?ts={stamp}&ext="
            )
        else:
            parts = urlsplit(reference)
            image_path = parts.path
            if not image_path.lower().endswith((".jpg", ".jpeg")):
                image_path += ".jpg"
            path = urlunsplit(parts._replace(path=image_path))
        return self.resource_url(path)

    async def capture_snapshot(self, device: Device) -> bytes:
        """Explicitly wake one camera for a thumbnail, then download it.

        A conflict is an error, never a successful capture of an old thumbnail.
        Without a command ID we require a changed image reference before returning.
        """
        async with self._device_command_lock, asyncio.timeout(120):
            current = self.devices.get(device.key)
            if current is None or not current.online:
                raise BlinkError("Camera is unavailable")
            groups = {"camera": "cameras", "doorbell": "doorbells", "owl": "owls"}
            if device.kind not in groups:
                raise BlinkError("Unsupported camera snapshot command")
            original = self.thumbnail_path(current)
            prefix = "" if device.kind == "camera" else "/api/v1"
            path = (
                f"{prefix}/accounts/{remote_id(self.account_id)}/networks/"
                f"{remote_id(device.network_id)}/{groups[device.kind]}/"
                f"{remote_id(device.id)}/thumbnail"
            )
            response = await self.request("POST", path)
            command_id = response.get("id", response.get("command_id"))
            if command_id is not None:
                await self.wait_command(device.network_id, int(remote_id(command_id)))
            for attempt in range(6):
                await self.discover()
                observed = self.devices.get(device.key)
                if observed is None:
                    raise BlinkError("Camera disappeared during snapshot capture")
                thumbnail = self.thumbnail_path(observed)
                if thumbnail and (command_id is not None or thumbnail != original):
                    try:
                        image = await self.resource(thumbnail, 4 * 1024 * 1024)
                    except ApiError as error:
                        if error.status not in (404, 409):
                            raise
                    else:
                        if image.startswith(b"\xff\xd8"):
                            return image
                if attempt < 5:
                    await asyncio.sleep(2)
            raise BlinkError("Blink did not provide a completed snapshot")

    def device(self, selector: str) -> Device:
        matches = [d for d in self.devices.values() if selector in (d.key, d.serial, d.name)]
        if not matches:
            matches = [d for d in self.devices.values() if selector.casefold() == d.name.casefold()]
        if len(matches) != 1:
            raise BlinkError("Camera not found or ambiguous; use the key shown by devices")
        return matches[0]

    async def liveview(self, device: Device) -> dict:
        if not device.online:
            raise BlinkError("Camera is offline")
        if not device.serial:
            raise BlinkError("Camera has no serial number; IMMIS is unavailable")
        version = "v6" if device.kind == "camera" else "v2"
        group = {"camera": "cameras", "owl": "owls", "doorbell": "doorbells"}[device.kind]
        return await self.request(
            "POST",
            f"/api/{version}/accounts/{self.account_id}/networks/"
            f"{device.network_id}/{group}/{device.id}/liveview",
            {"intent": "liveview", "motion_event_start_time": None},
        )

    async def command(self, network_id: int, command_id: int) -> dict:
        return await self.request(
            "GET",
            f"/accounts/{self.account_id}/networks/"
            f"{remote_id(network_id)}/commands/{remote_id(command_id)}",
        )

    async def wait_command(self, network_id: int, command_id: int) -> None:
        async with asyncio.timeout(90):
            while True:
                status = await self.command(network_id, command_id)
                if status.get("status") == "failed":
                    raise BlinkError("Blink command failed")
                if status.get("complete") or status.get("status") == "complete":
                    return
                await asyncio.sleep(min(10, max(1, float(status.get("polling_interval", 2)))))

    def storage_path(self, module: SyncModule) -> str:
        return (
            f"/api/v1/accounts/{self.account_id}/networks/{module.network_id}"
            f"/sync_modules/{module.id}/local_storage"
        )

    def module_lock(self, module_id: int) -> asyncio.Lock:
        return self._module_locks.setdefault(module_id, asyncio.Lock())

    async def clips(self, module: SyncModule) -> list[Clip]:
        if not module.local_storage:
            return []
        async with self.module_lock(module.id):
            return await self._clips(module)

    async def _clips(self, module: SyncModule) -> list[Clip]:
        base = self.storage_path(module)
        command = await self.request("POST", f"{base}/manifest/request", {})
        command_id = int(remote_id(command["id"]))
        await self.wait_command(module.network_id, command_id)
        # New Android uses /media/{command}; retain the older manifest endpoint for older firmware.
        try:
            data = await self.request("GET", f"{base}/media/{command_id}")
        except ApiError as error:
            if error.status != 404:
                raise
            data = await self.request("GET", f"{base}/manifest/request/{command_id}")
        clips: dict[str, Clip] = {}
        pages: set[str] = set()
        for _ in range(100):
            manifest_id = remote_id(data["manifest_id"])
            rows = data.get("clips", data.get("media", []))
            if not isinstance(rows, list):
                raise BlinkError("Invalid USB manifest")
            if self.observer:
                self.observer("usb_manifest", {"module_id": module.id, "clips": rows})
            for row in rows:
                device = self._clip_device(module, row)
                if device is None:
                    continue  # Never attribute an ambiguous camera name to the wrong camera.
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                clip = Clip(
                    self.account_id,
                    module.network_id,
                    module.id,
                    remote_id(row["id"]),
                    manifest_id,
                    device.key,
                    created.astimezone(UTC).isoformat(),
                    int(row.get("size", 0)),
                )
                clips[clip.key] = clip
            cursor = data.get("pagination_key")
            if cursor in (None, "", False):
                return sorted(clips.values(), key=lambda clip: clip.created_at)
            cursor = str(cursor)
            if cursor in pages:
                raise BlinkError("USB manifest pagination did not advance")
            pages.add(cursor)
            data = await self.request(
                "GET",
                f"{base}/media/{command_id}?{urlencode({'pagination_key': cursor})}",
            )
        raise BlinkError("USB manifest page limit exceeded")

    def _clip_device(self, module: SyncModule, row: dict) -> Device | None:
        candidates = [d for d in self.devices.values() if d.network_id == module.network_id]
        device_id = row.get("camera_id", row.get("device_id", row.get("deviceId")))
        if device_id is not None:
            candidates = [d for d in candidates if d.id == int(device_id)]
        else:
            name = alphanumeric(str(row.get("camera_name", row.get("device_name", ""))))
            candidates = [d for d in candidates if alphanumeric(d.name) == name]
        return candidates[0] if len(candidates) == 1 else None

    def resource_url(self, path: str) -> str:
        """Keep bearer tokens on this account's REST origin, including redirects."""
        url = urljoin(self.root + "/", path)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != urlsplit(self.root).netloc
            or parsed.username
            or parsed.fragment
        ):
            raise BlinkError("Refused media destination outside account REST origin")
        return url

    async def cloud_clips(self, since: str | None = None) -> list[Clip]:
        """Read cloud media changes without updating viewed flags or deleting originals.

        The established v1 changed-media endpoint uses numbered pages terminated
        by an empty page. Never commit an incomplete scan as a successful baseline.
        """
        try:
            return await self._cloud_clips(since)
        except (KeyError, TypeError, ValueError):
            raise BlinkError("Invalid cloud recording metadata") from None

    async def _cloud_clips(self, since: str | None) -> list[Clip]:
        clips = {}
        pages = set()
        for page in range(100):
            query = urlencode({"since": since or "1970-01-01T00:00:00+00:00", "page": page})
            data = await self.request(
                "GET", f"/api/v1/accounts/{self.account_id}/media/changed?{query}"
            )
            rows = data.get("media")
            if not isinstance(rows, list):
                raise BlinkError("Invalid cloud recording list")
            if not rows:
                return sorted(clips.values(), key=lambda clip: clip.created_at)
            # Detect endpoints/firmware ignoring the page argument.
            signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
            if signature in pages:
                raise BlinkError("Cloud media pagination did not advance")
            pages.add(signature)
            if self.observer:
                self.observer("cloud_media", {"clips": rows})
            for row in rows:
                if row.get("deleted") or not row.get("media"):
                    continue
                network = int(remote_id(row["network_id"]))
                camera_id = int(remote_id(row["camera_id"]))
                candidates = [
                    d
                    for d in self.devices.values()
                    if d.network_id == network and d.id == camera_id
                ]
                if row.get("device_type"):
                    candidates = [d for d in candidates if d.kind == row["device_type"]]
                if len(candidates) != 1:
                    continue
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                # Validate before persisting, and again before sending credentials.
                self.resource_url(row["media"])
                clip = Clip(
                    self.account_id,
                    network,
                    None,
                    remote_id(row["id"]),
                    "",
                    candidates[0].key,
                    created.astimezone(UTC).isoformat(),
                    source="cloud",
                    media_path=row["media"],
                )
                clips[clip.key] = clip
        raise BlinkError("Cloud media page limit exceeded")

    async def resource(self, path: str, limit: int = 8 * 1024 * 1024) -> bytes:
        chunks = bytearray()
        async for chunk in self.resource_chunks(path, limit):
            chunks.extend(chunk)
        return bytes(chunks)

    async def resource_chunks(self, path: str, limit: int = 256 * 1024 * 1024):
        url = self.resource_url(path)
        token = await self.auth.token()
        try:
            for attempt in range(2):
                async with self.session.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=180, sock_read=30),
                ) as response:
                    if response.status == 401 and attempt == 0:
                        token = await self.auth.token(rejected=token)
                        continue
                    if response.status in (401, 403):
                        raise AuthError("Blink rejected media authorization")
                    if response.status != 200:
                        raise ApiError(response.status)
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise BlinkError("Blink media exceeds size limit")
                        yield chunk
                    if not total:
                        raise BlinkError("Blink returned empty media")
                    return
        except (aiohttp.ClientError, TimeoutError):
            raise BlinkError("Blink media transfer failed") from None

    async def clip_chunks(self, clip: Clip):
        if clip.account_id != self.account_id:
            raise BlinkError("Recording belongs to another account")
        if clip.source == "cloud":
            if not clip.media_path:
                raise BlinkError("Cloud recording has no media path; refresh clips")
            async for chunk in self.resource_chunks(clip.media_path):
                yield chunk
            return
        if clip.source != "usb":
            raise BlinkError("Unknown recording source")
        module = self.modules.get(clip.module_id)
        if module is None or not module.local_storage:
            raise BlinkError("USB storage is unavailable")
        async with self.module_lock(module.id):
            # Refresh the manifest so saved metadata never reuses an obsolete manifest ID.
            current = {item.key: item for item in await self._clips(module)}
            if clip.key not in current:
                raise BlinkError("Recording is no longer available on USB storage")
            clip = current[clip.key]
            path = f"{self.storage_path(module)}/manifest/{clip.manifest_id}/clip/request/{clip.id}"
            command = await self.request("POST", path, {})
            await self.wait_command(module.network_id, int(remote_id(command["id"])))
            for attempt in range(3):
                try:
                    async for chunk in self.resource_chunks(path):
                        yield chunk
                    return
                except ApiError as error:
                    if error.status not in (404, 409, 429, 503) or attempt == 2:
                        raise
                    await asyncio.sleep(max(2**attempt, error.retry_after))

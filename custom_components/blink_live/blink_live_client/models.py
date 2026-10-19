"""Small, explicit public models shared by the CLI and HA adapter."""

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Device:
    id: int
    network_id: int
    kind: str
    name: str
    serial: str
    model: str = "Blink camera"
    thumbnail: str | int | None = None
    online: bool = True
    motion_enabled: bool | None = None
    network_armed: bool | None = None

    @property
    def key(self) -> str:
        return f"{self.network_id}_{self.kind}_{self.id}"


@dataclass(frozen=True)
class Network:
    id: int
    name: str
    armed: bool | None = None


@dataclass(frozen=True)
class SyncModule:
    id: int
    network_id: int
    name: str
    local_storage: bool


@dataclass(frozen=True)
class Clip:
    account_id: int
    network_id: int
    module_id: int | None
    id: str
    manifest_id: str
    camera_key: str
    created_at: str
    size: int = 0
    source: str = "usb"
    discovered_at: str | None = None
    media_path: str | None = None

    @property
    def key(self) -> str:
        # Clip numbers may be reused after storage replacement; creation time distinguishes them.
        identity = (
            f"{self.account_id}:{self.network_id}:{self.module_id}:{self.id}:{self.created_at}"
        )
        if self.source != "usb":
            identity = f"{self.source}:{identity}"
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    def event_data(self) -> dict:
        return {
            "account_id": self.account_id,
            "network_id": self.network_id,
            "sync_module_id": self.module_id,
            "camera_key": self.camera_key,
            "clip_id": self.key,
            "source": self.source,
            "recorded_at": self.created_at,
            "discovered_at": self.discovered_at or datetime.now(UTC).isoformat(),
        }

    def to_dict(self) -> dict:
        return asdict(self)

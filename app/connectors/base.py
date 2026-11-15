from dataclasses import dataclass, field
from typing import Any

@dataclass
class Item:
    scope: str
    remote_id: str
    title: str
    kind: str = 'track'
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

class NotConfigured(Exception):
    pass

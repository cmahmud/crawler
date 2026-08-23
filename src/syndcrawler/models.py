from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PageRecord:
    url: str
    status_code: int | None
    content_type: str | None
    title: str | None
    links: tuple[str, ...] = ()
    engine: str = "http"
    route: str = "direct"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["links"] = list(self.links)
        return data

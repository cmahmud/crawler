from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ChangeKind(StrEnum):
    NEW = "new"
    NOT_MODIFIED = "not_modified"
    UNCHANGED = "unchanged"
    REPRESENTATION_CHANGED = "representation_changed"
    SEMANTIC_CHANGED = "semantic_changed"


@dataclass(frozen=True, slots=True)
class HttpValidators:
    etag: str | None = None
    last_modified: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.etag or self.last_modified)

    def request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["if-none-match"] = self.etag
        if self.last_modified:
            headers["if-modified-since"] = self.last_modified
        return headers

    def to_dict(self) -> dict[str, str | None]:
        return {"etag": self.etag, "last_modified": self.last_modified}


@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    content_sha256: str
    semantic_sha256: str
    validators: HttpValidators = HttpValidators()

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "semantic_sha256": self.semantic_sha256,
            "validators": self.validators.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ChangeAssessment:
    kind: ChangeKind
    raw_changed: bool
    semantic_changed: bool

    @property
    def changed(self) -> bool:
        return self.kind in {
            ChangeKind.REPRESENTATION_CHANGED,
            ChangeKind.SEMANTIC_CHANGED,
        }


def validators_from_headers(headers: Mapping[str, object]) -> HttpValidators:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    return HttpValidators(
        etag=_clean_header(normalized.get("etag")),
        last_modified=_clean_header(normalized.get("last-modified")),
    )


def fingerprint_page(
    body: bytes | str,
    *,
    title: str | None,
    links: tuple[str, ...],
    structured: Mapping[str, Any],
    headers: Mapping[str, object] | None = None,
) -> ContentFingerprint:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    semantic_payload = {
        "title": title,
        "links": sorted(set(links)),
        "structured": structured,
    }
    semantic_bytes = json.dumps(
        semantic_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return ContentFingerprint(
        content_sha256=hashlib.sha256(raw).hexdigest(),
        semantic_sha256=hashlib.sha256(semantic_bytes).hexdigest(),
        validators=validators_from_headers(headers or {}),
    )


def assess_change(
    previous: ContentFingerprint | None,
    current: ContentFingerprint | None,
    *,
    status_code: int | None = None,
) -> ChangeAssessment:
    if status_code == 304:
        return ChangeAssessment(
            kind=ChangeKind.NOT_MODIFIED,
            raw_changed=False,
            semantic_changed=False,
        )
    if previous is None:
        return ChangeAssessment(
            kind=ChangeKind.NEW,
            raw_changed=False,
            semantic_changed=False,
        )
    if current is None:
        raise ValueError("current fingerprint is required unless status_code is 304")

    raw_changed = previous.content_sha256 != current.content_sha256
    semantic_changed = previous.semantic_sha256 != current.semantic_sha256
    if semantic_changed:
        kind = ChangeKind.SEMANTIC_CHANGED
    elif raw_changed:
        kind = ChangeKind.REPRESENTATION_CHANGED
    else:
        kind = ChangeKind.UNCHANGED
    return ChangeAssessment(
        kind=kind,
        raw_changed=raw_changed,
        semantic_changed=semantic_changed,
    )


def _clean_header(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None

"""Small internal value object for bounded UTF-8 text artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextArtifactSpec:
    """Text content plus deterministic integrity metadata."""

    name: str
    mime_type: str
    content: str

    @property
    def byte_size(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def integrity_record(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }

"""Small internal value object for bounded UTF-8 text artifacts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextArtifactSpec:
    """Text content plus bounded artifact metadata."""

    name: str
    mime_type: str
    content: str

    @property
    def byte_size(self) -> int:
        return len(self.content.encode("utf-8"))

    def metadata_record(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
        }

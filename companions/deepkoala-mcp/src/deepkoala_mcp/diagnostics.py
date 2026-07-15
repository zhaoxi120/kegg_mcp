"""Bounded sanitization for output produced by the external runner process."""

from __future__ import annotations

import re
from asyncio import StreamReader
from collections import deque
from collections.abc import Iterable
from pathlib import Path

_SECRET_LINE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|authorization|cookie|credential|password|secret|token)\b"
    r"\s*[\"']?\s*[:=]|\bbearer\s+[A-Za-z0-9._~+/-]+)"
)
_LABELED_PRIVATE_VALUE = re.compile(r"(?i)\b(?:fasta[_-]?header|header|sequence)\s*[:=]\s*\S+")
_SEQUENCE_LINE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYBXZJUO*]+$", re.IGNORECASE)
_SEQUENCE_FRAGMENT = re.compile(
    r"(?<![A-Za-z])[ACDEFGHIKLMNPQRSTVWYBXZJUO*]{20,}(?![A-Za-z])", re.IGNORECASE
)
_MAX_PENDING_BYTES = 16_384


class SanitizedDiagnosticTail:
    """Keep a UTF-8-safe tail while excluding paths, secrets, and sequence-like lines."""

    def __init__(self, *, max_bytes: int, redacted_paths: Iterable[Path] = ()) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._paths = tuple(
            sorted(
                {str(path) for path in redacted_paths if str(path)},
                key=len,
                reverse=True,
            )
        )
        self._lines: deque[bytes] = deque()
        self._size = 0
        self._pending = bytearray()
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def feed(self, chunk: bytes) -> None:
        """Consume arbitrary subprocess bytes without retaining an unbounded partial line."""
        if not chunk:
            return
        self._pending.extend(chunk)
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self._pending[: newline + 1])
            del self._pending[: newline + 1]
            self._append_sanitized(raw)
        if len(self._pending) > _MAX_PENDING_BYTES:
            self._append_sanitized(bytes(self._pending[:_MAX_PENDING_BYTES]) + b"\n")
            self._pending.clear()
            self._truncated = True

    def finish(self) -> None:
        """Flush the final partial line."""
        if self._pending:
            self._append_sanitized(bytes(self._pending))
            self._pending.clear()

    def text(self) -> str:
        """Return the retained diagnostic tail as bounded text."""
        return b"".join(self._lines).decode("utf-8", errors="replace")

    def _append_sanitized(self, raw: bytes) -> None:
        decoded = raw.decode("utf-8", errors="replace").replace("\x00", "�")
        stripped = decoded.strip()
        if _SECRET_LINE.search(decoded):
            decoded = "<sensitive diagnostic omitted>\n"
        elif stripped.startswith(">") or _SEQUENCE_LINE.fullmatch(stripped):
            decoded = "<sequence-like diagnostic omitted>\n"
        else:
            for value in self._paths:
                decoded = decoded.replace(value, "<local-path>")
            decoded = _LABELED_PRIVATE_VALUE.sub("<private input omitted>", decoded)
            decoded = _SEQUENCE_FRAGMENT.sub("<sequence-like diagnostic omitted>", decoded)
        encoded = decoded.encode("utf-8")
        if len(encoded) > self._max_bytes:
            encoded = encoded[-self._max_bytes :]
            self._truncated = True
        self._lines.append(encoded)
        self._size += len(encoded)
        while self._size > self._max_bytes and self._lines:
            removed = self._lines.popleft()
            self._size -= len(removed)
            self._truncated = True


async def drain_sanitized_stream(
    stream: StreamReader,
    sink: SanitizedDiagnosticTail,
) -> None:
    """Drain one child pipe continuously so verbose tools cannot deadlock."""
    while True:
        chunk = await stream.read(8_192)
        if not chunk:
            break
        sink.feed(chunk)
    sink.finish()


__all__ = ["SanitizedDiagnosticTail", "drain_sanitized_stream"]

"""Parse non-empty text lines as ``text.line`` events."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_PROBE_BYTES = 4096
_MAX_LINES = 200_000


class PlainTextParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="plaintext",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="generic",
                artifact_type="text",
                extensions=[".txt", ".log", ".csv"],
                description="Any UTF-8 decodable text file; one event per line.",
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes="Lines are reported verbatim; no interpretation is performed.",
        known_limitations=[
            f"Files with more than {_MAX_LINES} lines are truncated and flagged.",
            "Non-UTF-8 text encodings are not detected.",
        ],
        specificity=0,  # generic fallback: any format-specific parser outranks it
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            probe = path.open("rb").read(_PROBE_BYTES)
        except OSError:
            return False
        if not probe or b"\x00" in probe:
            return False
        try:
            # Trailing bytes may split a multibyte character; ignore that edge.
            probe.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def parse(self, path: Path) -> ParseResult:
        events: list[Event] = []
        truncated = False
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                if lineno > _MAX_LINES:
                    truncated = True
                    break
                text = line.rstrip("\r\n")
                if not text.strip():
                    continue
                events.append(
                    Event(
                        event_type="text.line",
                        source_offset=f"line:{lineno}",
                        payload={"text": text},
                    )
                )
        if truncated:
            events.append(
                Event(
                    event_type="parser.truncated",
                    source_offset=f"line:{_MAX_LINES}",
                    confidence=1.0,
                    payload={"reason": f"file exceeds {_MAX_LINES} lines"},
                )
            )
        return ParseResult(events=events)

"""DJI subtitle telemetry (.SRT) parser.

DJI aircraft embed per-frame telemetry as SRT subtitles alongside recorded
video. Two families of layouts are supported:

1. Bracket style (Mavic 2 and later, DJI Fly era)::

       1
       00:00:00,000 --> 00:00:00,033
       <font size="36">FrameCnt : 1, DiffTime : 33ms
       2026-07-08 10:00:01,123
       [iso : 100] [shutter : 1/1000.0] ... [latitude: 47.397742]
       [longitude: 8.545594] [rel_alt: 12.300 abs_alt: 460.500] </font>

2. Legacy ``GPS(...)`` style (Phantom/Inspire era)::

       1
       00:00:00,000 --> 00:00:01,000
       GPS(8.545594,47.397742,17) BAROMETER:12.3
       HOME(8.5455,47.3977) 2017.06.22 09:13:57

Timestamps embedded in SRT blocks are camera-local time with no timezone
marker, so events carry them as ``ts_original`` with ``clock_confidence``
"camera_local" - never as authoritative UTC.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_PROBE_BYTES = 8192
_TIMECODE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}", re.MULTILINE
)
_DJI_MARKERS = ("[latitude", "latitude:", "GPS(", "FrameCnt", "[longitude", "longtitude")

# Bracket style. DJI has shipped both `[latitude: x]` and `latitude: x` and the
# infamous misspelling `longtitude` on some firmware.
_LAT_RE = re.compile(r"\blatitude\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_LON_RE = re.compile(r"\blong(?:t)?itude\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_REL_ALT_RE = re.compile(r"\brel_alt\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_ABS_ALT_RE = re.compile(r"\babs_alt\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_PLAIN_ALT_RE = re.compile(r"\baltitude\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_DATETIME_RE = re.compile(
    r"(\d{4})[-.](\d{2})[-.](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[,.](\d{1,6}))?"
)
# Legacy style: GPS(longitude, latitude, altitude-or-satcount)
_GPS_RE = re.compile(
    r"GPS\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*M?\s*\)",
    re.IGNORECASE,
)
_BAROMETER_RE = re.compile(r"BAROMETER\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
# Extra camera fields worth preserving in the payload.
_FIELD_RE = re.compile(r"\[(\w+)\s*:\s*([^\]]+)\]")


class DjiSrtParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="dji-srt",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="DJI",
                artifact_type="video-subtitle-telemetry",
                extensions=[".srt"],
                description="Per-frame telemetry subtitles recorded next to DJI video files.",
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "Embedded timestamps are camera-local with unknown timezone; positions are "
            "as reported by the aircraft at recording time."
        ),
        known_limitations=[
            "Only bracket-style and legacy GPS() layouts are recognized.",
            "SRT files without DJI telemetry markers are not claimed by detection.",
            "Camera-local timestamps are not converted to UTC.",
        ],
        specificity=60,
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
            text = probe.decode("utf-8", errors="replace")
        except Exception:
            return False
        if not _TIMECODE_RE.search(text):
            return False
        return any(marker in text for marker in _DJI_MARKERS)

    def parse(self, path: Path) -> ParseResult:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        events: list[Event] = []
        # Split on blank lines into subtitle blocks.
        for block in re.split(r"\r?\n\r?\n", text):
            block = block.strip()
            if not block or not _TIMECODE_RE.search(block):
                continue
            lines = block.splitlines()
            try:
                index = int(lines[0].strip())
            except (ValueError, IndexError):
                index = None
            event = self._parse_block(block, index)
            if event is not None:
                events.append(event)
        return ParseResult(events=events)

    def _parse_block(self, block: str, index: int | None) -> Event | None:
        lat = lon = alt_msl = alt_agl = None

        gps_match = _GPS_RE.search(block)
        if gps_match:
            lon = float(gps_match.group(1))
            lat = float(gps_match.group(2))
        else:
            lat_match = _LAT_RE.search(block)
            lon_match = _LON_RE.search(block)
            if lat_match and lon_match:
                lat = float(lat_match.group(1))
                lon = float(lon_match.group(1))

        if m := _ABS_ALT_RE.search(block):
            alt_msl = float(m.group(1))
        if m := _REL_ALT_RE.search(block):
            alt_agl = float(m.group(1))
        if alt_msl is None and alt_agl is None:
            if m := _BAROMETER_RE.search(block):
                alt_agl = float(m.group(1))
            elif m := _PLAIN_ALT_RE.search(block):
                # Plain "altitude" is ambiguous between MSL and relative on
                # legacy firmware; keep it in the payload only.
                pass

        ts_original = None
        if m := _DATETIME_RE.search(block):
            frac = (m.group(7) or "").ljust(6, "0")[:6]
            ts_original = (
                f"{m.group(1)}-{m.group(2)}-{m.group(3)}T"
                f"{m.group(4)}:{m.group(5)}:{m.group(6)}"
                + (f".{frac}" if m.group(7) else "")
            )

        if lat is None and lon is None and ts_original is None:
            return None

        payload = {k.lower(): v.strip() for k, v in _FIELD_RE.findall(block)}
        timecode = _TIMECODE_RE.search(block)
        if timecode:
            payload["video_timecode"] = timecode.group(0)

        return Event(
            event_type="video.telemetry",
            source_offset=f"block:{index}" if index is not None else None,
            ts_original=ts_original,
            ts_source="camera_clock",
            clock_confidence="camera_local",
            lat=lat,
            lon=lon,
            alt_msl=alt_msl,
            alt_agl=alt_agl,
            payload=payload,
        )

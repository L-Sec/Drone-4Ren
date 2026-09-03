"""DJI FlightRecord (.TXT) recognition and optional telemetry decoding.

DJI record payloads are scrambled from format v6 and encrypted from v13.
The parser always recognizes the file and extracts printable details strings.
When ``DJI_API_KEY`` is set in the server environment, ``pydjirecord`` also
decodes normalized flight frames. The key is never written to case data.
"""

from __future__ import annotations

import math
import os
import re
import struct
from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_MIN_SIZE = 128
_MAX_VERSION = 60
_ENCRYPTED_FROM_VERSION = 13
_PREFIX_END_OLD = 12
_PREFIX_END_V13 = 100
_STRING_RE = re.compile(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]{3,}")
_MAX_STRINGS = 50
_API_KEY_ENV = "DJI_API_KEY"  # pragma: allowlist secret -- variable name only


class DjiTxtParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="dji-txt",
        version="0.2.0",
        formats=[
            FormatSupport(
                vendor="DJI",
                artifact_type="flightrecord-txt",
                extensions=[".txt"],
                description=(
                    "DJI GO / DJI Fly binary FlightRecord logs; optional telemetry "
                    "decoding with a locally supplied DJI API key."
                ),
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "Details strings are reported verbatim. Optional pydjirecord telemetry is "
            "normalized to one-second samples with relative flight time."
        ),
        known_limitations=[
            "DJI format v13+ telemetry requires DJI_API_KEY in the server environment; "
            "without it the parser remains recognition-only.",
            "Telemetry decoding depends on pydjirecord and DJI's keychain service. "
            "Treat this path as test-build support until independently validated against "
            "known flights.",
            "Details-block strings may be absent or garbled in v13+ (encrypted) logs.",
            "Decoded DJI flight time is relative; this parser does not infer UTC.",
            "Version detection relies on the header version byte; hand-crafted files can "
            "spoof it.",
        ],
        specificity=75,
        support_level="partial",
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            size = path.stat().st_size
            head = path.open("rb").read(16)
        except OSError:
            return False
        if size < _MIN_SIZE or len(head) < 16:
            return False
        (details_offset,) = struct.unpack("<Q", head[:8])
        version = head[10]
        return (
            _PREFIX_END_OLD < details_offset <= size
            and 0 < version <= _MAX_VERSION
            and (size - details_offset) < 1_000_000
        )

    def parse(self, path: Path) -> ParseResult:
        raw = path.read_bytes()
        (details_offset,) = struct.unpack("<Q", raw[:8])
        version = raw[10]
        encrypted = version >= _ENCRYPTED_FROM_VERSION
        records_start = _PREFIX_END_V13 if encrypted else _PREFIX_END_OLD
        record_area = max(0, min(details_offset, len(raw)) - records_start)

        details = raw[min(details_offset, len(raw)):]
        strings = []
        for match in _STRING_RE.finditer(details):
            value = match.group(0).decode("utf-8", errors="replace").strip()
            if len(value) >= 4:
                strings.append(value)
            if len(strings) >= _MAX_STRINGS:
                break

        api_key = os.environ.get(_API_KEY_ENV, "").strip()
        events = [
            Event(
                event_type="artifact.recognized",
                source_offset="header",
                payload={
                    "format": "dji-flightrecord-txt",
                    "format_version": int(version),
                    "encrypted": encrypted,
                    "details_offset": int(details_offset),
                    "record_area_bytes": int(record_area),
                    "details_strings": strings,
                    "decoding_status": (
                        "requested" if api_key else
                        "available: set DJI_API_KEY in the server environment and parse again"
                    ),
                },
            )
        ]
        if not api_key:
            events.append(Event(
                event_type="parser.summary",
                payload={
                    "telemetry_decoded": False,
                    "reason": "DJI_API_KEY is not set; recognition completed",
                },
            ))
            return ParseResult(events=events)

        try:
            from pydjirecord import DJILog

            log = DJILog.from_bytes(raw)
            keychains = log.fetch_keychains(api_key) if encrypted else []
            frames = log.frames(keychains)
            telemetry = _frames_to_events(frames)
        except Exception as exc:  # decoder/network failures must not lose recognition
            detail = str(exc).replace(api_key, "[redacted]")
            events.append(Event(
                event_type="parser.summary",
                payload={
                    "telemetry_decoded": False,
                    "reason": f"DJI telemetry decoder failed: {type(exc).__name__}: {detail}",
                },
            ))
            return ParseResult(events=events)

        events[0].payload["decoding_status"] = "decoded with pydjirecord"
        events.extend(telemetry)
        events.append(Event(
            event_type="parser.summary",
            payload={
                "telemetry_decoded": True,
                "decoded_frames": len(frames),
                "emitted_samples": len(telemetry),
                "decoder": "pydjirecord",
            },
        ))
        return ParseResult(events=events)


def _enum_text(value: object) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_position(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not (lat == 0 and lon == 0)
    )


def _sample_frames(frames: list[object]) -> list[tuple[int, object]]:
    """Keep the first frame in each elapsed second, plus the final frame."""
    selected: list[tuple[int, object]] = []
    seen_seconds: set[int] = set()
    for index, frame in enumerate(frames):
        elapsed = _finite(getattr(frame.osd, "fly_time", None))
        second = math.floor(elapsed) if elapsed is not None else index
        if second not in seen_seconds:
            selected.append((index, frame))
            seen_seconds.add(second)
    if frames and (not selected or selected[-1][1] is not frames[-1]):
        selected.append((len(frames) - 1, frames[-1]))
    return selected


def _frames_to_events(frames: list[object]) -> list[Event]:
    events: list[Event] = []
    for index, frame in _sample_frames(frames):
        osd = frame.osd
        battery = frame.battery
        gimbal = frame.gimbal
        rc = frame.rc
        elapsed = _finite(osd.fly_time)
        lat, lon = _finite(osd.latitude), _finite(osd.longitude)
        has_position = _valid_position(lat, lon)
        yaw = _finite(osd.yaw)
        payload = {
            "flight_time_s": elapsed,
            "vertical_speed_m_s": _finite(osd.z_speed),
            "gps_satellites": osd.gps_num,
            "gps_level": osd.gps_level,
            "gps_used": osd.is_gpd_used,
            "flight_mode": _enum_text(osd.flyc_state),
            "flight_action": _enum_text(osd.flight_action),
            "drone_type": _enum_text(osd.drone_type),
            "on_ground": osd.is_on_ground,
            "motor_on": osd.is_motor_on,
            "battery_percent": battery.charge_level,
            "battery_voltage_v": _finite(battery.voltage),
            "battery_current_a": _finite(battery.current),
            "battery_temperature_c": _finite(battery.temperature),
            "gimbal_pitch": _finite(gimbal.pitch),
            "gimbal_roll": _finite(gimbal.roll),
            "gimbal_yaw": _finite(gimbal.yaw),
            "downlink_signal": rc.downlink_signal,
            "uplink_signal": rc.uplink_signal,
        }
        events.append(Event(
            event_type="gps.position" if has_position else "flight.telemetry",
            source_offset=f"frame:{index}",
            ts_original=f"+{elapsed:.3f}s" if elapsed is not None else None,
            ts_source="flight_elapsed",
            clock_confidence="relative",
            lat=lat if has_position else None,
            lon=lon if has_position else None,
            alt_msl=_finite(osd.altitude),
            alt_agl=_finite(osd.height),
            pitch=_finite(osd.pitch),
            roll=_finite(osd.roll),
            yaw=yaw,
            speed=_finite(osd.h_speed),
            heading=yaw % 360 if yaw is not None else None,
            payload={key: value for key, value in payload.items() if value is not None},
        ))
    return events

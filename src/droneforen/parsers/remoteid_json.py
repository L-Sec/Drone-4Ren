"""Remote ID observation import (JSON).

Imports UAS Remote ID observations (ASTM F3411 / FAA Remote ID broadcast
content) as recorded by receiver apps and C-UAS sensors that export JSON.
There is no single vendor-neutral export standard, so this parser accepts a
documented flexible mapping: a top-level array, or an object with an
``observations`` array, of objects carrying at least a position and one
Remote ID field.

Recognized keys (first match wins):

- time:      time | timestamp | ts | ts_utc   (ISO-8601, or epoch s/ms)
- UAS id:    uas_id | basic_id | serial | id
- position:  lat|latitude, lon|lng|longitude, alt|altitude|geodetic_altitude
             (m MSL), height|height_agl (m AGL)
- kinematics: speed | speed_ms, heading | track | course
- operator:  operator_id, operator_lat|pilot_lat, operator_lon|pilot_lon
- reception: rssi, receiver_lat|sensor_lat, receiver_lon|sensor_lon, mac, channel

Each observation becomes a ``rid.observation`` event. Timestamps are the
*receiver's* clock (clock_confidence "receiver") - Remote ID broadcasts are
unauthenticated and trivially spoofable, so observations corroborate or
contradict other evidence; they prove nothing alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_MAX_BYTES = 50 * 1024 * 1024
_RID_ID_KEYS = ("uas_id", "basic_id", "serial", "id")
_MARKER_KEYS = ("uas_id", "basic_id", "operator_id", "rssi", "rid")


def _pick(obs: dict, *keys):
    for key in keys:
        if key in obs and obs[key] is not None:
            return obs[key]
    return None


def _to_iso_utc(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch seconds vs milliseconds by magnitude
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="microseconds")
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)  # exports are UTC by convention; noted
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _maybe_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def observation_to_event(obs: dict, source_offset: str) -> Event | None:
    """Map one Remote ID observation dict to a normalized rid.observation Event.

    Shared by the JSON export parser and the readiness-segment parser so the
    two paths cannot diverge. Returns None when the object has no position."""
    lat = _maybe_float(_pick(obs, "lat", "latitude"))
    lon = _maybe_float(_pick(obs, "lon", "lng", "longitude"))
    if lat is None or lon is None:
        return None
    raw_time = _pick(obs, "time", "timestamp", "ts", "ts_utc")
    ts_utc = _to_iso_utc(raw_time)
    naive = isinstance(raw_time, str) and "+" not in raw_time \
        and not str(raw_time).endswith("Z")
    uas_id = _pick(obs, *_RID_ID_KEYS)

    payload = {
        "uas_id": str(uas_id) if uas_id is not None else None,
        "operator_id": _pick(obs, "operator_id"),
        "operator_lat": _maybe_float(_pick(obs, "operator_lat", "pilot_lat")),
        "operator_lon": _maybe_float(_pick(obs, "operator_lon", "pilot_lon")),
        "rssi": _maybe_float(_pick(obs, "rssi")),
        "receiver_lat": _maybe_float(_pick(obs, "receiver_lat", "sensor_lat")),
        "receiver_lon": _maybe_float(_pick(obs, "receiver_lon", "sensor_lon")),
        "mac": _pick(obs, "mac"),
        "channel": _pick(obs, "channel"),
    }
    if naive:
        payload["time_assumed_utc"] = True
    payload = {k: v for k, v in payload.items() if v is not None}

    return Event(
        event_type="rid.observation",
        source_offset=source_offset,
        ts_utc=ts_utc,
        ts_original=str(raw_time) if raw_time is not None else None,
        ts_source="receiver_clock",
        clock_confidence="receiver",
        lat=lat,
        lon=lon,
        alt_msl=_maybe_float(_pick(obs, "alt", "altitude", "geodetic_altitude")),
        alt_agl=_maybe_float(_pick(obs, "height", "height_agl")),
        speed=_maybe_float(_pick(obs, "speed", "speed_ms")),
        heading=_maybe_float(_pick(obs, "heading", "track", "course")),
        payload=payload,
    )


class RemoteIdJsonParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="remoteid-json",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="generic",
                artifact_type="remoteid-observations",
                extensions=[".json"],
                description=("Remote ID (ASTM F3411) observation exports from receiver "
                             "apps and C-UAS sensors, flexible JSON key mapping."),
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "Remote ID broadcasts are unauthenticated and spoofable; timestamps come "
            "from the receiving device's clock. Observations corroborate or contradict "
            "other evidence - alone they identify nothing."
        ),
        known_limitations=[
            "JSON exports only; raw Bluetooth/Wi-Fi captures (pcap) are not decoded.",
            "Timezone-naive timestamps are assumed UTC and flagged in the payload.",
            "Only files whose objects carry a recognizable Remote ID field are claimed.",
        ],
        specificity=72,
    )

    @classmethod
    def _load(cls, path: Path) -> list[dict] | None:
        try:
            if path.stat().st_size > _MAX_BYTES:
                return None
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if isinstance(doc, dict):
            doc = doc.get("observations")
        if not isinstance(doc, list) or not doc:
            return None
        rows = [x for x in doc if isinstance(x, dict)]
        return rows or None

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            head = path.open("rb").read(1)
        except OSError:
            return False
        if head not in (b"{", b"["):
            return False
        rows = cls._load(path)
        if rows is None:
            return False
        first = rows[0]
        has_position = (_pick(first, "lat", "latitude") is not None
                        and _pick(first, "lon", "lng", "longitude") is not None)
        has_marker = any(k in first for k in _MARKER_KEYS)
        return has_position and has_marker

    def parse(self, path: Path) -> ParseResult:
        rows = self._load(path)
        if rows is None:
            raise ValueError("not a recognizable Remote ID observation export")
        events: list[Event] = []
        uas_ids: set[str] = set()

        for i, obs in enumerate(rows):
            event = observation_to_event(obs, f"obs:{i}")
            if event is None:
                continue
            if event.payload.get("uas_id"):
                uas_ids.add(event.payload["uas_id"])
            events.append(event)

        events.append(Event(
            event_type="parser.summary",
            payload={
                "observations": len(events),
                "distinct_uas_ids": sorted(uas_ids),
                "caution": "Remote ID is unauthenticated; corroborate before relying",
            },
        ))
        return ParseResult(events=events)

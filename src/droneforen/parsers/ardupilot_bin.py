"""ArduPilot dataflash log (.BIN) parser.

Dataflash logs are written onboard by the flight controller and are the
richest ArduPilot artifact. pymavlink's DFReader derives a UTC clock from the
GPS week/millisecond fields in the log itself, so timestamps are GPS-anchored
("gps" clock confidence) once a fix exists.

Events emitted:

- ``gps.position``   - GPS records (downsampled; first/last kept)
- ``flight.mode``    - MODE records
- ``flight.armed`` / ``flight.disarmed`` / ``flight.event`` - EV records
- ``battery.status`` - BAT records (sampled on whole-volt / 1% changes)
- ``log.message``    - MSG records
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_DF_MAGIC = b"\xa3\x95\x80"  # HEAD1, HEAD2, FMT message type
_POSITION_INTERVAL_S = 1.0
_MAX_EVENTS = 200_000

# ArduPilot LogEvent ids (libraries/AP_Logger/LogStructure.h)
_EV_NAMES = {
    10: "armed",
    11: "disarmed",
    15: "auto_armed",
    17: "land_complete_maybe",
    18: "land_complete",
    25: "set_home",
    62: "takeoff",  # DATA_TAKEOFF in older firmwares differs; id kept in payload
}


def _iso_utc(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, UTC).isoformat(timespec="microseconds")


class ArduPilotBinParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="ardupilot-bin",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="ArduPilot",
                artifact_type="dataflash-log",
                extensions=[".bin", ".log"],
                description="Onboard dataflash log written by ArduPilot flight controllers.",
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "Timestamps are derived from GPS week/ms inside the log; records before "
            "first GPS lock are extrapolated by DFReader."
        ),
        known_limitations=[
            f"Positions are downsampled to one event per {_POSITION_INTERVAL_S}s "
            "(first and last kept); the original file retains full rate.",
            "Only GPS, MODE, EV, BAT and MSG record types are interpreted; all other "
            "record types are counted but not emitted.",
            "Text .log dataflash exports are not handled by this parser (binary only).",
        ],
        specificity=80,
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            return path.open("rb").read(3) == _DF_MAGIC
        except OSError:
            return False

    def parse(self, path: Path) -> ParseResult:  # noqa: C901 - dispatch loop
        from pymavlink import DFReader

        log = DFReader.DFReader_binary(str(path))
        events: list[Event] = []
        seq = 0
        skipped_types: dict[str, int] = {}
        last_position_ts: float | None = None
        pending_position: Event | None = None
        last_volt: float | None = None
        truncated = False

        while True:
            try:
                msg = log.recv_msg()
            except Exception:
                break
            if msg is None:
                break
            seq += 1
            if len(events) >= _MAX_EVENTS:
                truncated = True
                break
            mtype = msg.get_type()
            ts = getattr(msg, "_timestamp", None)
            common = dict(
                source_offset=f"rec:{seq}",
                ts_utc=_iso_utc(ts) if ts else None,
                ts_source="gps_log_clock",
                clock_confidence="gps",
            )

            if mtype == "GPS":
                lat = getattr(msg, "Lat", None)
                lng = getattr(msg, "Lng", None)
                if lat is None or lng is None or (lat == 0 and lng == 0):
                    continue
                event = Event(
                    event_type="gps.position",
                    **common,
                    lat=float(lat),
                    lon=float(lng),
                    alt_msl=float(getattr(msg, "Alt", 0.0)),
                    speed=float(getattr(msg, "Spd", 0.0)),
                    heading=float(getattr(msg, "GCrs", 0.0)),
                    payload={
                        "satellites": getattr(msg, "NSats", None),
                        "fix_status": getattr(msg, "Status", None),
                        "hdop": getattr(msg, "HDop", None),
                    },
                )
                if last_position_ts is None or ts is None or (
                    ts - last_position_ts >= _POSITION_INTERVAL_S
                ):
                    events.append(event)
                    last_position_ts = ts
                    pending_position = None
                else:
                    pending_position = event

            elif mtype == "MODE":
                events.append(
                    Event(
                        event_type="flight.mode",
                        **common,
                        payload={
                            "mode": str(getattr(msg, "Mode", "")),
                            "mode_num": getattr(msg, "ModeNum", None),
                            "reason": getattr(msg, "Rsn", None),
                        },
                    )
                )

            elif mtype == "EV":
                ev_id = int(getattr(msg, "Id", -1))
                name = _EV_NAMES.get(ev_id)
                if name == "armed":
                    etype = "flight.armed"
                elif name == "disarmed":
                    etype = "flight.disarmed"
                else:
                    etype = "flight.event"
                events.append(
                    Event(event_type=etype, **common,
                          payload={"event_id": ev_id, "event_name": name})
                )

            elif mtype == "BAT":
                volt = getattr(msg, "Volt", None)
                if volt is None:
                    continue
                # Sample: emit only when voltage moves >= 0.1 V to keep volume sane.
                if last_volt is not None and abs(float(volt) - last_volt) < 0.1:
                    continue
                last_volt = float(volt)
                events.append(
                    Event(
                        event_type="battery.status",
                        **common,
                        payload={
                            "voltage_v": float(volt),
                            "current_a": _maybe_float(getattr(msg, "Curr", None)),
                            "consumed_mah": _maybe_float(getattr(msg, "CurrTot", None)),
                            "remaining_pct": _maybe_float(getattr(msg, "RemPct", None)),
                            "temperature_c": _maybe_float(getattr(msg, "Temp", None)),
                        },
                    )
                )

            elif mtype == "MSG":
                events.append(
                    Event(event_type="log.message", **common,
                          payload={"text": str(getattr(msg, "Message", ""))})
                )
            else:
                skipped_types[mtype] = skipped_types.get(mtype, 0) + 1

        if pending_position is not None:
            events.append(pending_position)
        if truncated:
            events.append(
                Event(event_type="parser.truncated", source_offset=f"rec:{seq}",
                      payload={"reason": f"event cap {_MAX_EVENTS} reached"})
            )
        events.append(
            Event(
                event_type="parser.summary",
                source_offset=f"rec:{seq}",
                payload={
                    "records_read": seq,
                    "uninterpreted_record_types": dict(
                        sorted(skipped_types.items(), key=lambda kv: -kv[1])[:25]
                    ),
                    "position_downsample_interval_s": _POSITION_INTERVAL_S,
                },
            )
        )
        return ParseResult(events=events)


def _maybe_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

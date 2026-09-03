"""MAVLink telemetry log (.tlog) parser.

A tlog is the ground-station capture of the MAVLink command/telemetry link:
each MAVLink frame is prefixed with an 8-byte big-endian microsecond wall
clock stamp written by the logging GCS. Timestamps therefore reflect the
*ground station's* clock ("logger_wallclock"), not GPS time.

Events emitted:

- ``flight.armed`` / ``flight.disarmed`` - HEARTBEAT arm-flag transitions
- ``flight.mode``                        - HEARTBEAT custom-mode transitions
- ``gps.position``                       - GLOBAL_POSITION_INT (downsampled)
- ``battery.status``                     - SYS_STATUS remaining-% changes
- ``log.message``                        - STATUSTEXT from the vehicle
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_MAVLINK_MAGIC = (0xFE, 0xFD)  # v1, v2
_USEC_2000 = 946_684_800 * 1_000_000
_USEC_2100 = 4_102_444_800 * 1_000_000
_POSITION_INTERVAL_S = 1.0
_MAX_EVENTS = 200_000
_ARMED_FLAG = 128  # MAV_MODE_FLAG_SAFETY_ARMED
_GCS_SYSTEM_ID = 255


def _iso_utc(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, UTC).isoformat(timespec="microseconds")


class MavlinkTlogParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="mavlink-tlog",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="MAVLink",
                artifact_type="telemetry-log",
                extensions=[".tlog"],
                description="Timestamped MAVLink stream captured by a ground control station.",
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "Timestamps come from the logging ground station's wall clock, not GPS; "
            "they are as trustworthy as that machine's clock was."
        ),
        known_limitations=[
            f"Positions are downsampled to one event per {_POSITION_INTERVAL_S}s "
            "(first and last are always kept); the original file retains full rate.",
            "Corrupt frames are skipped, counted, and reported in a parser.summary event.",
            f"Parsing stops with a parser.truncated event after {_MAX_EVENTS} events.",
        ],
        specificity=80,
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            head = path.open("rb").read(9)
        except OSError:
            return False
        if len(head) < 9 or head[8] not in _MAVLINK_MAGIC:
            return False
        (usec,) = struct.unpack(">Q", head[:8])
        return usec == 0 or _USEC_2000 <= usec <= _USEC_2100

    def parse(self, path: Path) -> ParseResult:  # noqa: C901 - dispatch loop
        from pymavlink import mavutil

        conn = mavutil.mavlogfile(str(path), notimestamps=False, robust_parsing=True)
        events: list[Event] = []
        seq = 0
        bad_frames = 0
        armed: bool | None = None
        mode: str | None = None
        battery_remaining: int | None = None
        satellites: int | None = None
        fix_type: int | None = None
        last_position_ts: float | None = None
        pending_position: Event | None = None  # last seen, kept so the final fix is emitted
        truncated = False

        while True:
            try:
                msg = conn.recv_match(blocking=False)
            except Exception:
                bad_frames += 1
                continue
            if msg is None:
                break
            seq += 1
            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                bad_frames += 1
                continue
            if len(events) >= _MAX_EVENTS:
                truncated = True
                break

            ts = getattr(msg, "_timestamp", None)
            ts_utc = _iso_utc(ts) if ts else None
            common = dict(
                source_offset=f"msg:{seq}",
                ts_utc=ts_utc,
                ts_source="logger_wallclock",
                clock_confidence="logger",
            )

            if mtype == "HEARTBEAT":
                if msg.get_srcSystem() == _GCS_SYSTEM_ID or msg.type == 6:  # MAV_TYPE_GCS
                    continue
                now_armed = bool(msg.base_mode & _ARMED_FLAG)
                if armed is None or now_armed != armed:
                    if armed is not None or now_armed:
                        events.append(
                            Event(
                                event_type="flight.armed" if now_armed else "flight.disarmed",
                                **common,
                                payload={"base_mode": int(msg.base_mode),
                                         "custom_mode": int(msg.custom_mode)},
                            )
                        )
                    armed = now_armed
                try:
                    now_mode = mavutil.mode_string_v10(msg)
                except Exception:
                    now_mode = str(msg.custom_mode)
                if now_mode != mode:
                    events.append(
                        Event(event_type="flight.mode", **common,
                              payload={"mode": now_mode, "previous_mode": mode})
                    )
                    mode = now_mode

            elif mtype == "GPS_RAW_INT":
                satellites = int(msg.satellites_visible)
                fix_type = int(msg.fix_type)

            elif mtype == "GLOBAL_POSITION_INT":
                if msg.lat == 0 and msg.lon == 0:
                    continue
                speed = (msg.vx**2 + msg.vy**2) ** 0.5 / 100.0
                heading = msg.hdg / 100.0 if msg.hdg != 65535 else None
                event = Event(
                    event_type="gps.position",
                    **common,
                    lat=msg.lat / 1e7,
                    lon=msg.lon / 1e7,
                    alt_msl=msg.alt / 1000.0,
                    alt_agl=msg.relative_alt / 1000.0,
                    speed=round(speed, 3),
                    heading=heading,
                    payload={"satellites": satellites, "fix_type": fix_type},
                )
                if last_position_ts is None or ts is None or (
                    ts - last_position_ts >= _POSITION_INTERVAL_S
                ):
                    events.append(event)
                    last_position_ts = ts
                    pending_position = None
                else:
                    pending_position = event

            elif mtype == "SYS_STATUS":
                remaining = int(msg.battery_remaining)
                if remaining != battery_remaining:
                    events.append(
                        Event(
                            event_type="battery.status",
                            **common,
                            payload={
                                "voltage_v": (msg.voltage_battery / 1000.0
                                              if msg.voltage_battery != 65535 else None),
                                "current_a": (msg.current_battery / 100.0
                                              if msg.current_battery != -1 else None),
                                "remaining_pct": remaining if remaining != -1 else None,
                            },
                        )
                    )
                    battery_remaining = remaining

            elif mtype == "STATUSTEXT":
                events.append(
                    Event(
                        event_type="log.message",
                        **common,
                        payload={"severity": int(msg.severity), "text": str(msg.text)},
                    )
                )

        if pending_position is not None:
            events.append(pending_position)
        if truncated:
            events.append(
                Event(event_type="parser.truncated", source_offset=f"msg:{seq}",
                      payload={"reason": f"event cap {_MAX_EVENTS} reached"})
            )
        events.append(
            Event(
                event_type="parser.summary",
                source_offset=f"msg:{seq}",
                payload={
                    "messages_read": seq,
                    "bad_frames": bad_frames,
                    "position_downsample_interval_s": _POSITION_INTERVAL_S,
                },
            )
        )
        return ParseResult(events=events)

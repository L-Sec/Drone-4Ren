"""PX4 ULog (.ulg) parser.

ULog is PX4's onboard logging format. Internal timestamps are microseconds
since boot; UTC is recovered from the GPS dataset's ``time_utc_usec`` field
(boot→UTC offset), so all UTC times here are GPS-anchored. Logs without any
GPS lock keep boot-relative time in ``ts_original`` with no ``ts_utc``.

Events emitted:

- ``gps.position``   - vehicle_gps_position / sensor_gps (downsampled)
- ``flight.armed`` / ``flight.disarmed`` - vehicle_status arming transitions
- ``battery.status`` - battery_status (sampled on 1% remaining changes)
- ``log.message``    - logged messages (PX4 console/mavlink log)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_ULOG_MAGIC = b"\x55\x4c\x6f\x67\x01\x12\x35"  # "ULog" 0x01 0x12 0x35
_POSITION_INTERVAL_US = 1_000_000
_ARMING_STATE_ARMED = 2

_LOG_LEVELS = {0: "EMERG", 1: "ALERT", 2: "CRIT", 3: "ERR",
               4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG"}


def _iso_from_usec(usec: int) -> str:
    return datetime.fromtimestamp(usec / 1e6, UTC).isoformat(timespec="microseconds")


class Px4UlogParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="px4-ulog",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="PX4",
                artifact_type="ulog",
                extensions=[".ulg", ".ulog"],
                description="Onboard ULog written by PX4 flight controllers.",
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "UTC is reconstructed from the GPS receiver's time; boot-relative "
            "timestamps in logs without GPS lock are not converted."
        ),
        known_limitations=[
            "Positions are downsampled to one event per second (first/last kept); "
            "the original file retains full rate.",
            "Only GPS, arming, battery, and logged-message datasets are interpreted.",
            "Multi-instance GPS (multi_id > 0) datasets are skipped.",
        ],
        specificity=80,
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            return path.open("rb").read(7) == _ULOG_MAGIC
        except OSError:
            return False

    def parse(self, path: Path) -> ParseResult:  # noqa: C901 - dataset dispatch
        from pyulog import ULog

        ulog = ULog(str(path))
        datasets = {(d.name, d.multi_id): d for d in ulog.data_list}
        events: list[Event] = []

        # --- boot-time -> UTC offset from GPS ---------------------------------
        gps = None
        for name in ("vehicle_gps_position", "sensor_gps"):
            if (name, 0) in datasets:
                gps = datasets[(name, 0)]
                break
        utc_offset_us: int | None = None
        if gps is not None and "time_utc_usec" in gps.data:
            for boot_ts, utc in zip(
                gps.data["timestamp"], gps.data["time_utc_usec"], strict=False
            ):
                if utc > 0:
                    utc_offset_us = int(utc) - int(boot_ts)
                    break

        def times(boot_us: int, source: str) -> dict:
            fields = {
                "ts_original": f"boot_us:{int(boot_us)}",
                "ts_source": source,
            }
            if utc_offset_us is not None:
                fields["ts_utc"] = _iso_from_usec(int(boot_us) + utc_offset_us)
                fields["clock_confidence"] = "gps"
            else:
                fields["clock_confidence"] = "boot_relative_only"
            return fields

        # --- GPS positions -----------------------------------------------------
        if gps is not None:
            data = gps.data
            n = len(data["timestamp"])
            if "lat" in data:  # classic: int32 degrees * 1e7
                get_lat = lambda i: data["lat"][i] / 1e7  # noqa: E731
                get_lon = lambda i: data["lon"][i] / 1e7  # noqa: E731
                get_alt = lambda i: data["alt"][i] / 1e3  # noqa: E731
            elif "latitude_deg" in data:  # PX4 >= 1.14: double degrees
                get_lat = lambda i: float(data["latitude_deg"][i])  # noqa: E731
                get_lon = lambda i: float(data["longitude_deg"][i])  # noqa: E731
                get_alt = lambda i: float(data.get("altitude_msl_m", [0] * n)[i])  # noqa: E731
            else:
                get_lat = None
            if get_lat is not None:
                last_emitted_us: int | None = None
                pending: Event | None = None
                for i in range(n):
                    lat, lon = get_lat(i), get_lon(i)
                    if lat == 0 and lon == 0:
                        continue
                    boot_us = int(data["timestamp"][i])
                    event = Event(
                        event_type="gps.position",
                        source_offset=f"{gps.name}:{i}",
                        **times(boot_us, "gps"),
                        lat=lat,
                        lon=lon,
                        alt_msl=get_alt(i),
                        speed=_maybe(data, "vel_m_s", i),
                        payload={
                            "satellites": _maybe(data, "satellites_used", i, int),
                            "fix_type": _maybe(data, "fix_type", i, int),
                            "eph_m": _maybe(data, "eph", i),
                        },
                    )
                    if last_emitted_us is None or boot_us - last_emitted_us >= (
                        _POSITION_INTERVAL_US
                    ):
                        events.append(event)
                        last_emitted_us = boot_us
                        pending = None
                    else:
                        pending = event
                if pending is not None:
                    events.append(pending)

        # --- arming transitions -------------------------------------------------
        status = datasets.get(("vehicle_status", 0))
        if status is not None and "arming_state" in status.data:
            prev = None
            for i, state in enumerate(status.data["arming_state"]):
                state = int(state)
                if prev is not None and state != prev:
                    if state == _ARMING_STATE_ARMED or prev == _ARMING_STATE_ARMED:
                        events.append(
                            Event(
                                event_type=("flight.armed" if state == _ARMING_STATE_ARMED
                                            else "flight.disarmed"),
                                source_offset=f"vehicle_status:{i}",
                                **times(int(status.data["timestamp"][i]), "logger"),
                                payload={"arming_state": state, "previous_state": prev},
                            )
                        )
                prev = state

        # --- battery -------------------------------------------------------------
        battery = datasets.get(("battery_status", 0))
        if battery is not None and "voltage_v" in battery.data:
            prev_pct = None
            for i in range(len(battery.data["timestamp"])):
                remaining = _maybe(battery.data, "remaining", i)
                pct = round(remaining * 100) if remaining is not None else None
                if pct == prev_pct:
                    continue
                prev_pct = pct
                events.append(
                    Event(
                        event_type="battery.status",
                        source_offset=f"battery_status:{i}",
                        **times(int(battery.data["timestamp"][i]), "logger"),
                        payload={
                            "voltage_v": _maybe(battery.data, "voltage_v", i),
                            "current_a": _maybe(battery.data, "current_a", i),
                            "remaining_pct": pct,
                        },
                    )
                )

        # --- logged messages -------------------------------------------------------
        for i, logged in enumerate(ulog.logged_messages):
            events.append(
                Event(
                    event_type="log.message",
                    source_offset=f"logged_messages:{i}",
                    **times(int(logged.timestamp), "logger"),
                    payload={
                        "level": _LOG_LEVELS.get(logged.log_level, str(logged.log_level)),
                        "text": logged.message,
                    },
                )
            )

        events.sort(key=lambda e: (e.ts_utc is None, e.ts_utc or "", e.source_offset or ""))
        events.append(
            Event(
                event_type="parser.summary",
                payload={
                    "datasets": sorted(name for name, _ in datasets),
                    "utc_anchored": utc_offset_us is not None,
                    "position_downsample_interval_s": _POSITION_INTERVAL_US / 1e6,
                },
            )
        )
        return ParseResult(events=events)


def _maybe(data: dict, key: str, i: int, cast=float):
    if key not in data:
        return None
    try:
        value = cast(data[key][i])
    except (ValueError, TypeError, IndexError):
        return None
    # NaN guard: NaN != NaN
    if isinstance(value, float) and value != value:
        return None
    return value

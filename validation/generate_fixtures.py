"""Generate the deterministic validation fixtures in validation/fixtures/.

Run from the repository root:

    python validation/generate_fixtures.py

Every fixture is synthetic and reproducible from this script: a scripted
"flight" near the ArduPilot/PX4 SITL default home (Zurich, 47.397742 8.545594)
on 2026-07-08. Synthetic fixtures exercise the parsers' format handling with
known ground truth; they complement - not replace - validation against real
aircraft logs (NIST CFReDS / VTO Labs corpora), which are not redistributed
here.

Requires the dev extra (piexif) for the JPEG fixture.
"""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 7, 8, 10, 0, 0, tzinfo=UTC)
T0_US = int(T0.timestamp() * 1e6)
HOME_LAT, HOME_LON = 47.397742, 8.545594
HOME_ALT_MSL = 488.0

# Scripted track: (seconds after T0, lat, lon, alt_msl m, alt_agl m, speed m/s)
TRACK = [
    (2.0, 47.397742, 8.545594, 488.0, 0.0, 0.0),
    (3.0, 47.397760, 8.545600, 493.0, 5.0, 2.5),
    (4.0, 47.397800, 8.545650, 503.0, 15.0, 5.0),
    (4.4, 47.397815, 8.545670, 507.0, 19.0, 5.2),   # sub-second: downsampled away
    (5.0, 47.397860, 8.545720, 513.0, 25.0, 6.0),
    (6.0, 47.397930, 8.545800, 518.0, 30.0, 7.5),
    (7.0, 47.398000, 8.545900, 518.0, 30.0, 8.0),
    (8.0, 47.397930, 8.545800, 508.0, 20.0, 6.0),
    (8.5, 47.397880, 8.545740, 500.0, 12.0, 4.0),   # sub-second: kept as final fix
]


# --------------------------------------------------------------------------- tlog
def make_tlog(path: Path) -> None:
    from pymavlink.dialects.v20 import ardupilotmega as mavlink

    mav = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    out = bytearray()

    def w(t_offset_s: float, msg) -> None:
        out.extend(struct.pack(">Q", T0_US + int(t_offset_s * 1e6)))
        out.extend(msg.pack(mav))

    hb = lambda base, custom: mav.heartbeat_encode(2, 3, base, custom, 3)  # noqa: E731
    w(0.0, hb(81, 0))  # disarmed, STABILIZE
    w(0.1, mav.statustext_encode(6, b"ArduCopter V4.5.1 synthetic"))
    w(0.2, mav.sys_status_encode(0, 0, 0, 250, 12600, 800, 100, 0, 0, 0, 0, 0, 0))
    w(1.0, hb(217, 4))  # armed, GUIDED
    w(1.5, mav.gps_raw_int_encode(T0_US, 3, int(HOME_LAT * 1e7), int(HOME_LON * 1e7),
                                  int(HOME_ALT_MSL * 1000), 120, 150, 0, 0, 12))
    w(1.6, mav.statustext_encode(6, b"Takeoff detected"))
    for t, lat, lon, msl, agl, spd in TRACK:
        w(t, mav.global_position_int_encode(
            int(t * 1000), int(lat * 1e7), int(lon * 1e7), int(msl * 1000),
            int(agl * 1000), int(spd * 100), 0, 0, 9000))
    w(6.5, mav.sys_status_encode(0, 0, 0, 250, 12100, 2500, 91, 0, 0, 0, 0, 0, 0))
    w(7.5, hb(217, 6))  # RTL
    w(9.0, hb(81, 6))   # disarmed
    w(9.1, mav.statustext_encode(6, b"Disarming motors"))
    path.write_bytes(bytes(out))


# ---------------------------------------------------------------- ArduPilot .BIN
_DF_FMT_TYPE = 0x80
_DF_TYPES = {"GPS": 129, "MODE": 130, "EV": 131, "MSG": 132, "BAT": 133}
# DF format char -> struct char (subset used here)
_DF_STRUCT = {"Q": "Q", "L": "i", "f": "f", "B": "B", "H": "H", "I": "I",
              "M": "B", "Z": "64s", "n": "4s", "N": "16s"}
_DF_SIZE = {"Q": 8, "L": 4, "f": 4, "B": 1, "H": 2, "I": 4, "M": 1, "Z": 64,
            "n": 4, "N": 16}


def _df_header(msg_type: int) -> bytes:
    return b"\xa3\x95" + bytes([msg_type])


def _df_fmt(msg_type: int, name: str, fmt: str, columns: str) -> bytes:
    length = 3 + sum(_DF_SIZE[c] for c in fmt)
    return _df_header(_DF_FMT_TYPE) + struct.pack(
        "<BB4s16s64s", msg_type, length, name.encode().ljust(4, b"\x00"),
        fmt.encode().ljust(16, b"\x00"), columns.encode().ljust(64, b"\x00"),
    )


def _df_pack(name: str, fmt: str, *values) -> bytes:
    packed = []
    for char, value in zip(fmt, values, strict=True):
        s = _DF_STRUCT[char]
        if s.endswith("s"):
            value = value.encode() if isinstance(value, str) else value
            value = value.ljust(int(s[:-1]), b"\x00")
        packed.append(struct.pack("<" + s, value))
    return _df_header(_DF_TYPES[name]) + b"".join(packed)


def make_ardupilot_bin(path: Path) -> None:
    # GPS week/ms for T0 (GPS time = UTC + 18 leap seconds as of 2026)
    gps_epoch = datetime(1980, 1, 6, tzinfo=UTC)
    gps_seconds = (T0 - gps_epoch).total_seconds() + 18
    gwk = int(gps_seconds // 604800)
    gms0 = int((gps_seconds % 604800) * 1000)

    out = bytearray()
    out += _df_fmt(_DF_FMT_TYPE, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
    out += _df_fmt(_DF_TYPES["GPS"], "GPS", "QBIHBfLLfff",
                   "TimeUS,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs")
    out += _df_fmt(_DF_TYPES["MODE"], "MODE", "QMB", "TimeUS,Mode,ModeNum")
    out += _df_fmt(_DF_TYPES["EV"], "EV", "QB", "TimeUS,Id")
    out += _df_fmt(_DF_TYPES["MSG"], "MSG", "QZ", "TimeUS,Message")
    out += _df_fmt(_DF_TYPES["BAT"], "BAT", "Qff", "TimeUS,Volt,Curr")

    def us(t_offset_s: float) -> int:  # onboard TimeUS: boot at T0 - 60 s
        return int((60 + t_offset_s) * 1e6)

    out += _df_pack("MSG", "QZ", us(0.0), "ArduCopter V4.5.1 synthetic")
    out += _df_pack("MODE", "QMB", us(0.1), 0, 0)  # STABILIZE
    out += _df_pack("EV", "QB", us(1.0), 10)       # armed
    out += _df_pack("MODE", "QMB", us(1.1), 4, 4)  # GUIDED
    out += _df_pack("BAT", "Qff", us(1.2), 12.6, 0.8)
    for t, lat, lon, msl, _agl, spd in TRACK:
        out += _df_pack("GPS", "QBIHBfLLfff", us(t), 3,
                        gms0 + int(t * 1000), gwk, 12, 0.8,
                        int(lat * 1e7), int(lon * 1e7), msl, spd, 90.0)
    out += _df_pack("BAT", "Qff", us(6.5), 12.1, 2.5)
    out += _df_pack("MODE", "QMB", us(7.5), 6, 6)  # RTL
    out += _df_pack("EV", "QB", us(9.0), 11)       # disarmed
    path.write_bytes(bytes(out))


# --------------------------------------------------------------------- PX4 .ulg
def _ulog_msg(mtype: str, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload), ord(mtype)) + payload


def make_px4_ulg(path: Path) -> None:
    boot0_us = 100_000_000  # log starts 100 s after boot; time_utc_usec anchors to T0

    out = bytearray()
    out += b"\x55\x4c\x6f\x67\x01\x12\x35" + bytes([1]) + struct.pack("<Q", boot0_us)
    out += _ulog_msg("B", bytes(16) + struct.pack("<3Q", 0, 0, 0))  # flag bits
    out += _ulog_msg("F", b"vehicle_gps_position:uint64_t timestamp;int32_t lat;"
                          b"int32_t lon;int32_t alt;uint64_t time_utc_usec;"
                          b"float vel_m_s;uint8_t satellites_used;uint8_t fix_type;")
    out += _ulog_msg("F", b"battery_status:uint64_t timestamp;float voltage_v;"
                          b"float current_a;float remaining;")
    out += _ulog_msg("F", b"vehicle_status:uint64_t timestamp;uint8_t arming_state;")

    GPS_ID, BAT_ID, STATUS_ID = 0, 1, 2
    out += _ulog_msg("A", struct.pack("<BH", 0, GPS_ID) + b"vehicle_gps_position")
    out += _ulog_msg("A", struct.pack("<BH", 0, BAT_ID) + b"battery_status")
    out += _ulog_msg("A", struct.pack("<BH", 0, STATUS_ID) + b"vehicle_status")

    def boot_us(t_offset_s: float) -> int:
        return boot0_us + int(t_offset_s * 1e6)

    def data(msg_id: int, fmt: str, *values) -> bytes:
        return _ulog_msg("D", struct.pack("<H", msg_id) + struct.pack(fmt, *values))

    out += data(STATUS_ID, "<QB", boot_us(0.5), 1)   # standby
    out += data(STATUS_ID, "<QB", boot_us(1.0), 2)   # armed
    out += data(BAT_ID, "<Qfff", boot_us(1.2), 16.8, 1.1, 1.00)
    for t, lat, lon, msl, _agl, spd in TRACK:
        out += data(GPS_ID, "<QiiiQfBB", boot_us(t), int(lat * 1e7), int(lon * 1e7),
                    int(msl * 1000), T0_US + int(t * 1e6), spd, 14, 3)
    out += _ulog_msg("L", struct.pack("<BQ", 6, boot_us(1.6)) + b"Takeoff detected")
    out += data(BAT_ID, "<Qfff", boot_us(6.5), 15.9, 6.4, 0.91)
    out += _ulog_msg("L", struct.pack("<BQ", 4, boot_us(7.5)) + b"RTL: return at 30m")
    out += data(STATUS_ID, "<QB", boot_us(9.0), 1)   # disarmed
    path.write_bytes(bytes(out))


# -------------------------------------------------------------------- DJI SRT
def make_dji_srt_bracket(path: Path) -> None:
    blocks = []
    for i, (t, lat, lon, msl, agl, _spd) in enumerate(TRACK[:5], start=1):
        start_ms, end_ms = int((t - 2.0) * 1000), int((t - 2.0) * 1000) + 200
        # camera-local clock without timezone information, advancing with the flight
        ts = (T0 + timedelta(seconds=int(t))).replace(tzinfo=None)
        blocks.append(
            f"{i}\n"
            f"00:00:{start_ms // 1000:02d},{start_ms % 1000:03d} --> "
            f"00:00:{end_ms // 1000:02d},{end_ms % 1000:03d}\n"
            f'<font size="36">FrameCnt : {i}, DiffTime : 200ms\n'
            f"{ts:%Y-%m-%d %H:%M:%S},{int(t * 1000) % 1000:03d}\n"
            f"[iso : 100] [shutter : 1/1000.0] [fnum : 280] "
            f"[latitude: {lat:.6f}] [longtitude: {lon:.6f}] "
            f"[rel_alt: {agl:.3f} abs_alt: {msl:.3f}] </font>\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")


def make_dji_srt_legacy(path: Path) -> None:
    blocks = []
    for i, (t, lat, lon, _msl, agl, _spd) in enumerate(TRACK[:3], start=1):
        blocks.append(
            f"{i}\n"
            f"00:00:{i - 1:02d},000 --> 00:00:{i:02d},000\n"
            f"GPS({lon:.6f},{lat:.6f},19) BAROMETER:{agl:.1f}\n"
            f"HOME({HOME_LON:.6f},{HOME_LAT:.6f}) 2026.07.08 10:00:{int(t):02d}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ DJI .TXT
def make_dji_txt(path: Path) -> None:
    """Minimal DJI FlightRecord-shaped file: header with details-offset pointer
    and version byte, opaque record area, details block with legible strings.
    Exercises recognition and string extraction only, matching the parser's
    limited scope. This is not a decodable flight log."""
    records = bytes((i * 37 + 11) % 251 for i in range(256))  # opaque, deterministic
    details = (
        b"\x02\x18" + b"Mavic 2 Pro\x00" + b"\x01\x0e" + b"SN0123456789AB\x00"
        + b"\x03\x10" + b"Zurich Oerlikon\x00" + bytes(range(16))
    )
    details_offset = 12 + len(records)
    header = struct.pack("<Q", details_offset) + bytes([0, 0, 12, 0])  # version 12
    path.write_bytes(header + records + details)


# ------------------------------------------------------------- Remote ID JSON
def make_remoteid_json(path: Path) -> None:
    """Receiver-app style Remote ID observation export following the scripted
    flight with a few meters of jitter - close enough to corroborate the
    flight-log tracks in cross-source checks."""
    observations = []
    for i, (t, lat, lon, msl, agl, spd) in enumerate(TRACK):
        observations.append({
            "time": (T0 + timedelta(seconds=t)).isoformat(),
            "basic_id": "1581F5FGD226Q00A7891",
            "lat": round(lat + 0.000030, 7),     # ~3 m north of the true track
            "lon": round(lon + 0.000015, 7),
            "geodetic_altitude": msl + 2.0,
            "height_agl": agl,
            "speed": spd,
            "heading": 90.0,
            "rssi": -62 - (i % 5),
            "operator_lat": HOME_LAT,
            "operator_lon": HOME_LON,
            "receiver_lat": 47.3980,
            "receiver_lon": 8.5450,
            "mac": "60:60:1f:5a:4d:99",
        })
    path.write_text(
        json.dumps({"observations": observations}, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )


# ----------------------------------------------------------------------- JPEG
def make_geotagged_jpeg(path: Path) -> None:
    import piexif
    from PIL import Image

    def dms(value: float) -> tuple:
        d = int(value)
        m = int((value - d) * 60)
        s = round(((value - d) * 60 - m) * 60 * 100)
        return ((d, 1), (m, 1), (s, 100))

    exif_bytes = piexif.dump({
        "0th": {piexif.ImageIFD.Make: b"DJI", piexif.ImageIFD.Model: b"FC3582",
                piexif.ImageIFD.Software: b"v01.00.0500"},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:07:08 12:00:05"},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: dms(47.397860),
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: dms(8.545720),
            piexif.GPSIFD.GPSAltitudeRef: 0,
            piexif.GPSIFD.GPSAltitude: (513000, 1000),
            piexif.GPSIFD.GPSTimeStamp: ((10, 1), (0, 1), (5, 1)),
            piexif.GPSIFD.GPSDateStamp: b"2026:07:08",
        },
    })
    img = Image.new("RGB", (64, 48), (30, 60, 90))
    img.save(path, "JPEG", exif=exif_bytes, quality=85)

    # Splice a DJI-style XMP APP1 segment after the EXIF APP1 so the parser's
    # drone-dji attribute extraction has something real to find.
    xmp_xml = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" '
        b'drone-dji:AbsoluteAltitude="+513.00" drone-dji:RelativeAltitude="+25.00" '
        b'drone-dji:GimbalPitchDegree="-90.00" drone-dji:FlightYawDegree="+90.00"/>'
        b"</rdf:RDF></x:xmpmeta>"
    )
    xmp_payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp_xml
    app1 = b"\xff\xe1" + struct.pack(">H", len(xmp_payload) + 2) + xmp_payload

    raw = bytearray(path.read_bytes())
    # Walk marker segments from SOI; insert the XMP APP1 after the EXIF APP1
    # (or after the last leading APPn segment if none is found).
    pos = 2
    insert_at = 2
    while pos + 4 <= len(raw) and raw[pos] == 0xFF and 0xE0 <= raw[pos + 1] <= 0xEF:
        seg_len = struct.unpack(">H", raw[pos + 2:pos + 4])[0]
        insert_at = pos + 2 + seg_len
        if raw[pos + 1] == 0xE1 and raw[pos + 4:pos + 10] == b"Exif\x00\x00":
            break
        pos = pos + 2 + seg_len
    raw[insert_at:insert_at] = app1
    path.write_bytes(bytes(raw))


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_tlog(FIXTURES / "synthetic_flight.tlog")
    make_ardupilot_bin(FIXTURES / "synthetic_flight.bin")
    make_px4_ulg(FIXTURES / "synthetic_flight.ulg")
    make_dji_srt_bracket(FIXTURES / "dji_bracket.srt")
    make_dji_srt_legacy(FIXTURES / "dji_legacy.srt")
    make_geotagged_jpeg(FIXTURES / "geotagged.jpg")
    make_dji_txt(FIXTURES / "dji_flightrecord.txt")
    make_remoteid_json(FIXTURES / "remoteid_scan.json")
    for f in sorted(FIXTURES.iterdir()):
        print(f"{f.name}: {f.stat().st_size} bytes")


if __name__ == "__main__":
    main()

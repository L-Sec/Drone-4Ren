"""JPEG EXIF/XMP media parser.

Emits one ``media.capture`` event per image with GPS position, capture
timestamps, and camera identity. Drone vendors (notably DJI) also embed
flight state in XMP ``drone-dji:`` attributes - those are preserved verbatim
in the event payload.

Timestamp semantics follow the EXIF specification:

- ``GPSDateStamp`` + ``GPSTimeStamp`` are UTC → used for ``ts_utc``
  (clock_confidence "gps").
- ``DateTimeOriginal`` is camera-local time → kept as ``ts_original``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from PIL import Image

from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult

_JPEG_MAGIC = b"\xff\xd8\xff"

# EXIF tag ids
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_SOFTWARE = 0x0131
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_OFFSET_TIME_ORIGINAL = 0x9011

_GPS_LAT_REF, _GPS_LAT = 1, 2
_GPS_LON_REF, _GPS_LON = 3, 4
_GPS_ALT_REF, _GPS_ALT = 5, 6
_GPS_TIMESTAMP, _GPS_DATESTAMP = 7, 29

_XMP_DJI_RE = re.compile(rb'drone-dji:([\w-]+)\s*=\s*"([^"]*)"')


def _dms_to_decimal(dms, ref: str | None) -> float | None:
    try:
        degrees = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None
    if ref in ("S", "W"):
        degrees = -degrees
    return degrees


class ImageExifParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="image-exif",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="generic",
                artifact_type="image-metadata",
                extensions=[".jpg", ".jpeg"],
                description="JPEG EXIF (GPS, timestamps, camera) and DJI XMP flight state.",
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "EXIF fields are written by the capturing device and are trivially editable; "
            "corroborate against flight logs before relying on them."
        ),
        known_limitations=[
            "JPEG only; TIFF/HEIC/PNG and video containers are not handled.",
            "XMP extraction is limited to drone-dji attributes (regex, not full XML).",
            "DateTimeOriginal is camera-local and not converted to UTC.",
        ],
        specificity=70,
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            return path.open("rb").read(3) == _JPEG_MAGIC
        except OSError:
            return False

    def parse(self, path: Path) -> ParseResult:
        with Image.open(path) as img:
            width, height = img.size
            exif = img.getexif()
            exif_ifd = exif.get_ifd(_IFD_EXIF)
            gps_ifd = exif.get_ifd(_IFD_GPS)

        payload: dict = {"width": width, "height": height}
        for tag, key in ((_TAG_MAKE, "make"), (_TAG_MODEL, "model"), (_TAG_SOFTWARE, "software")):
            value = exif.get(tag)
            if value:
                payload[key] = str(value).strip("\x00 ")

        lat = lon = alt_msl = None
        if gps_ifd:
            lat = _dms_to_decimal(gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF))
            lon = _dms_to_decimal(gps_ifd.get(_GPS_LON), gps_ifd.get(_GPS_LON_REF))
            if (raw_alt := gps_ifd.get(_GPS_ALT)) is not None:
                try:
                    alt_msl = float(raw_alt)
                    alt_ref = gps_ifd.get(_GPS_ALT_REF, 0)
                    if isinstance(alt_ref, bytes):
                        alt_ref = alt_ref[0] if alt_ref else 0
                    if alt_ref == 1:  # below sea level
                        alt_msl = -alt_msl
                except (ValueError, ZeroDivisionError):
                    alt_msl = None

        ts_utc = None
        date_stamp = gps_ifd.get(_GPS_DATESTAMP) if gps_ifd else None
        time_stamp = gps_ifd.get(_GPS_TIMESTAMP) if gps_ifd else None
        if date_stamp and time_stamp is not None:
            try:
                h, m, s = (float(x) for x in time_stamp)
                y, mo, d = str(date_stamp).replace(":", "-").split("-")[:3]
                ts_utc = f"{y}-{mo}-{d}T{int(h):02d}:{int(m):02d}:{s:06.3f}+00:00"
            except (ValueError, TypeError, ZeroDivisionError):
                ts_utc = None

        ts_original = None
        if raw_dto := exif_ifd.get(_TAG_DATETIME_ORIGINAL):
            # EXIF "YYYY:MM:DD HH:MM:SS" -> ISO-ish, still local time
            parts = str(raw_dto).split(" ", 1)
            if len(parts) == 2:
                ts_original = parts[0].replace(":", "-") + "T" + parts[1]
            if offset := exif_ifd.get(_TAG_OFFSET_TIME_ORIGINAL):
                payload["tz_offset_original"] = str(offset)

        # DJI XMP flight-state attributes, scanned from raw bytes.
        raw = path.read_bytes()
        xmp_dji = {
            k.decode("ascii", "replace"): v.decode("utf-8", "replace")
            for k, v in _XMP_DJI_RE.findall(raw)
        }
        if xmp_dji:
            payload["xmp_dji"] = xmp_dji

        event = Event(
            event_type="media.capture",
            source_offset="exif",
            ts_utc=ts_utc,
            ts_original=ts_original,
            ts_source="gps" if ts_utc else "camera_clock",
            clock_confidence="gps" if ts_utc else "camera_local",
            lat=lat,
            lon=lon,
            alt_msl=alt_msl,
            payload=payload,
        )
        return ParseResult(events=[event])

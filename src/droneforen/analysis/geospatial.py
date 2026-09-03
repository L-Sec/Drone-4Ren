"""Track assembly and geospatial exports (KML, GPX, GeoJSON, CSV).

Tracks are assembled per evidence source from ``gps.position`` timeline
entries - sources are never silently merged, because disagreement between
sources is itself evidence. All writers are stdlib-only and deterministic.
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape

from ..case import CaseWorkspace
from .timeline import load_timeline

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


@dataclass
class TrackPoint:
    ts_utc: str | None
    lat: float
    lon: float
    alt_msl: float | None
    alt_agl: float | None
    speed: float | None
    heading: float | None
    source_offset: str | None
    event_id: str


@dataclass
class Track:
    evidence_sha256: str
    evidence_name: str
    parser_name: str
    parser_version: str
    points: list[TrackPoint] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.evidence_name} ({self.evidence_sha256[:12]}, {self.parser_name})"

    def path_distance_m(self) -> float:
        return sum(
            haversine_m(a.lat, a.lon, b.lat, b.lon)
            for a, b in zip(self.points, self.points[1:], strict=False)
        )


def build_tracks(case: CaseWorkspace) -> list[Track]:
    """One track per evidence source, points in UTC order (parser order for ties)."""
    entries = load_timeline(case, event_type="gps.position")
    tracks: dict[str, Track] = {}
    for e in entries:
        if e.lat is None or e.lon is None:
            continue
        track = tracks.setdefault(
            e.evidence_sha256,
            Track(
                evidence_sha256=e.evidence_sha256,
                evidence_name=e.evidence_name,
                parser_name=e.parser_name,
                parser_version=e.parser_version,
            ),
        )
        track.points.append(
            TrackPoint(
                ts_utc=e.ts_utc, lat=e.lat, lon=e.lon, alt_msl=e.alt_msl,
                alt_agl=e.alt_agl, speed=e.speed, heading=e.heading,
                source_offset=e.source_offset, event_id=e.event_id,
            )
        )
    for track in tracks.values():
        track.points.sort(key=lambda p: (p.ts_utc is None, p.ts_utc or "", p.source_offset or ""))
    return sorted(tracks.values(), key=lambda t: t.evidence_name)


def rid_tracks(case: CaseWorkspace) -> list[Track]:
    """Remote ID observation tracks, one per (evidence item, UAS id).

    Kept separate from flight-log tracks: RID is unauthenticated third-party
    observation, and comparing the two is the point (checks.py does exactly
    that)."""
    grouped: dict[tuple[str, str], Track] = {}
    for e in load_timeline(case, event_type="rid.observation"):
        if e.lat is None or e.lon is None:
            continue
        uas_id = str(e.payload.get("uas_id", "unknown"))
        key = (e.evidence_sha256, uas_id)
        track = grouped.setdefault(key, Track(
            evidence_sha256=e.evidence_sha256,
            evidence_name=f"RID {uas_id} ‹{e.evidence_name}›",
            parser_name=e.parser_name,
            parser_version=e.parser_version,
        ))
        track.points.append(TrackPoint(
            ts_utc=e.ts_utc, lat=e.lat, lon=e.lon, alt_msl=e.alt_msl,
            alt_agl=e.alt_agl, speed=e.speed, heading=e.heading,
            source_offset=e.source_offset, event_id=e.event_id,
        ))
    for track in grouped.values():
        track.points.sort(key=lambda p: (p.ts_utc is None, p.ts_utc or "", p.source_offset or ""))
    return sorted(grouped.values(), key=lambda t: t.evidence_name)


@dataclass
class MediaPoint:
    """A media.capture event with a position (from EXIF), for map overlays."""

    ts_utc: str | None
    lat: float
    lon: float
    alt_msl: float | None
    evidence_name: str
    evidence_sha256: str
    event_id: str


def media_points(case: CaseWorkspace) -> list[MediaPoint]:
    return [
        MediaPoint(
            ts_utc=e.ts_utc, lat=e.lat, lon=e.lon, alt_msl=e.alt_msl,
            evidence_name=e.evidence_name, evidence_sha256=e.evidence_sha256,
            event_id=e.event_id,
        )
        for e in load_timeline(case, event_type="media.capture")
        if e.lat is not None and e.lon is not None
    ]


# ----------------------------------------------------------------- exporters

def to_kml(tracks: list[Track], media: list[MediaPoint], case_number: str) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        f"<name>Drone 4Ren case {escape(case_number)}</name>",
    ]
    for track in tracks:
        coords = " ".join(
            f"{p.lon:.7f},{p.lat:.7f},{p.alt_msl if p.alt_msl is not None else 0:.1f}"
            for p in track.points
        )
        parts += [
            "<Placemark>",
            f"<name>{escape(track.label)}</name>",
            "<LineString><tessellate>1</tessellate>"
            "<altitudeMode>absolute</altitudeMode>",
            f"<coordinates>{coords}</coordinates>",
            "</LineString></Placemark>",
        ]
    for m in media:
        parts += [
            "<Placemark>",
            f"<name>media: {escape(m.evidence_name)}</name>",
            f"<description>captured {escape(m.ts_utc or 'unknown time')}; "
            f"evidence {m.evidence_sha256[:12]}</description>",
            f"<Point><coordinates>{m.lon:.7f},{m.lat:.7f},"
            f"{m.alt_msl if m.alt_msl is not None else 0:.1f}</coordinates></Point>",
            "</Placemark>",
        ]
    parts.append("</Document></kml>")
    return "\n".join(parts) + "\n"


def to_gpx(tracks: list[Track], media: list[MediaPoint], case_number: str) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="droneforen" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"<metadata><name>Drone 4Ren case {escape(case_number)}</name></metadata>",
    ]
    for m in media:
        parts.append(
            f'<wpt lat="{m.lat:.7f}" lon="{m.lon:.7f}">'
            + (f"<ele>{m.alt_msl:.1f}</ele>" if m.alt_msl is not None else "")
            + (f"<time>{escape(m.ts_utc)}</time>" if m.ts_utc else "")
            + f"<name>media: {escape(m.evidence_name)}</name></wpt>"
        )
    for track in tracks:
        parts.append(f"<trk><name>{escape(track.label)}</name><trkseg>")
        for p in track.points:
            parts.append(
                f'<trkpt lat="{p.lat:.7f}" lon="{p.lon:.7f}">'
                + (f"<ele>{p.alt_msl:.1f}</ele>" if p.alt_msl is not None else "")
                + (f"<time>{escape(p.ts_utc)}</time>" if p.ts_utc else "")
                + "</trkpt>"
            )
        parts.append("</trkseg></trk>")
    parts.append("</gpx>")
    return "\n".join(parts) + "\n"


def to_geojson(tracks: list[Track], media: list[MediaPoint], case_number: str) -> str:
    features = []
    for track in tracks:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [round(p.lon, 7), round(p.lat, 7)]
                    + ([round(p.alt_msl, 1)] if p.alt_msl is not None else [])
                    for p in track.points
                ],
            },
            "properties": {
                "kind": "track",
                "evidence_sha256": track.evidence_sha256,
                "evidence_name": track.evidence_name,
                "parser": f"{track.parser_name} {track.parser_version}",
                "point_count": len(track.points),
                "first_utc": track.points[0].ts_utc if track.points else None,
                "last_utc": track.points[-1].ts_utc if track.points else None,
            },
        })
    for m in media:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(m.lon, 7), round(m.lat, 7)]},
            "properties": {
                "kind": "media",
                "evidence_sha256": m.evidence_sha256,
                "evidence_name": m.evidence_name,
                "ts_utc": m.ts_utc,
            },
        })
    doc = {"type": "FeatureCollection",
           "properties": {"case_number": case_number, "generator": "droneforen"},
           "features": features}
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def to_csv(tracks: list[Track], media: list[MediaPoint], case_number: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["kind", "ts_utc", "lat", "lon", "alt_msl", "alt_agl", "speed_ms",
                     "heading", "evidence_sha256", "evidence_name", "source_offset",
                     "event_id"])
    for track in tracks:
        for p in track.points:
            writer.writerow(["track", p.ts_utc, p.lat, p.lon, p.alt_msl, p.alt_agl,
                             p.speed, p.heading, track.evidence_sha256,
                             track.evidence_name, p.source_offset, p.event_id])
    for m in media:
        writer.writerow(["media", m.ts_utc, m.lat, m.lon, m.alt_msl, None, None, None,
                         m.evidence_sha256, m.evidence_name, None, m.event_id])
    return buf.getvalue()


EXPORTERS = {"kml": to_kml, "gpx": to_gpx, "geojson": to_geojson, "csv": to_csv}

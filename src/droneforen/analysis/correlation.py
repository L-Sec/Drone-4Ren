"""Media ↔ telemetry correlation.

Places each UTC-anchored ``media.capture`` event onto the flight track by
interpolating the aircraft position at capture time, then compares it with the
position embedded in the media's own metadata. Small offsets corroborate the
sources; large ones are worth investigating (wrong aircraft, edited EXIF,
clock error).

Media without a UTC-anchored timestamp (camera-local only) is listed as
uncorrelatable rather than guessed at - cross-clock matching without an anchor
would fabricate confidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..case import CaseWorkspace
from .geospatial import build_tracks, haversine_m, parse_iso
from .timeline import load_timeline

MAX_INTERPOLATION_GAP_S = 10.0


@dataclass
class MediaCorrelation:
    media_name: str
    media_sha256: str
    media_event_id: str
    ts_utc: str | None
    correlated: bool
    reason: str | None = None
    track_evidence_name: str | None = None
    track_lat: float | None = None
    track_lon: float | None = None
    track_alt_msl: float | None = None
    exif_lat: float | None = None
    exif_lon: float | None = None
    offset_m: float | None = None
    supporting_event_ids: list[str] | None = None


def correlate_media(case: CaseWorkspace) -> list[MediaCorrelation]:
    tracks = [t for t in build_tracks(case) if t.points]
    results: list[MediaCorrelation] = []

    for e in load_timeline(case, event_type="media.capture"):
        base = MediaCorrelation(
            media_name=e.evidence_name, media_sha256=e.evidence_sha256,
            media_event_id=e.event_id, ts_utc=e.ts_utc, correlated=False,
            exif_lat=e.lat, exif_lon=e.lon,
        )
        t_capture = parse_iso(e.ts_utc)
        if t_capture is None:
            base.reason = ("no UTC-anchored capture time (camera-local only); "
                           "not correlated to avoid a false match")
            results.append(base)
            continue

        best = None  # (gap_span_s, track, before, after, frac)
        for track in tracks:
            if track.evidence_sha256 == e.evidence_sha256:
                continue
            timed = [(parse_iso(p.ts_utc), p) for p in track.points if parse_iso(p.ts_utc)]
            for (ta, pa), (tb, pb) in zip(timed, timed[1:], strict=False):
                if ta <= t_capture <= tb:
                    span = (tb - ta).total_seconds()
                    if span > MAX_INTERPOLATION_GAP_S or span < 0:
                        continue
                    frac = 0.0 if span == 0 else (t_capture - ta).total_seconds() / span
                    if best is None or span < best[0]:
                        best = (span, track, pa, pb, frac)

        if best is None:
            base.reason = "capture time falls outside every track's coverage"
            results.append(base)
            continue

        _, track, pa, pb, frac = best
        lat = pa.lat + (pb.lat - pa.lat) * frac
        lon = pa.lon + (pb.lon - pa.lon) * frac
        alt = None
        if pa.alt_msl is not None and pb.alt_msl is not None:
            alt = pa.alt_msl + (pb.alt_msl - pa.alt_msl) * frac
        base.correlated = True
        base.track_evidence_name = track.evidence_name
        base.track_lat = round(lat, 7)
        base.track_lon = round(lon, 7)
        base.track_alt_msl = round(alt, 1) if alt is not None else None
        base.supporting_event_ids = [pa.event_id, pb.event_id]
        if e.lat is not None and e.lon is not None:
            base.offset_m = round(haversine_m(e.lat, e.lon, lat, lon), 1)
        results.append(base)
    return results

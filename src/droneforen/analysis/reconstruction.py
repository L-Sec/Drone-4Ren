"""Flight reconstruction: derive flight phases from source events.

Everything produced here is calculated from source events by stated rules, not
read directly from the input. Each derived
item carries the rule that produced it and references to the supporting
events, so a report can show its work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..case import CaseWorkspace
from .geospatial import Track, build_tracks, haversine_m, parse_iso
from .timeline import load_timeline

TAKEOFF_AGL_M = 2.0
TAKEOFF_MSL_DELTA_M = 2.0
SIGNAL_GAP_S = 5.0


@dataclass
class DerivedItem:
    """A single derived statement with its method and supporting events."""

    kind: str                    # takeoff | landing | rth | signal_gap | ...
    ts_utc: str | None
    description: str
    method: str                  # the rule that produced this statement
    event_ids: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None


@dataclass
class FlightSummary:
    evidence_sha256: str
    evidence_name: str
    parser: str
    items: list[DerivedItem] = field(default_factory=list)
    first_fix_utc: str | None = None
    last_fix_utc: str | None = None
    duration_s: float | None = None
    path_distance_m: float | None = None
    max_alt_agl_m: float | None = None
    max_alt_msl_m: float | None = None
    max_speed_ms: float | None = None
    position_count: int = 0


def _phase_items_from_events(case: CaseWorkspace, sha256: str) -> list[DerivedItem]:
    items: list[DerivedItem] = []
    for e in load_timeline(case, evidence_sha_prefix=sha256):
        if e.event_type == "flight.armed":
            items.append(DerivedItem(
                kind="armed", ts_utc=e.ts_utc,
                description="Aircraft armed",
                method="source event flight.armed", event_ids=[e.event_id]))
        elif e.event_type == "flight.disarmed":
            items.append(DerivedItem(
                kind="disarmed", ts_utc=e.ts_utc,
                description="Aircraft disarmed",
                method="source event flight.disarmed", event_ids=[e.event_id]))
        elif e.event_type == "flight.mode":
            mode = str(e.payload.get("mode", "")).upper()
            if mode in ("RTL", "RTH", "SMART_RTL", "AUTO_RTL", "6"):
                items.append(DerivedItem(
                    kind="rth", ts_utc=e.ts_utc,
                    description=f"Return-to-home initiated (mode {mode})",
                    method="flight.mode change to an RTL/RTH mode",
                    event_ids=[e.event_id]))
    return items


def _analyze_track(track: Track, summary: FlightSummary) -> None:
    points = track.points
    summary.position_count = len(points)
    if not points:
        return
    summary.first_fix_utc = points[0].ts_utc
    summary.last_fix_utc = points[-1].ts_utc
    summary.path_distance_m = round(track.path_distance_m(), 1)

    t0, t1 = parse_iso(points[0].ts_utc), parse_iso(points[-1].ts_utc)
    if t0 and t1:
        summary.duration_s = (t1 - t0).total_seconds()

    agls = [p.alt_agl for p in points if p.alt_agl is not None]
    msls = [p.alt_msl for p in points if p.alt_msl is not None]
    speeds = [p.speed for p in points if p.speed is not None]
    if agls:
        summary.max_alt_agl_m = max(agls)
    if msls:
        summary.max_alt_msl_m = max(msls)
    if speeds:
        summary.max_speed_ms = max(speeds)

    # Takeoff: first fix above AGL threshold, or first fix that climbed
    # >2 m above the first fix's MSL when AGL is unavailable.
    takeoff = None
    if agls:
        takeoff = next((p for p in points if (p.alt_agl or 0) >= TAKEOFF_AGL_M), None)
        method = f"first fix with altitude AGL >= {TAKEOFF_AGL_M} m"
    elif msls:
        base = points[0].alt_msl
        takeoff = next(
            (p for p in points
             if p.alt_msl is not None and base is not None
             and p.alt_msl - base >= TAKEOFF_MSL_DELTA_M),
            None,
        )
        method = f"first fix >= {TAKEOFF_MSL_DELTA_M} m above initial MSL altitude"
    if takeoff:
        summary.items.append(DerivedItem(
            kind="takeoff", ts_utc=takeoff.ts_utc,
            description="Estimated airborne (takeoff)",
            method=method, event_ids=[takeoff.event_id],
            lat=takeoff.lat, lon=takeoff.lon))

    last = points[-1]
    summary.items.append(DerivedItem(
        kind="last_fix", ts_utc=last.ts_utc,
        description="Last recorded position fix",
        method="final gps.position event in this source",
        event_ids=[last.event_id], lat=last.lat, lon=last.lon))

    # Signal / recording gaps between consecutive fixes.
    for a, b in zip(points, points[1:], strict=False):
        ta, tb = parse_iso(a.ts_utc), parse_iso(b.ts_utc)
        if ta and tb:
            gap = (tb - ta).total_seconds()
            if gap >= SIGNAL_GAP_S:
                dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
                summary.items.append(DerivedItem(
                    kind="signal_gap", ts_utc=a.ts_utc,
                    description=(f"Position gap of {gap:.1f} s "
                                 f"({dist:.0f} m apart) - possible signal loss, "
                                 "logging stop, or downsampling artifact"),
                    method=f"consecutive fixes more than {SIGNAL_GAP_S} s apart",
                    event_ids=[a.event_id, b.event_id]))


def reconstruct(case: CaseWorkspace) -> list[FlightSummary]:
    """One FlightSummary per evidence source that produced positions or
    flight-state events."""
    summaries: dict[str, FlightSummary] = {}
    tracks = {t.evidence_sha256: t for t in build_tracks(case)}

    with case.connect_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ei.sha256, ei.original_name, pr.parser_name, pr.parser_version "
            "FROM events ev "
            "JOIN artifacts a ON ev.artifact_id = a.id "
            "JOIN evidence_items ei ON a.evidence_id = ei.id "
            "JOIN parser_runs pr ON ev.parser_run_id = pr.id "
            "WHERE ev.event_type IN ('gps.position', 'flight.armed', 'flight.disarmed', "
            "'flight.mode') ORDER BY ei.original_name"
        ).fetchall()

    for row in rows:
        sha = row["sha256"]
        if sha in summaries:
            continue
        summary = FlightSummary(
            evidence_sha256=sha,
            evidence_name=row["original_name"],
            parser=f"{row['parser_name']} {row['parser_version']}",
        )
        summary.items.extend(_phase_items_from_events(case, sha))
        if sha in tracks:
            _analyze_track(tracks[sha], summary)
        summary.items.sort(key=lambda i: (i.ts_utc is None, i.ts_utc or ""))
        summaries[sha] = summary
    return list(summaries.values())

"""Plausibility and tamper/counter-forensics indicators (v1).

Findings are hypotheses ranked by severity, never conclusions: an "impossible
speed" flag can mean spoofed data, a bad GPS fix, or a parser defect, and the
description says so. Each finding references the supporting event ids.

Checks implemented:

- plausibility: impossible speed between fixes, altitude rate jumps,
  time reversal within a source, cross-source track disagreement
- tamper/integrity v1: parser truncation events, failed parser runs,
  large recording gaps (surfaced by reconstruction as signal_gap items)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..case import CaseWorkspace
from .geospatial import Track, build_tracks, haversine_m, parse_iso, rid_tracks
from .timeline import load_timeline

MAX_PLAUSIBLE_SPEED_MS = 60.0     # generous multirotor envelope; fixed wing noted
MAX_PLAUSIBLE_CLIMB_MS = 30.0
CROSS_SOURCE_TOLERANCE_S = 1.0
CROSS_SOURCE_DISTANCE_M = 50.0


@dataclass
class Finding:
    check: str
    severity: str          # info | warning | alert
    description: str
    evidence_sha256: str | None = None
    event_ids: list[str] = field(default_factory=list)


def _offset_key(offset: str | None) -> tuple:
    """Sort key recovering parser emission order from offsets like 'rec:12',
    'msg:7', 'vehicle_gps_position:3'."""
    if offset and ":" in offset:
        prefix, _, suffix = offset.rpartition(":")
        if suffix.isdigit():
            return (0, prefix, int(suffix))
    return (1, offset or "", 0)


def _source_order(track: Track) -> Track:
    """Track with points in source record order - time-sorted order would hide
    exactly the anomalies (time reversal, splices) these checks look for."""
    reordered = Track(
        evidence_sha256=track.evidence_sha256, evidence_name=track.evidence_name,
        parser_name=track.parser_name, parser_version=track.parser_version,
        points=sorted(track.points, key=lambda p: _offset_key(p.source_offset)),
    )
    return reordered


def _track_checks(case: CaseWorkspace) -> list[Finding]:
    findings: list[Finding] = []
    for track in map(_source_order, build_tracks(case)):
        for a, b in zip(track.points, track.points[1:], strict=False):
            ta, tb = parse_iso(a.ts_utc), parse_iso(b.ts_utc)
            if not ta or not tb:
                continue
            dt = (tb - ta).total_seconds()
            if dt < 0:
                findings.append(Finding(
                    check="time_reversal", severity="alert",
                    description=(f"{track.evidence_name}: position timestamps go "
                                 f"backwards ({a.ts_utc} -> {b.ts_utc}) - possible clock "
                                 "manipulation, log splicing, or parser defect"),
                    evidence_sha256=track.evidence_sha256,
                    event_ids=[a.event_id, b.event_id]))
                continue
            dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
            if dt == 0:
                if dist > CROSS_SOURCE_DISTANCE_M:
                    findings.append(Finding(
                        check="coordinate_discontinuity", severity="warning",
                        description=(f"{track.evidence_name}: {dist:.0f} m position jump "
                                     "between fixes with identical timestamps"),
                        evidence_sha256=track.evidence_sha256,
                        event_ids=[a.event_id, b.event_id]))
                continue
            speed = dist / dt
            if speed > MAX_PLAUSIBLE_SPEED_MS:
                findings.append(Finding(
                    check="impossible_speed", severity="alert",
                    description=(f"{track.evidence_name}: implied speed {speed:.0f} m/s "
                                 f"over {dt:.1f} s ({dist:.0f} m) exceeds "
                                 f"{MAX_PLAUSIBLE_SPEED_MS:.0f} m/s - possible spoofed "
                                 "position, bad fix, or clock error (verify airframe: "
                                 "threshold assumes small UAS)"),
                    evidence_sha256=track.evidence_sha256,
                    event_ids=[a.event_id, b.event_id]))
            alt_a = a.alt_msl if a.alt_msl is not None else a.alt_agl
            alt_b = b.alt_msl if b.alt_msl is not None else b.alt_agl
            if alt_a is not None and alt_b is not None:
                climb = abs(alt_b - alt_a) / dt
                if climb > MAX_PLAUSIBLE_CLIMB_MS:
                    findings.append(Finding(
                        check="altitude_jump", severity="warning",
                        description=(f"{track.evidence_name}: altitude rate "
                                     f"{climb:.0f} m/s between fixes exceeds "
                                     f"{MAX_PLAUSIBLE_CLIMB_MS:.0f} m/s"),
                        evidence_sha256=track.evidence_sha256,
                        event_ids=[a.event_id, b.event_id]))
    return findings


MIN_CORROBORATION_POINTS = 3


def _cross_source_checks(case: CaseWorkspace) -> list[Finding]:
    """Compare overlapping position sources - flight logs against each other and
    against Remote ID observations. Near-simultaneous fixes far apart mean the
    sources disagree; consistent agreement is reported as corroboration."""
    findings: list[Finding] = []
    tracks = [t for t in build_tracks(case) + rid_tracks(case) if t.points]
    for i, ta in enumerate(tracks):
        for tb in tracks[i + 1:]:
            if ta.evidence_sha256 == tb.evidence_sha256:
                continue  # e.g. two UAS ids in one RID capture: not comparable
            worst = None
            matched = 0
            max_matched_dist = 0.0
            b_times = [(parse_iso(p.ts_utc), p) for p in tb.points if parse_iso(p.ts_utc)]
            if not b_times:
                continue
            for pa in ta.points:
                t_a = parse_iso(pa.ts_utc)
                if not t_a:
                    continue
                nearest = min(b_times, key=lambda x: abs((x[0] - t_a).total_seconds()))
                if abs((nearest[0] - t_a).total_seconds()) > CROSS_SOURCE_TOLERANCE_S:
                    continue
                dist = haversine_m(pa.lat, pa.lon, nearest[1].lat, nearest[1].lon)
                matched += 1
                max_matched_dist = max(max_matched_dist, dist)
                if dist > CROSS_SOURCE_DISTANCE_M and (worst is None or dist > worst[0]):
                    worst = (dist, pa, nearest[1])
            if worst:
                dist, pa, pb = worst
                findings.append(Finding(
                    check="cross_source_disagreement", severity="alert",
                    description=(f"{ta.evidence_name} and {tb.evidence_name} disagree by "
                                 f"up to {dist:.0f} m at near-simultaneous timestamps - "
                                 "possible tampering with one source, clock offset, or "
                                 "different aircraft"),
                    event_ids=[pa.event_id, pb.event_id]))
            elif matched >= MIN_CORROBORATION_POINTS:
                findings.append(Finding(
                    check="cross_source_corroboration", severity="info",
                    description=(f"{ta.evidence_name} and {tb.evidence_name} agree within "
                                 f"{max(max_matched_dist, 1):.0f} m across {matched} "
                                 "near-simultaneous fixes"),
                ))
    return findings


def _integrity_checks(case: CaseWorkspace) -> list[Finding]:
    findings: list[Finding] = []
    for e in load_timeline(case, event_type="parser.truncated"):
        findings.append(Finding(
            check="parser_truncation", severity="warning",
            description=(f"{e.evidence_name}: parser stopped early "
                         f"({e.payload.get('reason', 'unknown reason')}) - events beyond "
                         "this point are missing from the timeline"),
            evidence_sha256=e.evidence_sha256, event_ids=[e.event_id]))
    with case.connect_db() as conn:
        failed = conn.execute(
            "SELECT pr.id, pr.parser_name, pr.parser_version, pr.error, ei.original_name, "
            "ei.sha256 FROM parser_runs pr "
            "JOIN artifacts a ON pr.artifact_id = a.id "
            "JOIN evidence_items ei ON a.evidence_id = ei.id "
            "WHERE pr.status = 'failed'"
        ).fetchall()
    for row in failed:
        findings.append(Finding(
            check="failed_parser_run", severity="warning",
            description=(f"{row['original_name']}: parser {row['parser_name']} "
                         f"{row['parser_version']} failed ({(row['error'] or '')[:200]}) - "
                         "this source is not represented in the timeline"),
            evidence_sha256=row["sha256"]))
    return findings


def _identity_checks(case: CaseWorkspace) -> list[Finding]:
    """Tamper/consistency v2: identity multiplicity and stripped media metadata.

    Messages list innocent explanations alongside suspicious ones."""
    findings: list[Finding] = []

    uas_ids: dict[str, set[str]] = {}
    for e in load_timeline(case, event_type="rid.observation"):
        uas_id = e.payload.get("uas_id")
        if uas_id:
            uas_ids.setdefault(str(uas_id), set()).add(e.evidence_name)
    if len(uas_ids) > 1:
        listing = "; ".join(f"{k} (in {', '.join(sorted(v))})"
                            for k, v in sorted(uas_ids.items()))
        findings.append(Finding(
            check="multiple_uas_identities", severity="warning",
            description=(f"{len(uas_ids)} distinct Remote ID UAS identifiers in this "
                         f"case: {listing}. Consistent with multiple aircraft at the "
                         "scene, a swarm, ID spoofing, or unrelated traffic captured "
                         "by the receiver - resolve against flight-log serials."),
        ))

    for e in load_timeline(case, event_type="media.capture"):
        has_position = e.lat is not None
        has_time = e.ts_utc is not None or e.ts_original is not None
        if not has_position and not has_time:
            findings.append(Finding(
                check="media_metadata_absent", severity="warning",
                description=(f"{e.evidence_name}: no capture time and no GPS position "
                             "in metadata - consistent with stripped metadata, an "
                             "export/re-encode, or a device that never wrote them. "
                             "Compare against original media on the source device."),
                evidence_sha256=e.evidence_sha256, event_ids=[e.event_id]))
        elif not has_position and (e.payload.get("make") or e.payload.get("model")):
            findings.append(Finding(
                check="media_position_absent", severity="info",
                description=(f"{e.evidence_name}: camera metadata present but no GPS "
                             "position - GPS may have been off, unlocked, or the "
                             "location fields removed."),
                evidence_sha256=e.evidence_sha256, event_ids=[e.event_id]))

    return findings


def _scope_checks(case: CaseWorkspace) -> list[Finding]:
    """Flag events outside optional time and location bounds."""
    scope = case.meta.scope
    t_start = parse_iso(scope.start_utc)
    t_end = parse_iso(scope.end_utc)
    has_area = (scope.area_center_lat is not None
                and scope.area_center_lon is not None
                and scope.area_radius_m is not None)
    if not (t_start or t_end or has_area):
        return []

    out_of_time: dict[str, int] = {}
    out_of_area: dict[str, int] = {}
    for e in load_timeline(case):
        ts = parse_iso(e.ts_utc)
        if ts is not None and ((t_start and ts < t_start) or (t_end and ts > t_end)):
            out_of_time[e.evidence_name] = out_of_time.get(e.evidence_name, 0) + 1
        if has_area and e.lat is not None and e.lon is not None:
            dist = haversine_m(e.lat, e.lon, scope.area_center_lat,
                               scope.area_center_lon)
            if dist > scope.area_radius_m:
                out_of_area[e.evidence_name] = out_of_area.get(e.evidence_name, 0) + 1

    findings: list[Finding] = []
    for name, count in sorted(out_of_time.items()):
        findings.append(Finding(
            check="outside_time_scope", severity="warning",
            description=(f"{name}: {count} event(s) timestamped outside the "
                         f"analysis window ({scope.start_utc or 'open'} - "
                         f"{scope.end_utc or 'open'}). Review the time settings "
                         "and source clock."),
        ))
    for name, count in sorted(out_of_area.items()):
        findings.append(Finding(
            check="outside_area_scope", severity="warning",
            description=(f"{name}: {count} positioned event(s) outside the "
                         f"analysis area ({scope.area_radius_m:.0f} m around "
                         f"{scope.area_center_lat:.5f},{scope.area_center_lon:.5f}). "
                         "Review the area settings and position data."),
        ))
    return findings


def run_checks(case: CaseWorkspace) -> list[Finding]:
    findings = (_track_checks(case) + _cross_source_checks(case)
                + _integrity_checks(case) + _identity_checks(case)
                + _scope_checks(case))
    order = {"alert": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.check))
    return findings

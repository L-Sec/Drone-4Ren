"""Analysis-layer tests: timeline, tracks, reconstruction, exports,
correlation, checks, and report - built over the validation fixtures."""

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from droneforen.analysis.checks import run_checks
from droneforen.analysis.correlation import correlate_media
from droneforen.analysis.geospatial import (
    EXPORTERS,
    build_tracks,
    haversine_m,
    media_points,
)
from droneforen.analysis.reconstruction import reconstruct
from droneforen.analysis.timeline import load_timeline, summarize
from droneforen.audit import utc_now_iso
from droneforen.case import CaseWorkspace
from droneforen.db import new_id
from droneforen.hashing import hash_file
from droneforen.parsers.runner import run_parser
from droneforen.report import generate_html_report

FIXTURES = Path(__file__).parent.parent / "validation" / "fixtures"


@pytest.fixture(scope="module")
def flight_case(tmp_path_factory) -> CaseWorkspace:
    """A case with the BIN log, the tlog, and the geotagged photo parsed."""
    root = tmp_path_factory.mktemp("analysis") / "CASE-AN-1"
    case = CaseWorkspace.create(
        root, case_number="AN-1", collector="Test", context="unit test",
        actor="pytest",
    )
    for name, parser in [("synthetic_flight.bin", "ardupilot-bin"),
                         ("synthetic_flight.tlog", "mavlink-tlog"),
                         ("geotagged.jpg", "image-exif")]:
        added = case.add_evidence(FIXTURES / name, actor="pytest")
        outcome = run_parser(case, added.artifact_id, parser, actor="pytest")
        assert outcome.status == "completed", outcome.error
    return case


def test_haversine_sanity():
    # one milli-degree of latitude is ~111.2 m anywhere
    assert haversine_m(47.0, 8.0, 47.001, 8.0) == pytest.approx(111.2, abs=1.0)
    assert haversine_m(47.4, 8.5, 47.4, 8.5) == 0.0


def test_timeline_merges_sources(flight_case):
    summary = summarize(flight_case)
    assert len(summary.sources) == 3
    assert summary.anchored_events > 30
    entries = load_timeline(flight_case, event_type="gps.position")
    assert {e.parser_name for e in entries} == {"ardupilot-bin", "mavlink-tlog"}
    # anchored events are in chronological order
    anchored = [e.ts_utc for e in load_timeline(flight_case) if e.ts_utc]
    assert anchored == sorted(anchored)


def test_tracks_one_per_source(flight_case):
    tracks = build_tracks(flight_case)
    assert len(tracks) == 2
    for track in tracks:
        assert len(track.points) == 8
        # scripted TRACK path length; both sources must agree exactly
        assert track.path_distance_m() == pytest.approx(55.0, abs=2.0)
    assert tracks[0].path_distance_m() == pytest.approx(
        tracks[1].path_distance_m(), abs=0.5)
    media = media_points(flight_case)
    assert len(media) == 1
    assert media[0].ts_utc.startswith("2026-07-08T10:00:05")


def test_reconstruction_derives_phases(flight_case):
    flights = reconstruct(flight_case)
    assert len(flights) == 2
    for f in flights:
        kinds = {i.kind for i in f.items}
        assert "takeoff" in kinds
        assert "rth" in kinds
        assert "last_fix" in kinds
        assert f.duration_s == pytest.approx(6.5, abs=0.01)
        takeoff = next(i for i in f.items if i.kind == "takeoff")
        assert takeoff.ts_utc.startswith("2026-07-08T10:00:03")
        assert takeoff.method  # every derived item states its rule
        assert takeoff.event_ids
    tlog_flight = next(f for f in flights if "tlog" in f.evidence_name)
    assert tlog_flight.max_alt_agl_m == 30.0
    bin_flight = next(f for f in flights if f.evidence_name.endswith(".bin"))
    assert bin_flight.max_alt_msl_m == 518.0
    arm_kinds = [i.kind for i in tlog_flight.items]
    assert "armed" in arm_kinds and "disarmed" in arm_kinds


@pytest.mark.parametrize("fmt", ["kml", "gpx", "geojson", "csv"])
def test_exports_are_well_formed(flight_case, fmt):
    content = EXPORTERS[fmt](build_tracks(flight_case), media_points(flight_case), "AN-1")
    if fmt in ("kml", "gpx"):
        root = ET.fromstring(content)
        assert root.tag.endswith(fmt)
        assert "47.39" in content and "8.54" in content
    elif fmt == "geojson":
        doc = json.loads(content)
        kinds = [f["properties"]["kind"] for f in doc["features"]]
        assert kinds.count("track") == 2
        assert kinds.count("media") == 1
        line = next(f for f in doc["features"]
                    if f["geometry"]["type"] == "LineString")
        assert len(line["geometry"]["coordinates"]) == 8
    else:
        lines = content.strip().splitlines()
        assert lines[0].startswith("kind,ts_utc,lat")
        assert len(lines) == 1 + 8 + 8 + 1  # header + 2 tracks + 1 media row


def test_media_correlation_places_photo_on_track(flight_case):
    results = correlate_media(flight_case)
    assert len(results) == 1
    c = results[0]
    assert c.correlated, c.reason
    assert c.track_evidence_name != c.media_name  # never correlated to itself
    # photo taken at 10:00:05: track position is 47.397860, 8.545720 @ 513 m
    assert c.track_lat == pytest.approx(47.397860, abs=1e-4)
    assert c.track_lon == pytest.approx(8.545720, abs=1e-4)
    # EXIF DMS rounding puts the photo within a few meters of the track
    assert c.offset_m is not None and c.offset_m < 5.0
    assert c.supporting_event_ids


def test_checks_clean_case_has_no_alerts(flight_case):
    findings = run_checks(flight_case)
    assert not [f for f in findings if f.severity == "alert"], findings


def _inject_track(case: CaseWorkspace, name: str, points: list[tuple]) -> None:
    """Insert a synthetic gps.position track directly (test helper)."""
    sha = hashlib.sha256(name.encode()).hexdigest()
    now = utc_now_iso()
    ev_id, art_id, run_id = new_id(), new_id(), new_id()
    with case.connect_db() as conn:
        conn.execute(
            "INSERT INTO evidence_items (id, sha256, md5, size_bytes, original_name, "
            "added_utc, actor, acquisition_method, data_origin, manifest_json) "
            "VALUES (?, ?, 'x', 0, ?, ?, 'pytest', 'logical', 'original_raw', '{}')",
            (ev_id, sha, name, now))
        conn.execute(
            "INSERT INTO artifacts (id, evidence_id, path_within, artifact_type, "
            "detected_by) VALUES (?, ?, ?, 'file', 'test')", (art_id, ev_id, name))
        conn.execute(
            "INSERT INTO parser_runs (id, artifact_id, parser_name, parser_version, "
            "input_sha256, started_utc, status, event_count) "
            "VALUES (?, ?, 'test', '0', ?, ?, 'completed', ?)",
            (run_id, art_id, sha, now, len(points)))
        conn.executemany(
            "INSERT INTO events (id, artifact_id, parser_run_id, source_offset, ts_utc, "
            "event_type, lat, lon, alt_msl, confidence, payload_json) "
            "VALUES (?, ?, ?, ?, ?, 'gps.position', ?, ?, ?, 1.0, '{}')",
            [(new_id(), art_id, run_id, f"p:{i}", ts, lat, lon, alt)
             for i, (ts, lat, lon, alt) in enumerate(points)])
        conn.commit()


def test_checks_flag_impossible_speed_and_time_reversal(tmp_path):
    case = CaseWorkspace.create(
        tmp_path / "CASE-BAD", case_number="BAD-1", collector="t",
        context="unit test", actor="pytest")
    _inject_track(case, "spoofed.log", [
        ("2026-07-08T12:00:00+00:00", 47.4000, 8.5000, 500.0),
        ("2026-07-08T12:00:01+00:00", 47.4001, 8.5001, 500.0),
        ("2026-07-08T12:00:02+00:00", 47.5000, 8.6000, 500.0),   # ~13 km in 1 s
        ("2026-07-08T12:00:01+00:00", 47.5001, 8.6001, 900.0),   # time reversal
    ])
    checks = {f.check for f in run_checks(case)}
    # checks must see source record order: time-sorting would hide the reversal
    assert "impossible_speed" in checks
    assert "time_reversal" in checks


def test_checks_flag_cross_source_disagreement(tmp_path):
    case = CaseWorkspace.create(
        tmp_path / "CASE-DIS", case_number="DIS-1", collector="t",
        context="unit test", actor="pytest")
    base = [(f"2026-07-08T12:00:0{i}+00:00", 47.4000 + i * 1e-5, 8.5000, 500.0)
            for i in range(4)]
    shifted = [(ts, lat + 0.01, lon, alt) for ts, lat, lon, alt in base]  # ~1.1 km off
    _inject_track(case, "aircraft.log", base)
    _inject_track(case, "controller.log", shifted)
    findings = run_checks(case)
    assert any(f.check == "cross_source_disagreement" and f.severity == "alert"
               for f in findings)


def test_html_report_generation(flight_case):
    result = generate_html_report(flight_case, actor="pytest")
    html = result.path.read_text(encoding="utf-8")

    assert "UAS Flight Data Report" in html
    assert flight_case.meta.case_number in html
    assert "SOURCE-DERIVED" in html and "ANALYST-DERIVED" in html
    assert "<svg" in html                      # offline track map embedded
    assert "Reproducibility appendix" in html
    assert "ardupilot-bin" in html and "mavlink-tlog" in html
    assert "hash chain intact" in html

    # sidecar hash matches the file on disk
    assert result.sidecar_path.exists()
    recorded = result.sidecar_path.read_text(encoding="utf-8").split()[0]
    assert recorded == result.sha256 == hash_file(result.path).sha256

    # generation is audited with the report hash
    actions = {r["action"]: r for r in flight_case.audit.records()}
    assert "report.generate" in actions
    assert actions["report.generate"]["params"]["sha256"] == result.sha256

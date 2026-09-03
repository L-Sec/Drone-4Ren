"""Tests for Remote ID, DJI TXT, PDF reports, bundles, and parser coverage."""

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from droneforen.analysis.checks import run_checks
from droneforen.analysis.geospatial import rid_tracks
from droneforen.bundle import export_bundle
from droneforen.case import CaseWorkspace
from droneforen.compat import build_matrix, to_markdown
from droneforen.hashing import hash_file
from droneforen.parsers.dji_txt import _frames_to_events
from droneforen.parsers.runner import run_parser
from droneforen.report_pdf import generate_pdf_report

FIXTURES = Path(__file__).parent.parent / "validation" / "fixtures"
VALIDATION = Path(__file__).parent.parent / "validation"


@pytest.fixture(scope="module")
def rid_case(tmp_path_factory) -> CaseWorkspace:
    """Case with a flight log and a Remote ID capture of the same flight."""
    root = tmp_path_factory.mktemp("phase3") / "CASE-P3-1"
    case = CaseWorkspace.create(
        root, case_number="P3-1", collector="Test", context="unit test",
        actor="pytest",
    )
    for name, parser in [("synthetic_flight.bin", "ardupilot-bin"),
                         ("remoteid_scan.json", "remoteid-json"),
                         ("dji_flightrecord.txt", "dji-txt")]:
        added = case.add_evidence(FIXTURES / name, actor="pytest")
        outcome = run_parser(case, added.artifact_id, parser, actor="pytest")
        assert outcome.status == "completed", outcome.error
    return case


def test_rid_tracks_grouped_by_uas_id(rid_case):
    tracks = rid_tracks(rid_case)
    assert len(tracks) == 1
    assert "1581F5FGD226Q00A7891" in tracks[0].evidence_name
    assert len(tracks[0].points) == 9  # RID import is not downsampled


def test_rid_corroborates_flight_log(rid_case):
    """RID observations ~3 m off the true track must corroborate, not alert."""
    findings = run_checks(rid_case)
    corroborations = [f for f in findings if f.check == "cross_source_corroboration"]
    assert corroborations, [f.check for f in findings]
    assert "RID" in corroborations[0].description
    assert not [f for f in findings if f.check == "cross_source_disagreement"]


def test_dji_txt_recognition_events(rid_case):
    with rid_case.connect_db() as conn:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'artifact.recognized'"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["format"] == "dji-flightrecord-txt"
    assert payload["format_version"] == 12
    assert payload["encrypted"] is False
    assert "Mavic 2 Pro" in payload["details_strings"]
    assert "DJI_API_KEY" in payload["decoding_status"]


def test_dji_decoded_frames_are_downsampled_and_normalized():
    def frame(elapsed: float, lat: float, lon: float):
        osd = SimpleNamespace(
            fly_time=elapsed, latitude=lat, longitude=lon, z_speed=-0.4,
            gps_num=14, gps_level=5, is_gpd_used=True, flyc_state=None,
            flight_action=None, drone_type=None, is_on_ground=False,
            is_motor_on=True, altitude=153.2, height=12.5, pitch=1.0,
            roll=-2.0, yaw=-10.0, h_speed=4.2,
        )
        battery = SimpleNamespace(
            charge_level=88, voltage=11.7, current=3.1, temperature=31.0,
        )
        gimbal = SimpleNamespace(pitch=-30.0, roll=0.0, yaw=2.0)
        rc = SimpleNamespace(downlink_signal=92, uplink_signal=89)
        return SimpleNamespace(osd=osd, battery=battery, gimbal=gimbal, rc=rc)

    events = _frames_to_events([
        frame(0.0, 0.0, 0.0),
        frame(0.4, 40.0, -75.0),
        frame(1.1, 40.0001, -75.0001),
        frame(1.8, 40.0002, -75.0002),
    ])
    assert len(events) == 3
    assert events[0].event_type == "flight.telemetry"
    assert events[1].event_type == events[2].event_type == "gps.position"
    assert events[1].ts_original == "+1.100s"
    assert events[1].heading == 350.0
    assert events[1].payload["battery_percent"] == 88


def test_pdf_report_generation(rid_case):
    result = generate_pdf_report(rid_case, actor="pytest")
    raw = result.path.read_bytes()
    assert raw.startswith(b"%PDF")
    assert result.path.suffix == ".pdf"
    recorded = result.sidecar_path.read_text(encoding="utf-8").split()[0]
    assert recorded == result.sha256 == hash_file(result.path).sha256
    audit = [r for r in rid_case.audit.records() if r["action"] == "report.generate"]
    assert any(r["params"]["format"] == "pdf" and r["params"]["sha256"] == result.sha256
               for r in audit)


def test_bundle_roundtrip(rid_case, tmp_path):
    out = tmp_path / "case-bundle.zip"
    result = export_bundle(rid_case, actor="pytest", out_path=out)
    assert result.path == out
    assert hash_file(out).sha256 == result.sha256

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("BUNDLE-MANIFEST.json"))
        # every manifest file is in the zip, hashes match extracted bytes
        assert manifest["case_number"] == "P3-1"
        assert manifest["file_count"] == len(manifest["files"])
        for entry in manifest["files"]:
            arcname = f"{rid_case.root.name}/{entry['path']}"
            assert arcname in names
            import hashlib

            assert hashlib.sha256(zf.read(arcname)).hexdigest() == entry["sha256"]
        # the audit log inside the bundle records the export itself
        audit_bytes = zf.read(f"{rid_case.root.name}/audit/audit.jsonl")
        assert b'"export.bundle"' in audit_bytes


def test_bundle_refuses_destination_inside_case(rid_case):
    with pytest.raises(ValueError, match="outside the case directory"):
        export_bundle(rid_case, actor="pytest",
                      out_path=rid_case.root / "reports" / "self.zip")


def test_compat_matrix_reflects_manifests_and_coverage():
    matrix = build_matrix(VALIDATION)
    by_name = {row["parser"]: row for row in matrix}
    assert by_name["dji-txt"]["support_level"] == "partial"
    assert by_name["ardupilot-bin"]["support_level"] == "full"
    assert "dji_flightrecord.txt" in by_name["dji-txt"]["validated_fixtures"]
    assert "remoteid_scan.json" in by_name["remoteid-json"]["validated_fixtures"]

    md = to_markdown(matrix)
    assert "| dji-txt | 0.2.0 | partial |" in md
    assert "Known limitations" in md
    assert "DJI_API_KEY" in md

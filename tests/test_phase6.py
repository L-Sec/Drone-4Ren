"""Tests for readiness logs, scope checks, and the feasibility model."""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from droneforen.analysis.checks import run_checks
from droneforen.analysis.feasibility import (
    FeasibilityInputs,
    assess_flight_feasibility,
    estimate_endurance,
)
from droneforen.case import CaseWorkspace
from droneforen.models import CaseScope
from droneforen.parsers.readiness_jsonl import ReadinessJsonlParser
from droneforen.parsers.runner import run_parser
from droneforen.readiness import ReadinessError, ReadinessStore

FIXTURES = Path(__file__).parent.parent / "validation" / "fixtures"

T_JULY8_10 = datetime(2026, 7, 8, 10, 30, tzinfo=UTC)
T_JULY9_11 = datetime(2026, 7, 9, 11, 15, tzinfo=UTC)
T_AUG20 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _observations():
    doc = json.loads((FIXTURES / "remoteid_scan.json").read_text(encoding="utf-8"))
    return doc["observations"]


@pytest.fixture
def store(tmp_path) -> ReadinessStore:
    return ReadinessStore.create(
        tmp_path / "readiness-site-a", site="Site A perimeter",
        context="perimeter exercise",
        collection_purpose="counter-UAS monitoring of restricted airspace",
        retention_days=30, actor="pytest")


def test_readiness_requires_deployment_docs(tmp_path):
    with pytest.raises(TypeError):
        ReadinessStore.create(tmp_path / "x", site="s", actor="pytest")  # noqa


def test_ingest_rotation_and_verify(store):
    obs = _observations()
    assert store.ingest(obs[:4], actor="pytest", sensor="rx-1", now=T_JULY8_10) == 4
    # next hour-window ingest closes the previous segment with a hash sidecar
    assert store.ingest(obs[4:], actor="pytest", sensor="rx-1", now=T_JULY9_11) == 5
    segments = store.segments()
    assert len(segments) == 2
    closed = segments[0].with_suffix(segments[0].suffix + ".sha256")
    assert closed.exists()

    result = store.verify()
    assert result.ok, result.problems
    assert result.segments_checked == 2
    assert result.records_total == 9

    actions = [r["action"] for r in store.audit.records()]
    assert actions.count("readiness.ingest") == 2
    assert "readiness.segment.close" in actions


def test_readiness_tamper_detected(store):
    store.ingest(_observations()[:3], actor="pytest", sensor="rx-1", now=T_JULY8_10)
    seg = store.segments()[0]
    lines = seg.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"rssi"', '"RSSI"')
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = store.verify()
    assert not result.ok
    assert any("chain broken" in p for p in result.problems)


def test_prune_archives_and_records(store, tmp_path):
    store.ingest(_observations()[:2], actor="pytest", sensor="rx-1", now=T_JULY8_10)
    store.ingest(_observations()[2:4], actor="pytest", sensor="rx-1", now=T_AUG20)

    with pytest.raises(ReadinessError):
        store.prune(actor="pytest", now=T_AUG20)  # neither archive nor delete chosen

    archive = tmp_path / "archive"
    pruned = store.prune(actor="pytest", archive_dir=archive, now=T_AUG20)
    assert pruned == ["obs-20260708T10.jsonl"]
    assert (archive / "obs-20260708T10.jsonl").exists()
    assert len(store.segments()) == 1

    archive_records = [r for r in store.audit.records()
                       if r["action"] == "readiness.segment.archive"]
    assert archive_records and archive_records[0]["params"]["sha256"]


def test_promote_and_parse_segment(store, tmp_path):
    store.ingest(_observations(), actor="pytest", sensor="rx-1", now=T_JULY8_10)
    case = CaseWorkspace.create(
        tmp_path / "CASE-RD", case_number="RD-1", collector="t",
        context="local", actor="pytest")
    added = store.promote("obs-20260708T10.jsonl", case, actor="pytest")
    manifest = case.store.load_manifest(added.manifest.sha256)
    assert manifest.acquisition_method == "readiness-promote"
    assert "Site A perimeter" in (manifest.description or "")

    # promoted segment auto-detects as readiness-jsonl and parses with chain OK
    data_path = case.store.data_path(added.manifest.sha256)
    assert ReadinessJsonlParser.detect(data_path)
    outcome = run_parser(case, added.artifact_id, "readiness-jsonl", actor="pytest")
    assert outcome.status == "completed"
    with case.connect_db() as conn:
        summary = conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'parser.summary'"
        ).fetchone()
        rid_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'rid.observation'"
        ).fetchone()[0]
    payload = json.loads(summary["payload_json"])
    assert payload["chain_valid"] is True
    assert rid_count == 9

    # promotion recorded on both sides
    assert any(r["action"] == "readiness.promote" for r in store.audit.records())
    assert any(r["action"] == "evidence.add" for r in case.audit.records())


def test_parser_reports_broken_chain(store, tmp_path):
    store.ingest(_observations()[:5], actor="pytest", sensor="rx-1", now=T_JULY8_10)
    seg = store.segments()[0]
    lines = seg.read_text(encoding="utf-8").splitlines()
    del lines[2]
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = ReadinessJsonlParser().parse(seg)
    types = [e.event_type for e in result.events]
    assert "integrity.chain_broken" in types
    summary = next(e for e in result.events if e.event_type == "parser.summary")
    assert summary.payload["chain_valid"] is False


def test_scope_checks_flag_out_of_scope(tmp_path):
    scope = CaseScope(
        start_utc="2026-07-08T10:00:03+00:00",   # scripted flight starts 10:00:02
        end_utc="2026-07-08T10:00:07+00:00",
        area_center_lat=47.397742, area_center_lon=8.545594, area_radius_m=20.0,
    )
    case = CaseWorkspace.create(
        tmp_path / "CASE-SCOPE", case_number="SC-1", collector="t",
        context="local", actor="pytest", scope=scope)
    added = case.add_evidence(FIXTURES / "synthetic_flight.bin", actor="pytest")
    assert run_parser(case, added.artifact_id, "ardupilot-bin",
                      actor="pytest").status == "completed"

    findings = {f.check: f for f in run_checks(case)}
    assert "outside_time_scope" in findings
    assert "analysis window" in findings["outside_time_scope"].description
    assert "outside_area_scope" in findings  # track reaches ~35 m from center


def test_no_scope_means_no_scope_findings(tmp_path):
    case = CaseWorkspace.create(
        tmp_path / "CASE-NOSCOPE", case_number="NS-1", collector="t",
        context="local", actor="pytest")
    added = case.add_evidence(FIXTURES / "synthetic_flight.bin", actor="pytest")
    run_parser(case, added.artifact_id, "ardupilot-bin", actor="pytest")
    checks = {f.check for f in run_checks(case)}
    assert not {c for c in checks if c.startswith("outside_")}


def test_feasibility_interval_sanity():
    # DJI-Mini-class: 0.25 kg, 17 Wh -> tens of minutes, min < max
    small = estimate_endurance(FeasibilityInputs(mass_kg=0.25, battery_wh=17.0,
                                                 rotors=4, rotor_diameter_m=0.12))
    assert small[2] < small[3]
    assert 5 * 60 < small[3] < 90 * 60
    # same battery lifting 5 kg cannot hover long
    heavy = estimate_endurance(FeasibilityInputs(mass_kg=5.0, battery_wh=17.0,
                                                 rotors=4, rotor_diameter_m=0.12))
    assert heavy[3] < small[2]

    with pytest.raises(ValueError):
        estimate_endurance(FeasibilityInputs(mass_kg=-1, battery_wh=17.0))


def test_feasibility_verdicts(tmp_path):
    inputs = FeasibilityInputs(mass_kg=0.9, battery_wh=43.0, rotors=4,
                               rotor_diameter_m=0.22)
    ok = assess_flight_feasibility(None, inputs, observed_duration_s=10 * 60)
    assert ok.verdict == "within_model"
    impossible = assess_flight_feasibility(None, inputs,
                                           observed_duration_s=5 * 3600)
    assert impossible.verdict == "exceeds_model"
    assert "not, by itself, proof" in impossible.caveat
    assert any("mass" in a for a in impossible.assumptions)

    # from a case: uses longest reconstructed duration (fixture flight = 6.5 s)
    case = CaseWorkspace.create(
        tmp_path / "CASE-FEAS", case_number="FE-1", collector="t",
        context="local", actor="pytest")
    added = case.add_evidence(FIXTURES / "synthetic_flight.bin", actor="pytest")
    run_parser(case, added.artifact_id, "ardupilot-bin", actor="pytest")
    from_case = assess_flight_feasibility(case, inputs)
    assert from_case.observed_duration_s == pytest.approx(6.5, abs=0.01)
    assert from_case.verdict == "within_model"


def test_readiness_segments_survive_readonly_promotion(store, tmp_path):
    """Promoted segment is copied; the readiness original stays writable for
    continued chain appends within its hour window."""
    store.ingest(_observations()[:2], actor="pytest", sensor="rx-1", now=T_JULY8_10)
    case = CaseWorkspace.create(
        tmp_path / "CASE-RD2", case_number="RD-2", collector="t",
        context="local", actor="pytest")
    store.promote("obs-20260708T10.jsonl", case, actor="pytest")
    seg = store.segments()[0]
    assert os.stat(seg).st_mode & stat.S_IWRITE  # original not frozen
    # stored copy in the case IS frozen
    sha = case.store.iter_hashes()[0]
    assert not (os.stat(case.store.data_path(sha)).st_mode & stat.S_IWRITE)

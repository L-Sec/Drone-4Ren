"""Tests for entity links, consistency checks, and scenario worksheets."""

from pathlib import Path

import pytest

from droneforen.analysis.checks import run_checks
from droneforen.analysis.entities import build_entity_graph
from droneforen.analysis.scenarios import evaluate_scenario, list_scenarios
from droneforen.case import CaseWorkspace
from droneforen.parsers.runner import run_parser

FIXTURES = Path(__file__).parent.parent / "validation" / "fixtures"


@pytest.fixture(scope="module")
def linked_case(tmp_path_factory) -> CaseWorkspace:
    """Flight log + RID + DJI TXT + photo: rich entity material."""
    root = tmp_path_factory.mktemp("phase5") / "CASE-P5-1"
    case = CaseWorkspace.create(
        root, case_number="P5-1", collector="Test", context="unit test",
        actor="pytest")
    for name, parser in [("synthetic_flight.bin", "ardupilot-bin"),
                         ("remoteid_scan.json", "remoteid-json"),
                         ("dji_flightrecord.txt", "dji-txt"),
                         ("geotagged.jpg", "image-exif")]:
        added = case.add_evidence(FIXTURES / name, actor="pytest")
        outcome = run_parser(case, added.artifact_id, parser, actor="pytest")
        assert outcome.status == "completed", outcome.error
    return case


def test_entity_graph_nodes(linked_case):
    graph = build_entity_graph(linked_case)
    by_type = {}
    for n in graph.nodes:
        by_type.setdefault(n.type, []).append(n)

    assert [n.label for n in by_type["uas_id"]] == ["1581F5FGD226Q00A7891"]
    assert by_type["uas_id"][0].attribution_level == "device"
    assert by_type["mac"][0].attribution_level == "device"
    assert by_type["operator_position"][0].attribution_level == "operator"
    # serial candidate extracted from the DJI TXT details block
    assert any(n.label == "SN0123456789AB" for n in by_type["serial_candidate"])
    # camera from EXIF, firmware from ArduPilot MSG
    assert any("FC3582" in n.label for n in by_type["camera"])
    assert any(n.label.startswith("ArduCopter") for n in by_type["firmware"])
    # launch/landing sites for flight log and RID tracks
    assert len(by_type["launch_site"]) == 2
    assert "ownership" in graph.caveat


def test_entity_graph_edges(linked_case):
    graph = build_entity_graph(linked_case)
    relations = {e.relation for e in graph.edges}
    assert "same_site" in relations       # BIN and RID launch sites ~3 m apart
    assert "observed_together" in relations  # uas_id + mac + operator in RID file
    together = [e for e in graph.edges if e.relation == "observed_together"]
    assert any("uas_id" in e.a and "mac:" in e.b or "mac:" in e.a for e in together)
    # no owner-level attributions exist anywhere
    assert all(n.attribution_level in ("device", "account", "operator", "location")
               for n in graph.nodes)


def test_media_metadata_absent_check(tmp_path):
    from PIL import Image

    case = CaseWorkspace.create(
        tmp_path / "CASE-STRIP", case_number="STRIP-1", collector="t",
        context="unit test", actor="pytest")
    bare = tmp_path / "stripped.jpg"
    Image.new("RGB", (32, 32), (5, 5, 5)).save(bare, "JPEG")
    added = case.add_evidence(bare, actor="pytest")
    assert run_parser(case, added.artifact_id, "image-exif",
                      actor="pytest").status == "completed"
    checks = {f.check for f in run_checks(case)}
    assert "media_metadata_absent" in checks


def test_multiple_uas_identities_check(tmp_path):
    import json as jsonlib

    case = CaseWorkspace.create(
        tmp_path / "CASE-MULTI", case_number="MULTI-1", collector="t",
        context="unit test", actor="pytest")
    rid = tmp_path / "two_drones.json"
    rid.write_text(jsonlib.dumps([
        {"time": "2026-07-08T12:00:00+00:00", "basic_id": "DRONE-A",
         "lat": 47.40, "lon": 8.50, "rssi": -60},
        {"time": "2026-07-08T12:00:01+00:00", "basic_id": "DRONE-B",
         "lat": 47.41, "lon": 8.51, "rssi": -70},
    ]), encoding="utf-8")
    added = case.add_evidence(rid, actor="pytest")
    assert run_parser(case, added.artifact_id, "remoteid-json",
                      actor="pytest").status == "completed"
    findings = run_checks(case)
    multi = [f for f in findings if f.check == "multiple_uas_identities"]
    assert multi and multi[0].severity == "warning"
    assert "DRONE-A" in multi[0].description and "DRONE-B" in multi[0].description
    # phrasing stays non-accusatory
    assert "unrelated traffic" in multi[0].description


def test_scenario_listing_and_unknown():
    names = {s["scenario"] for s in list_scenarios()}
    assert names == {"crash", "flyaway", "surveillance", "intrusion"}


def test_scenario_evaluation(linked_case):
    with pytest.raises(KeyError):
        evaluate_scenario(linked_case, "heist")

    flyaway = evaluate_scenario(linked_case, "flyaway")
    by_name = {s.name: s for s in flyaway.signals}
    assert by_name["return-to-home engaged"].status == "present"
    assert by_name["track ends far from launch"].status == "absent"  # ~20 m
    assert by_name["wind and interference conditions"].status == "manual"
    assert "not conclusions" in flyaway.caveat

    crash = evaluate_scenario(linked_case, "crash")
    by_name = {s.name: s for s in crash.signals}
    # fixture flight has both armed and disarmed events
    assert by_name["log ends without disarm"].status == "absent"

    surveillance = evaluate_scenario(linked_case, "surveillance")
    by_name = {s.name: s for s in surveillance.signals}
    assert by_name["Remote ID observed at scene"].status == "present"
    assert by_name["media captured during activity"].status == "present"

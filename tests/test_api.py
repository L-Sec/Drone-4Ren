"""API tests: the FastAPI service over a real case, via TestClient."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from droneforen.api import create_app
from droneforen.case import CaseWorkspace

FIXTURES = Path(__file__).parent.parent / "validation" / "fixtures"


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    root = tmp_path_factory.mktemp("api") / "CASE-API-1"
    CaseWorkspace.create(root, case_number="API-1", collector="Test",
                         context="unit test", actor="pytest")
    app = create_app(root, actor="api-test")
    return TestClient(app)


def _intake(client: TestClient, fixture: str, acknowledged: bool = True):
    with open(FIXTURES / fixture, "rb") as fh:
        return client.post(
            "/api/evidence",
            files={"file": (fixture, fh)},
            data={"acknowledged": "true" if acknowledged else "false",
                  "description": f"test intake of {fixture}",
                  "wb_make": "Tableau", "wb_test_result": "pass"},
        )


def test_case_endpoint(client):
    data = client.get("/api/case").json()
    assert data["meta"]["case_number"] == "API-1"
    assert data["actor"] == "api-test"


def test_intake_requires_acknowledgment(client):
    res = _intake(client, "synthetic_flight.bin", acknowledged=False)
    assert res.status_code == 400
    assert "warnings" in res.json()["detail"]


def test_intake_parse_timeline_flow(client):
    res = _intake(client, "synthetic_flight.bin")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["detected_parsers"][0] == "ardupilot-bin"
    assert body["write_blocker_documented"] is True

    # duplicate intake refused
    assert _intake(client, "synthetic_flight.bin").status_code == 409

    res = client.post("/api/parse", json={"sha_prefix": body["sha256"][:12]})
    assert res.status_code == 200, res.text
    outcome = res.json()
    assert outcome["status"] == "completed"
    assert outcome["event_count"] == 17

    data = client.get("/api/timeline", params={"limit": 5}).json()
    assert data["summary"]["total_events"] == 17
    assert len(data["entries"]) == 5
    page2 = client.get("/api/timeline", params={"limit": 5, "offset": 5}).json()
    assert page2["entries"][0] != data["entries"][0]

    filtered = client.get("/api/timeline",
                          params={"event_type": "gps.position"}).json()
    assert all(e["event_type"] == "gps.position" for e in filtered["entries"])


def test_tracks_flights_checks(client):
    _intake(client, "remoteid_scan.json")
    sha = client.get("/api/evidence").json()["items"][-1]["sha256"]
    assert client.post("/api/parse", json={"sha_prefix": sha}).json()[
        "parser_name"] == "remoteid-json"

    gj = client.get("/api/tracks").json()
    kinds = [f["properties"].get("kind") for f in gj["features"]]
    assert kinds.count("track") == 2  # flight log + RID

    flights = client.get("/api/flights").json()["flights"]
    assert len(flights) == 1
    assert {i["kind"] for i in flights[0]["items"]} >= {"takeoff", "rth"}

    findings = client.get("/api/checks").json()["findings"]
    assert any(f["check"] == "cross_source_corroboration" for f in findings)


def test_verify_and_audit(client):
    v = client.post("/api/verify").json()
    assert v["ok"] is True
    audit = client.get("/api/audit").json()
    actions = [r["action"] for r in audit["records"]]
    assert "api.serve.start" in actions
    assert "evidence.add" in actions and "case.verify" in actions


def test_report_and_export_endpoints(client):
    out = client.post("/api/report", json={"format": "html"}).json()
    assert out["name"].endswith(".html")
    reports = client.get("/api/reports").json()["reports"]
    names = [r["name"] for r in reports]
    assert out["name"] in names
    assert f"{out['name']}.sha256" in names
    # generated report is downloadable through /files
    res = client.get(f"/files/{out['name']}")
    assert res.status_code == 200
    assert "UAS Flight Data Report" in res.text

    exp = client.post("/api/export", json={"format": "geojson"}).json()
    assert exp["name"] == "track.geojson"
    assert client.post("/api/export", json={"format": "nope"}).status_code == 422


def test_gui_served(client):
    from droneforen import __version__

    res = client.get("/")
    assert res.status_code == 200
    assert "<title>Drone 4Ren</title>" in res.text
    # asset URLs are version-stamped for cache-busting; the token must be
    # substituted, never served literally
    assert "__DRONEFOREN_VERSION__" not in res.text
    assert f"/static/style.css?v={__version__}" in res.text
    assert f"/static/app.js?v={__version__}" in res.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_localhost_security_boundary(client):
    res = client.get("/api/case")
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]

    foreign_host = client.get("/api/case", headers={"host": "attacker.example"})
    assert foreign_host.status_code == 400

    foreign_origin = client.post(
        "/api/verify",
        headers={"origin": "https://attacker.example"},
    )
    assert foreign_origin.status_code == 403


def test_parsers_endpoint(client):
    parsers = client.get("/api/parsers").json()["parsers"]
    names = {p["parser"] for p in parsers}
    assert {"ardupilot-bin", "dji-txt", "remoteid-json"} <= names

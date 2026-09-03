import json

import pytest

from droneforen.case import CaseError, CaseWorkspace


def test_create_builds_layout(case):
    for sub in ("evidence", "db", "audit", "notes", "reports"):
        assert (case.root / sub).is_dir()
    assert (case.root / "case.json").is_file()
    assert case.db_path.is_file()

    meta = json.loads((case.root / "case.json").read_text(encoding="utf-8"))
    assert meta["case_number"] == "TEST-0001"
    assert meta["context"] == "unit test"


def test_create_refuses_non_empty_dir(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "junk.txt").write_text("x")
    with pytest.raises(CaseError):
        CaseWorkspace.create(
            target, case_number="X", collector="c", context="unit test", actor="pytest"
        )


def test_open_roundtrip(case):
    reopened = CaseWorkspace.open(case.root)
    assert reopened.meta.case_id == case.meta.case_id
    assert reopened.meta.case_number == case.meta.case_number


def test_open_accepts_legacy_authority_field(case):
    case_file = case.root / "case.json"
    doc = json.loads(case_file.read_text(encoding="utf-8"))
    doc["legal_authority"] = doc.pop("context")
    case_file.write_text(json.dumps(doc), encoding="utf-8")

    reopened = CaseWorkspace.open(case.root)
    assert reopened.meta.context == "unit test"


def test_actions_are_audited(case, sample_log):
    case.add_evidence(sample_log, actor="pytest")
    case.verify(actor="pytest")
    actions = [r["action"] for r in case.audit.records()]
    assert actions[0] == "case.create"
    assert "evidence.add" in actions
    assert "case.verify" in actions


def test_fresh_case_verifies_clean(case):
    result = case.verify(actor="pytest")
    assert result.ok
    assert result.audit.record_count >= 1

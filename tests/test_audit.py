import json
from pathlib import Path

from droneforen.audit import GENESIS_HASH, AuditLog


def make_log(tmp_path: Path) -> AuditLog:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("alice", "case.create", target="C-1")
    log.append("alice", "evidence.add", target="abc123", params={"size_bytes": 10})
    log.append("bob", "case.verify", target="C-1")
    return log


def test_chain_is_valid_after_appends(tmp_path):
    log = make_log(tmp_path)
    result = log.verify()
    assert result.ok
    assert result.record_count == 3
    assert result.head_sha256 != GENESIS_HASH
    assert log.records()[0]["prev_sha256"] == GENESIS_HASH


def test_modified_record_breaks_chain(tmp_path):
    log = make_log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["actor"] = "mallory"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = log.verify()
    assert not result.ok
    assert any("chain broken" in p for p in result.problems)


def test_deleted_interior_record_breaks_chain(tmp_path):
    log = make_log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = log.verify()
    assert not result.ok


def test_non_canonical_serialization_is_flagged(tmp_path):
    log = make_log(tmp_path)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    # Same JSON value, different byte serialization: whitespace added.
    record = json.loads(lines[2])
    lines[2] = json.dumps(record, sort_keys=True, indent=None, separators=(", ", ": "))
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = log.verify()
    assert not result.ok


def test_corrupt_log_remains_appendable_and_flagged(tmp_path):
    """Regression: a corrupt line (e.g. BOM, garbage) must not crash append -
    the verify action reporting the corruption is itself audited."""
    log = make_log(tmp_path)
    raw = log.path.read_bytes()
    log.path.write_bytes(b"\xef\xbb\xbf" + raw)  # BOM in front of record 1

    log.append("alice", "case.verify", target="C-1")  # must not raise
    result = log.verify()
    assert not result.ok
    assert result.problems


def test_sequence_numbers_are_monotonic(tmp_path):
    log = make_log(tmp_path)
    records = log.records()
    assert [r["seq"] for r in records] == [1, 2, 3]

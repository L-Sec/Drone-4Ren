import json
from pathlib import Path

from droneforen.parsers import detect_parsers, get_parser, load_registry
from droneforen.parsers.plaintext import PlainTextParser
from droneforen.parsers.runner import run_parser


def test_registry_contains_builtin():
    registry = load_registry()
    assert "plaintext" in registry
    assert get_parser("plaintext") is registry["plaintext"]


def test_detection_is_content_based(tmp_path: Path):
    text = tmp_path / "no_extension_at_all"
    text.write_text("hello\n", encoding="utf-8")
    binary = tmp_path / "misleading.txt"
    binary.write_bytes(b"\x00\x01\x02\x03")

    assert PlainTextParser.detect(text)
    assert not PlainTextParser.detect(binary)
    assert "plaintext" in detect_parsers(text)
    assert "plaintext" not in detect_parsers(binary)


def test_plaintext_events_have_offsets(sample_log):
    result = PlainTextParser().parse(sample_log)
    assert len(result.events) == 3  # blank line skipped
    assert result.events[0].source_offset == "line:1"
    assert result.events[0].event_type == "text.line"
    assert result.events[2].payload["text"].endswith("landed")


def _events_snapshot(case):
    with case.connect_db() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT event_type, source_offset, payload_json FROM events "
                "ORDER BY source_offset"
            )
        ]


def test_runner_records_provenance(case, sample_log):
    added = case.add_evidence(sample_log, actor="pytest")
    outcome = run_parser(case, added.artifact_id, "plaintext", actor="pytest")
    assert outcome.status == "completed"
    assert outcome.event_count == 3

    with case.connect_db() as conn:
        run = conn.execute(
            "SELECT * FROM parser_runs WHERE id = ?", (outcome.run_id,)
        ).fetchone()
        assert run["status"] == "completed"
        assert run["input_sha256"] == added.manifest.sha256
        assert run["parser_version"] == "0.1.0"

        events = conn.execute(
            "SELECT * FROM events WHERE parser_run_id = ?", (outcome.run_id,)
        ).fetchall()
        assert len(events) == 3
        for event in events:
            assert event["artifact_id"] == added.artifact_id
            assert event["parser_run_id"] == outcome.run_id
            assert json.loads(event["payload_json"])["text"]

    audit_actions = [r["action"] for r in case.audit.records()]
    assert "evidence.parse" in audit_actions


def test_reparse_is_reproducible(case, sample_log):
    """Same evidence + same parser version must yield identical event content."""
    added = case.add_evidence(sample_log, actor="pytest")
    first = run_parser(case, added.artifact_id, "plaintext", actor="pytest")
    snapshot_one = _events_snapshot(case)

    with case.connect_db() as conn:
        conn.execute("DELETE FROM events WHERE parser_run_id = ?", (first.run_id,))
        conn.commit()

    second = run_parser(case, added.artifact_id, "plaintext", actor="pytest")
    assert second.status == "completed"
    assert _events_snapshot(case) == snapshot_one


def test_runner_handles_non_ascii_content(case, tmp_path):
    """Regression: sandbox stdout must be UTF-8 regardless of console codepage.

    A UTF-8 BOM plus non-cp1252 characters previously crashed the sandbox
    subprocess on Windows (cp1252 stdout), failing the run with 'no output'.
    """
    log = tmp_path / "intl_log.txt"
    log.write_bytes("höhe 120м ✓ 東京\n".encode("utf-8-sig"))
    added = case.add_evidence(log, actor="pytest")

    outcome = run_parser(case, added.artifact_id, "plaintext", actor="pytest")
    assert outcome.status == "completed", outcome.error
    assert outcome.event_count == 1
    with case.connect_db() as conn:
        payload = conn.execute("SELECT payload_json FROM events").fetchone()[0]
    text = json.loads(payload)["text"]
    assert text == "höhe 120м ✓ 東京"  # BOM stripped, characters intact


def test_sandbox_reports_parser_exception(tmp_path):
    """The sandbox subprocess turns a crashing parser into a structured error."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "droneforen.parsers.sandbox", "plaintext",
         str(tmp_path / "does-not-exist.txt")],
        capture_output=True, text=True, encoding="utf-8",
    )
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert "error" in payload


def test_failed_run_is_recorded_and_case_stays_clean(case, sample_log, monkeypatch):
    """A crashing sandbox poisons only its own parser run, never the case."""
    import subprocess

    added = case.add_evidence(sample_log, actor="pytest")

    def broken_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("droneforen.parsers.runner.subprocess.run", broken_run)
    outcome = run_parser(case, added.artifact_id, "plaintext", actor="pytest")
    assert outcome.status == "failed"
    assert outcome.event_count == 0

    with case.connect_db() as conn:
        run = conn.execute(
            "SELECT status, error FROM parser_runs WHERE id = ?", (outcome.run_id,)
        ).fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert run["status"] == "failed"
    assert run["error"]
    assert event_count == 0

    monkeypatch.undo()
    assert case.verify(actor="pytest").ok

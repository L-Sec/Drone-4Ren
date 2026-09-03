from typer.testing import CliRunner

from droneforen.cli import app

runner = CliRunner()


def _create_case(tmp_path):
    case_dir = tmp_path / "CASE-CLI-1"
    result = runner.invoke(
        app,
        [
            "case", "create", str(case_dir),
            "--case-number", "CLI-1",
        ],
    )
    assert result.exit_code == 0, result.output
    return case_dir


def test_case_uses_non_identifying_defaults(tmp_path):
    case_dir = _create_case(tmp_path)
    result = runner.invoke(app, ["case", "info", str(case_dir)])
    assert result.exit_code == 0
    assert '"collector": "operator"' in result.output
    assert '"context": "local"' in result.output


def test_end_to_end_workflow(tmp_path, sample_log):
    case_dir = _create_case(tmp_path)

    result = runner.invoke(
        app,
        [
            "evidence", "add", str(case_dir), str(sample_log),
            "--description", "SD card flight notes",
            "--wb-make", "Tableau", "--wb-model", "T8u",
            "--actor", "cli-test",
        ],
    )
    assert result.exit_code == 0, result.output
    sha_prefix = next(
        line.split()[-1] for line in result.output.splitlines() if "sha256:" in line
    )[:12]

    result = runner.invoke(app, ["evidence", "list", str(case_dir), "--actor", "cli-test"])
    assert result.exit_code == 0
    assert "flight_notes.txt" in result.output

    result = runner.invoke(
        app, ["evidence", "parse", str(case_dir), sha_prefix, "--actor", "cli-test"]
    )
    assert result.exit_code == 0, result.output
    assert "completed" in result.output
    assert "events:  3" in result.output

    result = runner.invoke(
        app, ["evidence", "events", str(case_dir), "--actor", "cli-test"]
    )
    assert result.exit_code == 0
    assert "text.line" in result.output
    assert "plaintext" in result.output

    result = runner.invoke(app, ["case", "verify", str(case_dir), "--actor", "cli-test"])
    assert result.exit_code == 0, result.output
    assert "VERIFY OK" in result.output

    result = runner.invoke(app, ["case", "log", str(case_dir), "--actor", "cli-test"])
    assert result.exit_code == 0
    for action in ("case.create", "evidence.add", "evidence.parse", "case.verify"):
        assert action in result.output


def test_verify_fails_on_tampered_evidence(tmp_path, sample_log):
    import os
    import stat

    case_dir = _create_case(tmp_path)
    result = runner.invoke(
        app, ["evidence", "add", str(case_dir), str(sample_log), "--actor", "cli-test"]
    )
    assert result.exit_code == 0

    # Corrupt the stored original.
    from droneforen.case import CaseWorkspace

    case = CaseWorkspace.open(case_dir)
    sha = case.store.iter_hashes()[0]
    stored = case.store.data_path(sha)
    os.chmod(stored, stat.S_IWRITE)
    stored.write_bytes(b"evil")

    result = runner.invoke(app, ["case", "verify", str(case_dir), "--actor", "cli-test"])
    assert result.exit_code == 1
    assert "VERIFY FAILED" in result.output or "HASH MISMATCH" in result.output


def test_parsers_list():
    result = runner.invoke(app, ["parsers", "list"])
    assert result.exit_code == 0
    assert "plaintext" in result.output

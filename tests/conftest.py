from pathlib import Path

import pytest

from droneforen.case import CaseWorkspace


@pytest.fixture
def case(tmp_path: Path) -> CaseWorkspace:
    return CaseWorkspace.create(
        tmp_path / "CASE-TEST-0001",
        case_number="TEST-0001",
        collector="Test User",
        context="unit test",
        actor="pytest",
    )


@pytest.fixture
def sample_log(tmp_path: Path) -> Path:
    path = tmp_path / "flight_notes.txt"
    path.write_text(
        "2026-07-08 10:00:01 armed\n"
        "2026-07-08 10:00:05 takeoff\n"
        "\n"
        "2026-07-08 10:04:59 landed\n",
        encoding="utf-8",
    )
    return path

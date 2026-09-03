"""Golden-dataset validation (ARCHITECTURE.md §8).

Each fixture in validation/fixtures/ has a frozen expected-Event file in
validation/expected/. Any behavior change in a parser shows up here as a
diff against the golden; intentional changes are made by re-running
validation/make_expected.py and reviewing the golden diff in code review.
"""

import json
from pathlib import Path

import pytest

from droneforen.parsers import detect_parsers, get_parser

VALIDATION = Path(__file__).parent.parent / "validation"

CASES = [
    ("synthetic_flight.tlog", "mavlink-tlog"),
    ("synthetic_flight.bin", "ardupilot-bin"),
    ("synthetic_flight.ulg", "px4-ulog"),
    ("dji_bracket.srt", "dji-srt"),
    ("dji_legacy.srt", "dji-srt"),
    ("geotagged.jpg", "image-exif"),
    ("dji_flightrecord.txt", "dji-txt"),
    ("remoteid_scan.json", "remoteid-json"),
]


@pytest.mark.parametrize(("fixture", "parser_name"), CASES)
def test_parser_output_matches_golden(fixture: str, parser_name: str):
    path = VALIDATION / "fixtures" / fixture
    golden_path = VALIDATION / "expected" / f"{fixture}.expected.json"
    assert path.exists(), "fixture missing: run validation/generate_fixtures.py"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))

    cls = get_parser(parser_name)
    assert golden["parser_version"] == cls.manifest.version, (
        "parser version changed without regenerating goldens "
        "(run validation/make_expected.py and review the diff)"
    )
    result = cls().parse(path)
    actual = [e.model_dump(mode="json", exclude_none=True) for e in result.events]
    assert actual == golden["events"]


@pytest.mark.parametrize(("fixture", "parser_name"), CASES)
def test_fixture_detection_routes_to_right_parser(fixture: str, parser_name: str):
    """Auto-detection must pick the format parser, not the plaintext fallback."""
    path = VALIDATION / "fixtures" / fixture
    detected = detect_parsers(path)
    assert detected, f"no parser detects {fixture}"
    assert detected[0] == parser_name


def test_generic_srt_is_not_claimed_by_dji_parser(tmp_path: Path):
    """A movie-subtitle SRT without DJI telemetry must fall through to plaintext."""
    srt = tmp_path / "movie.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nHello there.\n\n"
        "2\n00:00:05,000 --> 00:00:08,000\nGeneral subtitle.\n",
        encoding="utf-8",
    )
    detected = detect_parsers(srt)
    assert "dji-srt" not in detected
    assert detected and detected[0] == "plaintext"


def test_cross_source_positions_agree():
    """The same scripted flight parsed from three formats must reconstruct the
    same track - the core cross-correlation premise of the tool."""

    def positions(fixture: str, parser_name: str) -> list[tuple]:
        result = get_parser(parser_name)().parse(VALIDATION / "fixtures" / fixture)
        return [
            (e.ts_utc, round(e.lat, 6), round(e.lon, 6), round(e.alt_msl, 1))
            for e in result.events
            if e.event_type == "gps.position"
        ]

    tlog = positions("synthetic_flight.tlog", "mavlink-tlog")
    binlog = positions("synthetic_flight.bin", "ardupilot-bin")
    ulg = positions("synthetic_flight.ulg", "px4-ulog")

    # tlog appends the pending final fix after later messages; compare as sets.
    assert set(tlog) == set(binlog) == set(ulg)
    assert len(tlog) == 8  # 9 track points, one removed by 1 Hz downsampling

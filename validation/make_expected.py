"""Regenerate the golden expected-output files in validation/expected/.

Run only when a parser's behavior changes *intentionally*; review the diff of
the goldens like any other code change:

    python validation/make_expected.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from droneforen.parsers import get_parser  # noqa: E402

CASES = {
    "synthetic_flight.tlog": "mavlink-tlog",
    "synthetic_flight.bin": "ardupilot-bin",
    "synthetic_flight.ulg": "px4-ulog",
    "dji_bracket.srt": "dji-srt",
    "dji_legacy.srt": "dji-srt",
    "geotagged.jpg": "image-exif",
    "dji_flightrecord.txt": "dji-txt",
    "remoteid_scan.json": "remoteid-json",
}


def main() -> None:
    root = Path(__file__).parent
    expected_dir = root / "expected"
    expected_dir.mkdir(exist_ok=True)
    for fixture, parser_name in CASES.items():
        cls = get_parser(parser_name)
        path = root / "fixtures" / fixture
        assert cls.detect(path), f"{parser_name} does not detect {fixture}"
        result = cls().parse(path)
        events = [e.model_dump(mode="json", exclude_none=True) for e in result.events]
        out = expected_dir / f"{fixture}.expected.json"
        out.write_text(
            json.dumps({"parser": parser_name, "parser_version": cls.manifest.version,
                        "events": events}, indent=1, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"{fixture}: {len(events)} events -> {out.name}")


if __name__ == "__main__":
    main()

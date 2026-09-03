"""Subprocess entry point for parser execution.

Invoked by the runner as ``python -m droneforen.parsers.sandbox <parser> <path>``.
Prints a single JSON document to stdout:

    {"ok": true, "parser": ..., "version": ..., "events": [...], "child_artifacts": [...]}
    {"ok": false, "error": "..."}

This provides process separation and a timeout, not an operating-system sandbox.
The parser receives no case database or audit-log handle. Network and filesystem
restrictions are not enforced; see ARCHITECTURE.md section 7.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path


def run(parser_name: str, data_path: str) -> dict:
    from . import get_parser

    cls = get_parser(parser_name)
    result = cls().parse(Path(data_path))
    return {
        "ok": True,
        "parser": cls.manifest.name,
        "version": cls.manifest.version,
        "events": [e.model_dump(mode="json") for e in result.events],
        "child_artifacts": [asdict(c) for c in result.child_artifacts],
    }


def main(argv: list[str]) -> int:
    # The runner decodes our stdout as UTF-8; never depend on the platform
    # console encoding (cp1252 on Windows chokes on non-ASCII evidence content).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: sandbox <parser> <path>"}))
        return 2
    try:
        out = run(argv[0], argv[1])
    except Exception:
        print(json.dumps({"ok": False, "error": traceback.format_exc(limit=20)}))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

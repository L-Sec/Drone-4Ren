"""Parser execution with provenance recording.

The runner is the only component allowed to turn parser output into database
rows. It records the ParserRun (name, version, settings, input hash, timing,
outcome) before and after execution, runs the parser in a subprocess via
``droneforen.parsers.sandbox``, validates the returned events against the
Event schema, and inserts them with full provenance.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

from ..audit import utc_now_iso
from ..case import CaseError, CaseWorkspace
from ..db import new_id
from ..models import Event
from . import get_parser

DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class ParserRunOutcome:
    run_id: str
    parser_name: str
    parser_version: str
    status: str  # completed | failed
    event_count: int = 0
    error: str | None = None


def run_parser(
    case: CaseWorkspace,
    artifact_id: str,
    parser_name: str,
    *,
    actor: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> ParserRunOutcome:
    parser_cls = get_parser(parser_name)
    manifest = parser_cls.manifest

    with case.connect_db() as conn:
        row = conn.execute(
            "SELECT a.id, a.sha256, a.path_within, e.sha256 AS evidence_sha256 "
            "FROM artifacts a JOIN evidence_items e ON a.evidence_id = e.id "
            "WHERE a.id = ?",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise CaseError(f"no artifact with id {artifact_id}")

    # The current runner parses whole stored evidence files.
    data_path = case.store.data_path(row["evidence_sha256"])
    input_sha256 = row["sha256"] or row["evidence_sha256"]

    run_id = new_id()
    started = utc_now_iso()
    with case.connect_db() as conn:
        conn.execute(
            """INSERT INTO parser_runs
               (id, artifact_id, parser_name, parser_version, settings_json,
                input_sha256, started_utc, status)
               VALUES (?, ?, ?, ?, '{}', ?, ?, 'running')""",
            (run_id, artifact_id, manifest.name, manifest.version, input_sha256, started),
        )
        conn.commit()

    status, error, events = "failed", None, []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "droneforen.parsers.sandbox", parser_name, str(data_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {"ok": False,
                                                                       "error": "no output"}
        if payload.get("ok"):
            events = [Event.model_validate(e) for e in payload["events"]]
            status = "completed"
        else:
            error = payload.get("error") or f"parser exited with code {proc.returncode}"
    except subprocess.TimeoutExpired:
        error = f"parser timed out after {timeout}s"
    except (json.JSONDecodeError, KeyError) as exc:
        error = f"parser produced invalid output: {exc}"

    ended = utc_now_iso()
    with case.connect_db() as conn:
        if status == "completed":
            conn.executemany(
                """INSERT INTO events
                   (id, artifact_id, parser_run_id, source_offset, ts_utc, ts_original,
                    ts_source, clock_confidence, event_type, lat, lon, alt_msl, alt_agl,
                    h_acc, v_acc, pitch, roll, yaw, speed, heading, confidence, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        new_id(), artifact_id, run_id, e.source_offset, e.ts_utc,
                        e.ts_original, e.ts_source, e.clock_confidence, e.event_type,
                        e.lat, e.lon, e.alt_msl, e.alt_agl, e.h_acc, e.v_acc,
                        e.pitch, e.roll, e.yaw, e.speed, e.heading, e.confidence,
                        json.dumps(e.payload, sort_keys=True, ensure_ascii=False),
                    )
                    for e in events
                ],
            )
        conn.execute(
            "UPDATE parser_runs SET ended_utc = ?, status = ?, event_count = ?, error = ? "
            "WHERE id = ?",
            (ended, status, len(events) if status == "completed" else None, error, run_id),
        )
        conn.commit()

    case.audit.append(
        actor,
        "evidence.parse",
        target=input_sha256,
        params={
            "parser": manifest.name,
            "parser_version": manifest.version,
            "run_id": run_id,
            "status": status,
            "event_count": len(events) if status == "completed" else 0,
            "error": error,
        },
    )
    return ParserRunOutcome(
        run_id=run_id,
        parser_name=manifest.name,
        parser_version=manifest.version,
        status=status,
        event_count=len(events) if status == "completed" else 0,
        error=error,
    )

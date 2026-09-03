"""Unified cross-source timeline.

A timeline entry is an Event joined with its full provenance chain: artifact,
parser run (name + version), and evidence item (hash + original name). Events
with UTC timestamps sort chronologically; events whose clock was never
anchored to UTC (camera-local SRT, boot-relative ULog) sort after them and are
reported separately so a user does not mistake them for anchored times.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..case import CaseWorkspace


@dataclass
class TimelineEntry:
    event_id: str
    event_type: str
    ts_utc: str | None
    ts_original: str | None
    ts_source: str | None
    clock_confidence: str | None
    lat: float | None
    lon: float | None
    alt_msl: float | None
    alt_agl: float | None
    speed: float | None
    heading: float | None
    confidence: float
    payload: dict
    source_offset: str | None
    evidence_sha256: str
    evidence_name: str
    artifact_id: str
    parser_run_id: str
    parser_name: str
    parser_version: str


@dataclass
class TimelineSummary:
    total_events: int
    anchored_events: int
    unanchored_events: int
    sources: list[dict] = field(default_factory=list)
    clock_sources: list[dict] = field(default_factory=list)


_BASE_QUERY = """
SELECT ev.id AS event_id, ev.event_type, ev.ts_utc, ev.ts_original, ev.ts_source,
       ev.clock_confidence, ev.lat, ev.lon, ev.alt_msl, ev.alt_agl, ev.speed,
       ev.heading, ev.confidence, ev.payload_json, ev.source_offset,
       ev.artifact_id, ev.parser_run_id,
       ei.sha256 AS evidence_sha256, ei.original_name AS evidence_name,
       pr.parser_name, pr.parser_version
FROM events ev
JOIN artifacts a       ON ev.artifact_id = a.id
JOIN evidence_items ei ON a.evidence_id = ei.id
JOIN parser_runs pr    ON ev.parser_run_id = pr.id
"""


def load_timeline(
    case: CaseWorkspace,
    *,
    event_type: str | None = None,
    evidence_sha_prefix: str | None = None,
    min_confidence: float | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[TimelineEntry]:
    clauses, params = [], []
    if event_type:
        clauses.append("ev.event_type = ?")
        params.append(event_type)
    if evidence_sha_prefix:
        clauses.append("ei.sha256 LIKE ?")
        params.append(evidence_sha_prefix + "%")
    if min_confidence is not None:
        clauses.append("ev.confidence >= ?")
        params.append(min_confidence)

    query = _BASE_QUERY
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    # UTC-anchored events first in time order; unanchored events after, grouped
    # by evidence so per-source original ordering is preserved.
    query += (
        " ORDER BY (ev.ts_utc IS NULL), ev.ts_utc, ei.sha256, "
        "LENGTH(ev.source_offset), ev.source_offset"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
        if offset:
            query += f" OFFSET {int(offset)}"

    with case.connect_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        TimelineEntry(
            event_id=row["event_id"],
            event_type=row["event_type"],
            ts_utc=row["ts_utc"],
            ts_original=row["ts_original"],
            ts_source=row["ts_source"],
            clock_confidence=row["clock_confidence"],
            lat=row["lat"],
            lon=row["lon"],
            alt_msl=row["alt_msl"],
            alt_agl=row["alt_agl"],
            speed=row["speed"],
            heading=row["heading"],
            confidence=row["confidence"],
            payload=json.loads(row["payload_json"]),
            source_offset=row["source_offset"],
            evidence_sha256=row["evidence_sha256"],
            evidence_name=row["evidence_name"],
            artifact_id=row["artifact_id"],
            parser_run_id=row["parser_run_id"],
            parser_name=row["parser_name"],
            parser_version=row["parser_version"],
        )
        for row in rows
    ]


def summarize(case: CaseWorkspace) -> TimelineSummary:
    with case.connect_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        anchored = conn.execute(
            "SELECT COUNT(*) FROM events WHERE ts_utc IS NOT NULL"
        ).fetchone()[0]
        sources = [
            dict(row)
            for row in conn.execute(
                "SELECT ei.sha256, ei.original_name, COUNT(ev.id) AS event_count, "
                "MIN(ev.ts_utc) AS first_utc, MAX(ev.ts_utc) AS last_utc "
                "FROM events ev "
                "JOIN artifacts a ON ev.artifact_id = a.id "
                "JOIN evidence_items ei ON a.evidence_id = ei.id "
                "GROUP BY ei.sha256, ei.original_name ORDER BY ei.original_name"
            )
        ]
        clock_sources = [
            dict(row)
            for row in conn.execute(
                "SELECT ts_source, clock_confidence, COUNT(*) AS event_count, "
                "SUM(ts_utc IS NULL) AS unanchored "
                "FROM events GROUP BY ts_source, clock_confidence ORDER BY event_count DESC"
            )
        ]
    return TimelineSummary(
        total_events=total,
        anchored_events=anchored,
        unanchored_events=total - anchored,
        sources=sources,
        clock_sources=clock_sources,
    )

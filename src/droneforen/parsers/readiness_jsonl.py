"""Parser for promoted readiness segments (hash-chained observation JSONL).

Readiness stores (droneforen readiness ...) log observations as hash-chained
JSONL segments. When a segment is promoted into a case, this parser:

1. verifies the segment's internal hash chain and reports the result as part
   of the parse output (a broken chain is itself evidence-relevant), and
2. maps each observation through the same normalization as the remoteid-json
   parser, so readiness-collected and ad-hoc-imported observations are
   indistinguishable downstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from ..audit import AuditLog
from ..models import DataOrigin, Event, FormatSupport, ParserManifest
from .base import Parser, ParseResult
from .remoteid_json import observation_to_event

_REQUIRED_KEYS = {"seq", "ts_utc", "actor", "action", "params", "prev_sha256"}


class ReadinessJsonlParser(Parser):
    manifest: ClassVar[ParserManifest] = ParserManifest(
        name="readiness-jsonl",
        version="0.1.0",
        formats=[
            FormatSupport(
                vendor="droneforen",
                artifact_type="readiness-segment",
                extensions=[".jsonl"],
                description=("Hash-chained observation segments written by "
                             "droneforen readiness mode."),
            )
        ],
        data_origin=DataOrigin.ORIGINAL_RAW,
        confidence_notes=(
            "Chain verification is performed at parse time and reported in the "
            "parser summary; observation content carries the same receiver-clock "
            "and spoofability caveats as remoteid-json."
        ),
        known_limitations=[
            "Only rid.observation records are interpreted; other record types "
            "are counted and skipped.",
            "Chain verification proves segment integrity since writing, not the "
            "truth of the underlying broadcasts.",
        ],
        specificity=74,
    )

    @classmethod
    def detect(cls, path: Path) -> bool:
        try:
            first = path.open("rb").readline(8192)
        except OSError:
            return False
        if not first.startswith(b"{"):
            return False
        try:
            record = json.loads(first.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(record, dict) and _REQUIRED_KEYS <= set(record)

    def parse(self, path: Path) -> ParseResult:
        log = AuditLog(path)
        chain = log.verify()
        events: list[Event] = []
        skipped = 0
        uas_ids: set[str] = set()

        for record in log.records():
            if record.get("action") != "rid.observation":
                skipped += 1
                continue
            obs = record.get("params") or {}
            event = observation_to_event(obs, f"seq:{record['seq']}")
            if event is None:
                skipped += 1
                continue
            event.payload["ingested_utc"] = record.get("ts_utc")
            event.payload["sensor"] = record.get("actor")
            if event.payload.get("uas_id"):
                uas_ids.add(event.payload["uas_id"])
            events.append(event)

        if not chain.ok:
            events.append(Event(
                event_type="integrity.chain_broken",
                source_offset="chain",
                confidence=1.0,
                payload={"problems": chain.problems[:20],
                         "note": ("segment hash chain is broken: records may have "
                                  "been altered, removed, or reordered after "
                                  "collection")},
            ))
        events.append(Event(
            event_type="parser.summary",
            payload={
                "chain_valid": chain.ok,
                "chain_records": chain.record_count,
                "chain_head_sha256": chain.head_sha256,
                "observations": len([e for e in events
                                     if e.event_type == "rid.observation"]),
                "skipped_records": skipped,
                "distinct_uas_ids": sorted(uas_ids),
            },
        ))
        return ParseResult(events=events)

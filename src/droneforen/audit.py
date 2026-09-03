"""Hash-chained, append-only audit log.

Every case action (create, intake, parse, view, export, verify, ...) appends
one JSON record to ``audit/audit.jsonl``. Each record embeds the SHA-256 of
the previous record's serialized line, so modifying, reordering, or deleting
any interior record breaks the chain and is detected by :meth:`AuditLog.verify`.

Known limitation: truncating records from the *end*
of the log cannot be detected from the file alone, because the chain has no
external anchor. ``case verify`` therefore reports the current chain-head hash
and record count so users can record them out-of-band (user notes or a
management system) and compare later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .hashing import hash_bytes

GENESIS_HASH = "0" * 64


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass
class AuditVerifyResult:
    ok: bool
    record_count: int
    head_sha256: str
    problems: list[str] = field(default_factory=list)


class AuditLog:
    def __init__(self, path: Path):
        self.path = path

    def _tail_state(self) -> tuple[int, str]:
        """Return (last_seq, last_line_hash) by scanning the log."""
        if not self.path.exists():
            return 0, GENESIS_HASH
        last_seq, last_hash = 0, GENESIS_HASH
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    last_seq = int(json.loads(line)["seq"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    # A corrupt line must not make the log unappendable: the
                    # verify action that *reports* the corruption is itself an
                    # audited action. verify() flags the bad line regardless.
                    pass
                last_hash = hash_bytes(line.encode("utf-8"))
        return last_seq, last_hash

    def append(
        self, actor: str, action: str, target: str = "", params: dict | None = None
    ) -> dict:
        last_seq, last_hash = self._tail_state()
        record = {
            "seq": last_seq + 1,
            "ts_utc": utc_now_iso(),
            "actor": actor,
            "action": action,
            "target": target,
            "params": params or {},
            "prev_sha256": last_hash,
        }
        line = _canonical(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as fh:
            fh.write(line + b"\n")
        return record

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    out.append(json.loads(line))
        return out

    def verify(self) -> AuditVerifyResult:
        if not self.path.exists():
            return AuditVerifyResult(
                ok=False, record_count=0, head_sha256=GENESIS_HASH, problems=["audit log missing"]
            )
        problems: list[str] = []
        prev_hash = GENESIS_HASH
        prev_seq = 0
        count = 0
        with open(self.path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    problems.append(f"line {lineno}: not valid JSON")
                    prev_hash = hash_bytes(raw.encode("utf-8"))
                    continue
                # The chain hash covers the exact bytes of the line, so a record
                # whose serialization differs from canonical form is itself a problem.
                if _canonical(record) != raw.encode("utf-8"):
                    problems.append(f"line {lineno}: non-canonical serialization")
                if record.get("prev_sha256") != prev_hash:
                    problems.append(
                        f"line {lineno} (seq {record.get('seq')}): chain broken - "
                        f"prev_sha256 does not match hash of preceding record"
                    )
                if record.get("seq") != prev_seq + 1:
                    problems.append(
                        f"line {lineno}: sequence gap (expected {prev_seq + 1}, "
                        f"got {record.get('seq')})"
                    )
                prev_seq = record.get("seq", prev_seq + 1)
                prev_hash = hash_bytes(raw.encode("utf-8"))
                count += 1
        return AuditVerifyResult(
            ok=not problems, record_count=count, head_sha256=prev_hash, problems=problems
        )

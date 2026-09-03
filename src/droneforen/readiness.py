"""Segmented observation logging for sensor data.

Records are written to hourly hash-chained files. Closed segments receive a SHA-256
sidecar. The ``promote`` command copies a segment into a case workspace for parsing.
This is an experimental path outside the basic local workflow.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field

from .audit import AuditLog, utc_now_iso
from .case import AddedEvidence, CaseWorkspace
from .hashing import hash_file

READINESS_FILE = "readiness.json"
SEGMENT_PREFIX = "obs-"


class ReadinessError(Exception):
    pass


class ReadinessMeta(BaseModel):
    store_id: str
    site: str
    context: str = Field(
        default="local",
        validation_alias=AliasChoices("context", "legal_authority"),
    )
    collection_purpose: str
    retention_days: int
    notes: str | None = Field(
        default=None,
        validation_alias=AliasChoices("notes", "minimization_notes"),
    )
    created_utc: str
    created_by: str
    format_version: int = 1


@dataclass
class ReadinessVerifyResult:
    ok: bool
    segments_checked: int
    records_total: int
    problems: list[str] = field(default_factory=list)


class ReadinessStore:
    def __init__(self, root: Path, meta: ReadinessMeta):
        self.root = Path(root)
        self.meta = meta
        self.audit = AuditLog(self.root / "audit" / "audit.jsonl")
        self.segments_dir = self.root / "segments"

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def create(cls, root: Path, *, site: str, collection_purpose: str,
               retention_days: int, actor: str,
               context: str = "local",
               notes: str | None = None) -> ReadinessStore:
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise ReadinessError(f"{root} exists and is not empty")
        (root / "segments").mkdir(parents=True, exist_ok=True)
        (root / "audit").mkdir(parents=True, exist_ok=True)
        import uuid

        meta = ReadinessMeta(
            store_id=uuid.uuid4().hex, site=site, context=context,
            collection_purpose=collection_purpose, retention_days=retention_days,
            notes=notes, created_utc=utc_now_iso(),
            created_by=actor,
        )
        (root / READINESS_FILE).write_text(
            json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8")
        store = cls(root, meta)
        store.audit.append(actor, "readiness.create", target=site,
                           params={"context": context,
                                   "collection_purpose": collection_purpose,
                                   "retention_days": retention_days})
        return store

    @classmethod
    def open(cls, root: Path) -> ReadinessStore:
        root = Path(root)
        meta_file = root / READINESS_FILE
        if not meta_file.exists():
            raise ReadinessError(f"{root} is not a readiness store (no {READINESS_FILE})")
        return cls(root, ReadinessMeta.model_validate_json(
            meta_file.read_text(encoding="utf-8")))

    # -- segments ------------------------------------------------------------

    def _segment_name(self, when: datetime) -> str:
        return f"{SEGMENT_PREFIX}{when:%Y%m%dT%H}.jsonl"

    def active_segment_path(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(UTC)
        return self.segments_dir / self._segment_name(when)

    def _finalize_stale_segments(self, actor: str, now: datetime) -> None:
        active = self.active_segment_path(now).name
        for seg in sorted(self.segments_dir.glob(f"{SEGMENT_PREFIX}*.jsonl")):
            if seg.name == active or seg.with_suffix(seg.suffix + ".sha256").exists():
                continue
            digest = hash_file(seg).sha256
            seg.with_suffix(seg.suffix + ".sha256").write_text(
                f"{digest}  {seg.name}\n", encoding="utf-8")
            self.audit.append(actor, "readiness.segment.close", target=seg.name,
                              params={"sha256": digest})

    def ingest(self, observations: list[dict], *, actor: str, sensor: str,
               now: datetime | None = None) -> int:
        """Append observations to the current hourly segment (hash-on-write)."""
        now = now or datetime.now(UTC)
        self._finalize_stale_segments(actor, now)
        segment = AuditLog(self.active_segment_path(now))
        count = 0
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            segment.append(sensor, "rid.observation",
                           target=str(obs.get("uas_id") or obs.get("basic_id") or ""),
                           params=obs)
            count += 1
        self.audit.append(actor, "readiness.ingest",
                          target=self.active_segment_path(now).name,
                          params={"observations": count, "sensor": sensor})
        return count

    def segments(self) -> list[Path]:
        return sorted(self.segments_dir.glob(f"{SEGMENT_PREFIX}*.jsonl"))

    # -- verification ---------------------------------------------------------

    def verify(self) -> ReadinessVerifyResult:
        problems: list[str] = []
        records = 0
        segs = self.segments()
        for seg in segs:
            result = AuditLog(seg).verify()
            records += result.record_count
            problems += [f"{seg.name}: {p}" for p in result.problems]
            sidecar = seg.with_suffix(seg.suffix + ".sha256")
            if sidecar.exists():
                recorded = sidecar.read_text(encoding="utf-8").split()[0]
                actual = hash_file(seg).sha256
                if recorded != actual:
                    problems.append(f"{seg.name}: closed-segment hash mismatch "
                                    f"(recorded {recorded[:16]}..., actual {actual[:16]}...)")
        audit_result = self.audit.verify()
        problems += [f"audit: {p}" for p in audit_result.problems]
        return ReadinessVerifyResult(ok=not problems, segments_checked=len(segs),
                                     records_total=records, problems=problems)

    # -- retention -------------------------------------------------------------

    def prune(self, *, actor: str, archive_dir: Path | None = None,
              now: datetime | None = None, delete: bool = False) -> list[str]:
        """Enforce retention: segments older than retention_days are archived
        (default) or deleted (explicit). Either way the action and the segment
        hash are recorded before the file leaves the store."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.meta.retention_days)
        self._finalize_stale_segments(actor, now)
        affected = []
        for seg in self.segments():
            stamp = seg.name.removeprefix(SEGMENT_PREFIX).removesuffix(".jsonl")
            try:
                seg_time = datetime.strptime(stamp, "%Y%m%dT%H").replace(tzinfo=UTC)
            except ValueError:
                continue
            if seg_time >= cutoff:
                continue
            digest = hash_file(seg).sha256
            sidecar = seg.with_suffix(seg.suffix + ".sha256")
            if archive_dir is not None:
                archive_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(seg), archive_dir / seg.name)
                if sidecar.exists():
                    shutil.move(str(sidecar), archive_dir / sidecar.name)
                action, detail = "readiness.segment.archive", str(archive_dir)
            elif delete:
                seg.unlink()
                sidecar.unlink(missing_ok=True)
                action, detail = "readiness.segment.delete", "retention expiry"
            else:
                raise ReadinessError(
                    "prune requires an archive_dir, or delete=True to discard")
            self.audit.append(actor, action, target=seg.name,
                              params={"sha256": digest, "detail": detail,
                                      "retention_days": self.meta.retention_days})
            affected.append(seg.name)
        return affected

    # -- promotion --------------------------------------------------------------

    def promote(self, segment_name: str, case: CaseWorkspace, *,
                actor: str) -> AddedEvidence:
        """Intake a segment into a case as evidence through the normal pipeline."""
        seg = self.segments_dir / segment_name
        if not seg.exists():
            raise ReadinessError(f"no segment named {segment_name}")
        self._finalize_stale_segments(actor, datetime.now(UTC))
        added = case.add_evidence(
            seg, actor=actor,
            description=(f"readiness segment from store '{self.meta.site}' "
                         f"(context: {self.meta.context}; "
                         f"purpose: {self.meta.collection_purpose})"),
            acquisition_method="readiness-promote",
        )
        self.audit.append(actor, "readiness.promote", target=segment_name,
                          params={"case_number": case.meta.case_number,
                                  "sha256": added.manifest.sha256})
        return added

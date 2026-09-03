"""Case workspace: the one-directory-per-case format (ARCHITECTURE.md §3).

Ties together case metadata (``case.json``), the evidence store, the case
database, and the audit log, and enforces the ordering rules between them:
originals land in the store first, derived rows second, and the audit log
records every action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditLog, AuditVerifyResult, utc_now_iso
from .db import connect, init_db, new_id
from .evidence import EvidenceStore, StoreVerifyResult
from .hashing import hash_file
from .models import CaseMeta, CaseScope, DataOrigin, EvidenceManifest, WriteBlockerInfo

CASE_FILE = "case.json"
SUBDIRS = ("evidence", "db", "audit", "notes", "reports")


class CaseError(Exception):
    pass


@dataclass
class CaseVerifyResult:
    ok: bool
    store: StoreVerifyResult
    audit: AuditVerifyResult
    problems: list[str] = field(default_factory=list)


@dataclass
class AddedEvidence:
    evidence_id: str
    artifact_id: str
    manifest: EvidenceManifest


class CaseWorkspace:
    def __init__(self, root: Path, meta: CaseMeta):
        self.root = root
        self.meta = meta
        self.store = EvidenceStore(root / "evidence")
        self.audit = AuditLog(root / "audit" / "audit.jsonl")
        self.db_path = root / "db" / "case.sqlite"

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        case_number: str,
        actor: str,
        collector: str = "operator",
        context: str = "local",
        description: str | None = None,
        scope: CaseScope | None = None,
    ) -> CaseWorkspace:
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise CaseError(f"{root} exists and is not empty")
        for sub in SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)

        meta = CaseMeta(
            case_id=new_id(),
            case_number=case_number,
            created_utc=utc_now_iso(),
            collector=collector,
            context=context,
            description=description,
            scope=scope or CaseScope(),
        )
        (root / CASE_FILE).write_text(
            json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8"
        )
        init_db(root / "db" / "case.sqlite")

        case = cls(root, meta)
        with case.connect_db() as conn:
            conn.execute(
                "INSERT INTO cases (id, case_number, created_utc, meta_json) VALUES (?, ?, ?, ?)",
                (meta.case_id, meta.case_number, meta.created_utc, meta.model_dump_json()),
            )
            conn.commit()
        case.audit.append(
            actor,
            "case.create",
            target=meta.case_number,
            params={"context": context, "collector": collector},
        )
        return case

    @classmethod
    def open(cls, root: Path) -> CaseWorkspace:
        root = Path(root)
        case_file = root / CASE_FILE
        if not case_file.exists():
            raise CaseError(f"{root} is not a case directory (no {CASE_FILE})")
        meta = CaseMeta.model_validate_json(case_file.read_text(encoding="utf-8"))
        return cls(root, meta)

    def connect_db(self):
        return connect(self.db_path)

    # -- evidence intake -----------------------------------------------------

    def add_evidence(
        self,
        source: Path,
        *,
        actor: str,
        description: str | None = None,
        acquisition_method: str = "logical",
        data_origin: DataOrigin = DataOrigin.ORIGINAL_RAW,
        source_device: str | None = None,
        write_blocker: WriteBlockerInfo | None = None,
    ) -> AddedEvidence:
        source = Path(source)
        if not source.is_file():
            raise CaseError(f"{source} is not a file")

        hashes = hash_file(source)
        manifest = EvidenceManifest(
            sha256=hashes.sha256,
            md5=hashes.md5,
            size_bytes=hashes.size_bytes,
            original_name=source.name,
            added_utc=utc_now_iso(),
            actor=actor,
            case_number=self.meta.case_number,
            description=description,
            acquisition_method=acquisition_method,
            data_origin=data_origin,
            source_device=source_device,
            write_blocker=write_blocker,
        )
        self.store.add(source, manifest)

        evidence_id = new_id()
        artifact_id = new_id()
        with self.connect_db() as conn:
            conn.execute(
                """INSERT INTO evidence_items
                   (id, sha256, md5, size_bytes, original_name, added_utc, actor,
                    description, acquisition_method, data_origin, source_device, manifest_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    manifest.sha256,
                    manifest.md5,
                    manifest.size_bytes,
                    manifest.original_name,
                    manifest.added_utc,
                    actor,
                    description,
                    acquisition_method,
                    data_origin.value,
                    source_device,
                    manifest.model_dump_json(),
                ),
            )
            conn.execute(
                """INSERT INTO custody_events (id, evidence_id, ts_utc, actor, action, details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (new_id(), evidence_id, manifest.added_utc, actor, "intake",
                 f"acquired via {acquisition_method}; source file {source}"),
            )
            conn.execute(
                """INSERT INTO artifacts
                   (id, evidence_id, parent_artifact_id, path_within, artifact_type,
                    sha256, detected_by)
                   VALUES (?, ?, NULL, ?, 'file', ?, 'intake')""",
                (artifact_id, evidence_id, manifest.original_name, manifest.sha256),
            )
            conn.commit()

        self.audit.append(
            actor,
            "evidence.add",
            target=manifest.sha256,
            params={
                "original_name": manifest.original_name,
                "size_bytes": manifest.size_bytes,
                "acquisition_method": acquisition_method,
                "data_origin": data_origin.value,
            },
        )
        return AddedEvidence(evidence_id=evidence_id, artifact_id=artifact_id, manifest=manifest)

    # -- verification --------------------------------------------------------

    def verify(self, actor: str) -> CaseVerifyResult:
        store_result = self.store.verify()
        audit_result = self.audit.verify()
        problems = list(store_result.problems) + list(audit_result.problems)

        # Cross-check: every evidence row in the database must exist in the store.
        with self.connect_db() as conn:
            for row in conn.execute("SELECT sha256 FROM evidence_items"):
                if not self.store.data_path(row["sha256"]).exists():
                    problems.append(f"{row['sha256']}: in database but missing from store")

        result = CaseVerifyResult(
            ok=not problems, store=store_result, audit=audit_result, problems=problems
        )
        self.audit.append(
            actor,
            "case.verify",
            target=self.meta.case_number,
            params={
                "ok": result.ok,
                "evidence_items": store_result.item_count,
                "audit_records": audit_result.record_count,
                "audit_head_sha256": audit_result.head_sha256,
            },
        )
        return result

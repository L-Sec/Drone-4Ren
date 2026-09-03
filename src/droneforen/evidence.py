"""Content-addressed, read-only evidence store.

Layout: ``evidence/sha256/<first two hex chars>/<full hash>/data`` with a
``manifest.json`` sidecar. Nothing under the store is ever written twice;
after intake both files are marked read-only. ``verify()`` rehashes every
stored original against its manifest.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import hash_file
from .models import EvidenceManifest


class EvidenceStoreError(Exception):
    pass


class DuplicateEvidenceError(EvidenceStoreError):
    pass


class AmbiguousHashPrefixError(EvidenceStoreError):
    pass


@dataclass
class StoreVerifyResult:
    ok: bool
    item_count: int
    problems: list[str] = field(default_factory=list)


def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root

    def item_dir(self, sha256: str) -> Path:
        return self.root / "sha256" / sha256[:2] / sha256

    def data_path(self, sha256: str) -> Path:
        return self.item_dir(sha256) / "data"

    def manifest_path(self, sha256: str) -> Path:
        return self.item_dir(sha256) / "manifest.json"

    def add(self, source: Path, manifest: EvidenceManifest) -> Path:
        """Copy ``source`` into the store and freeze it. Returns the stored data path.

        The caller must have already hashed the source and populated the
        manifest; this method re-verifies the stored copy against those hashes
        so a corrupted copy is caught at intake, not months later.
        """
        item_dir = self.item_dir(manifest.sha256)
        if item_dir.exists():
            raise DuplicateEvidenceError(
                f"evidence with sha256 {manifest.sha256} already in store"
            )
        item_dir.mkdir(parents=True)
        dest = item_dir / "data"
        shutil.copyfile(source, dest)

        stored = hash_file(dest)
        if stored.sha256 != manifest.sha256 or stored.size_bytes != manifest.size_bytes:
            dest.unlink(missing_ok=True)
            raise EvidenceStoreError(
                "stored copy does not match source hashes - copy failed, intake aborted"
            )

        manifest_file = item_dir / "manifest.json"
        manifest_file.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _make_read_only(dest)
        _make_read_only(manifest_file)
        return dest

    def load_manifest(self, sha256: str) -> EvidenceManifest:
        return EvidenceManifest.model_validate_json(
            self.manifest_path(sha256).read_text(encoding="utf-8")
        )

    def iter_hashes(self) -> list[str]:
        base = self.root / "sha256"
        if not base.exists():
            return []
        return sorted(p.name for shard in base.iterdir() for p in shard.iterdir() if p.is_dir())

    def resolve_prefix(self, prefix: str) -> str:
        prefix = prefix.lower()
        matches = [h for h in self.iter_hashes() if h.startswith(prefix)]
        if not matches:
            raise EvidenceStoreError(f"no evidence item matches hash prefix {prefix!r}")
        if len(matches) > 1:
            raise AmbiguousHashPrefixError(
                f"hash prefix {prefix!r} matches {len(matches)} items - use more characters"
            )
        return matches[0]

    def verify(self) -> StoreVerifyResult:
        problems: list[str] = []
        hashes = self.iter_hashes()
        for sha256 in hashes:
            data = self.data_path(sha256)
            manifest_file = self.manifest_path(sha256)
            if not data.exists():
                problems.append(f"{sha256}: data file missing")
                continue
            if not manifest_file.exists():
                problems.append(f"{sha256}: manifest missing")
                continue
            try:
                manifest = self.load_manifest(sha256)
            except Exception as exc:  # manifest unreadable/invalid is a finding, not a crash
                problems.append(f"{sha256}: manifest unreadable ({exc})")
                continue
            actual = hash_file(data)
            if actual.sha256 != sha256:
                problems.append(
                    f"{sha256}: HASH MISMATCH - stored data now hashes to {actual.sha256}"
                )
            if manifest.sha256 != sha256:
                problems.append(f"{sha256}: manifest sha256 field does not match store path")
            if actual.size_bytes != manifest.size_bytes:
                problems.append(
                    f"{sha256}: size mismatch (manifest {manifest.size_bytes}, "
                    f"actual {actual.size_bytes})"
                )
        return StoreVerifyResult(ok=not problems, item_count=len(hashes), problems=problems)

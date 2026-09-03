"""Evidence-bundle export: the whole case as one verifiable archive.

Produces a ZIP of the complete case directory plus a ``BUNDLE-MANIFEST.json``
listing every file with its SHA-256 so a recipient can verify the copy. The
export action is appended to the audit
log *before* archiving, so the bundle contains the record of its own creation.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .audit import utc_now_iso
from .case import CaseWorkspace
from .hashing import hash_file


@dataclass
class BundleResult:
    path: Path
    sha256: str
    file_count: int


def export_bundle(
    case: CaseWorkspace, *, actor: str, out_path: Path | None = None
) -> BundleResult:
    if out_path is None:
        out_path = case.root.parent / f"{case.root.name}-bundle.zip"
    out_path = Path(out_path)
    if out_path.resolve().is_relative_to(case.root.resolve()):
        raise ValueError("bundle destination must be outside the case directory")

    # Audited first so the bundle carries the record of its own export.
    case.audit.append(actor, "export.bundle", target=out_path.name,
                      params={"destination": str(out_path)})

    files = sorted(p for p in case.root.rglob("*") if p.is_file())
    entries = []
    for f in files:
        hashes = hash_file(f)
        entries.append({
            "path": f.relative_to(case.root).as_posix(),
            "sha256": hashes.sha256,
            "size_bytes": hashes.size_bytes,
        })
    manifest = {
        "bundle_format": 1,
        "case_number": case.meta.case_number,
        "case_id": case.meta.case_id,
        "created_utc": utc_now_iso(),
        "created_by": actor,
        "tool": f"droneforen {__version__}",
        "file_count": len(entries),
        "files": entries,
        "verification": ("unzip, hash every file, compare with this manifest; then run "
                         "'droneforen case verify' on the extracted case directory"),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "BUNDLE-MANIFEST.json",
            json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        )
        for f in files:
            zf.write(f, arcname=(Path(case.root.name) / f.relative_to(case.root)).as_posix())

    return BundleResult(
        path=out_path, sha256=hash_file(out_path).sha256, file_count=len(entries)
    )

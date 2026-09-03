import os
import stat

import pytest

from droneforen.evidence import DuplicateEvidenceError
from droneforen.hashing import hash_file


def test_intake_stores_hashes_and_freezes(case, sample_log):
    added = case.add_evidence(sample_log, actor="pytest", description="test log")
    manifest = added.manifest

    expected = hash_file(sample_log)
    assert manifest.sha256 == expected.sha256
    assert manifest.md5 == expected.md5
    assert manifest.size_bytes == expected.size_bytes

    stored = case.store.data_path(manifest.sha256)
    assert stored.exists()
    assert stored.read_bytes() == sample_log.read_bytes()
    # Read-only flag set on the stored original.
    assert not os.access(stored, os.W_OK) or not (os.stat(stored).st_mode & stat.S_IWRITE)


def test_duplicate_intake_is_refused(case, sample_log):
    case.add_evidence(sample_log, actor="pytest")
    with pytest.raises(DuplicateEvidenceError):
        case.add_evidence(sample_log, actor="pytest")


def test_verify_detects_corrupted_original(case, sample_log):
    added = case.add_evidence(sample_log, actor="pytest")
    assert case.verify(actor="pytest").ok

    stored = case.store.data_path(added.manifest.sha256)
    os.chmod(stored, stat.S_IWRITE)
    with open(stored, "ab") as fh:
        fh.write(b"tampered")

    result = case.verify(actor="pytest")
    assert not result.ok
    assert any("HASH MISMATCH" in p for p in result.problems)


def test_prefix_resolution(case, sample_log):
    added = case.add_evidence(sample_log, actor="pytest")
    sha = added.manifest.sha256
    assert case.store.resolve_prefix(sha[:8]) == sha

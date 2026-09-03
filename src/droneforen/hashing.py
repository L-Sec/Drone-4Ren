"""Streaming file hashing.

SHA-256 is the primary integrity hash. MD5 is computed alongside it (same
single pass over the data) solely for interoperability with legacy evidence
inventories and older tooling - it is never used as a security control.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileHashes:
    sha256: str
    md5: str
    size_bytes: int


def hash_file(path: Path) -> FileHashes:
    """Hash a file in one streaming pass. Raises OSError on unreadable input."""
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            sha256.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return FileHashes(sha256=sha256.hexdigest(), md5=md5.hexdigest(), size_bytes=size)


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

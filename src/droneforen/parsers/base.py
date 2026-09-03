"""Parser plugin contract (ARCHITECTURE.md §5).

A parser receives a path to stored artifact data and returns normalized Events.
The runner owns database and audit-log writes and executes the parser in a child
process.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ..models import Event, ParserManifest


@dataclass
class ChildArtifact:
    """Reserved description of a structure found inside an artifact."""

    path_within: str
    artifact_type: str
    sha256: str | None = None


@dataclass
class ParseResult:
    events: list[Event] = field(default_factory=list)
    child_artifacts: list[ChildArtifact] = field(default_factory=list)


class Parser(ABC):
    manifest: ClassVar[ParserManifest]

    @classmethod
    @abstractmethod
    def detect(cls, path: Path) -> bool:
        """Content-based format detection. Must never rely on extension alone."""

    @abstractmethod
    def parse(self, path: Path) -> ParseResult: ...

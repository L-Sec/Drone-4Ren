"""Parser registry.

Parsers are discovered from two sources, deduplicated by name:

1. Built-ins shipped with droneforen (listed in ``_BUILTIN``).
2. Third-party plugins registered under the ``droneforen.parsers``
   entry-point group.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

from .base import ChildArtifact, Parser, ParseResult

__all__ = ["Parser", "ParseResult", "ChildArtifact", "load_registry", "get_parser",
           "detect_parsers"]

_BUILTIN = {
    "plaintext": "droneforen.parsers.plaintext:PlainTextParser",
    "dji-srt": "droneforen.parsers.dji_srt:DjiSrtParser",
    "image-exif": "droneforen.parsers.image_exif:ImageExifParser",
    "mavlink-tlog": "droneforen.parsers.mavlink_tlog:MavlinkTlogParser",
    "ardupilot-bin": "droneforen.parsers.ardupilot_bin:ArduPilotBinParser",
    "px4-ulog": "droneforen.parsers.px4_ulog:Px4UlogParser",
    "dji-txt": "droneforen.parsers.dji_txt:DjiTxtParser",
    "remoteid-json": "droneforen.parsers.remoteid_json:RemoteIdJsonParser",
    "readiness-jsonl": "droneforen.parsers.readiness_jsonl:ReadinessJsonlParser",
}


def _load_ref(ref: str) -> type[Parser]:
    module_name, _, attr = ref.partition(":")
    return getattr(import_module(module_name), attr)


def load_registry() -> dict[str, type[Parser]]:
    registry: dict[str, type[Parser]] = {}
    for name, ref in _BUILTIN.items():
        registry[name] = _load_ref(ref)
    for ep in entry_points(group="droneforen.parsers"):
        if ep.name not in registry:
            registry[ep.name] = ep.load()
    return registry


def get_parser(name: str) -> type[Parser]:
    registry = load_registry()
    if name not in registry:
        available = ", ".join(sorted(registry)) or "(none)"
        raise KeyError(f"no parser named {name!r}; available: {available}")
    return registry[name]


def detect_parsers(path: Path) -> list[str]:
    """Names of parsers whose content-based detection accepts ``path``,
    ordered most-specific first (manifest ``specificity``, then name)."""
    matches = []
    for name, cls in load_registry().items():
        try:
            if cls.detect(path):
                matches.append((name, cls.manifest.specificity))
        except Exception:
            # A parser whose detection crashes must not block other parsers.
            continue
    matches.sort(key=lambda m: (-m[1], m[0]))
    return [name for name, _ in matches]

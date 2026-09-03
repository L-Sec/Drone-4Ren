"""Build the compatibility matrix from parser manifests and validation coverage."""

from __future__ import annotations

import json
from pathlib import Path

from .parsers import load_registry


def _validated_fixtures(validation_dir: Path | None) -> dict[str, list[str]]:
    coverage: dict[str, list[str]] = {}
    if validation_dir is None:
        return coverage
    expected = validation_dir / "expected"
    if not expected.is_dir():
        return coverage
    for golden in sorted(expected.glob("*.expected.json")):
        try:
            doc = json.loads(golden.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        parser = doc.get("parser")
        if parser:
            fixture = golden.name.removesuffix(".expected.json")
            coverage.setdefault(parser, []).append(fixture)
    return coverage


def build_matrix(validation_dir: Path | None = None) -> list[dict]:
    coverage = _validated_fixtures(validation_dir)
    rows = []
    for name, cls in sorted(load_registry().items()):
        m = cls.manifest
        rows.append({
            "parser": name,
            "version": m.version,
            "support_level": m.support_level,
            "vendors": sorted({f.vendor for f in m.formats}),
            "artifact_types": sorted({f.artifact_type for f in m.formats}),
            "extensions": sorted({ext for f in m.formats for ext in f.extensions}),
            "data_origin": m.data_origin.value,
            "detection_specificity": m.specificity,
            "validated_fixtures": coverage.get(name, []),
            "confidence_notes": m.confidence_notes,
            "known_limitations": list(m.known_limitations),
        })
    return rows


def to_markdown(matrix: list[dict]) -> str:
    lines = [
        "# Drone 4Ren parser compatibility matrix",
        "",
        "Generated from parser manifests and golden-validation coverage. "
        "`recognition` means the format is identified and metadata surfaced but "
        "telemetry is NOT decoded.",
        "",
        "| Parser | Version | Support | Vendor / artifact | Extensions | Data origin "
        "| Golden fixtures |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in matrix:
        vendor = ", ".join(f"{v}" for v in row["vendors"]) or "-"
        artifact = ", ".join(row["artifact_types"]) or "-"
        lines.append(
            f"| {row['parser']} | {row['version']} | {row['support_level']} "
            f"| {vendor} / {artifact} | {', '.join(row['extensions']) or '-'} "
            f"| {row['data_origin']} "
            f"| {', '.join(row['validated_fixtures']) or 'none'} |"
        )
    lines.append("")
    lines.append("## Known limitations")
    lines.append("")
    for row in matrix:
        if row["known_limitations"]:
            lines.append(f"### {row['parser']} {row['version']}")
            for limitation in row["known_limitations"]:
                lines.append(f"- {limitation}")
            lines.append("")
    return "\n".join(lines) + "\n"

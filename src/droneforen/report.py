"""HTML flight-data report generation.

The report distinguishes source-derived content from analyst-derived content,
runs an integrity verification at generation time, embeds a schematic SVG
track map (a local SVG with no tile requests), and closes with a reproducibility
appendix generated from recorded parser runs. The report file's SHA-256 is
written to a sidecar so any copy can be checked against what was generated.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment

from . import __version__
from .analysis.checks import run_checks
from .analysis.correlation import correlate_media
from .analysis.geospatial import (
    MediaPoint,
    Track,
    build_tracks,
    media_points,
    rid_tracks,
)
from .analysis.reconstruction import reconstruct
from .analysis.timeline import load_timeline, summarize
from .audit import utc_now_iso
from .case import CaseWorkspace
from .hashing import hash_file

TIMELINE_ROW_CAP = 500
_SVG_W, _SVG_H, _SVG_PAD = 860, 520, 45
_TRACK_COLORS = ["#1f4e79", "#7a2e2e", "#2e6b2e", "#6a4a8a", "#8a6a1a"]


@dataclass
class GeneratedReport:
    path: Path
    sha256: str
    sidecar_path: Path


@dataclass
class ReportData:
    """Everything a report renderer needs, collected once so HTML and PDF
    renderings cannot drift apart."""

    generated_utc: str
    verify: object
    evidence: list[dict]
    parser_runs: list[dict]
    timeline_rows: list[dict]
    timeline_truncated: bool
    timeline_summary: object
    tracks: list[Track]
    media: list[MediaPoint]
    flights: list
    correlations: list
    findings: list


def collect_report_data(case: CaseWorkspace, actor: str) -> ReportData:
    generated_utc = utc_now_iso()
    verify = case.verify(actor)  # audited; includes rehash + chain walk

    with case.connect_db() as conn:
        evidence = [dict(r) for r in conn.execute(
            "SELECT * FROM evidence_items ORDER BY added_utc")]
        parser_runs = [dict(r) for r in conn.execute(
            "SELECT * FROM parser_runs ORDER BY started_utc")]

    entries = load_timeline(case, limit=TIMELINE_ROW_CAP + 1)
    timeline_truncated = len(entries) > TIMELINE_ROW_CAP
    entries = entries[:TIMELINE_ROW_CAP]
    rows = []
    for e in entries:
        row = e.__dict__.copy()
        compact = json.dumps(e.payload, sort_keys=True, ensure_ascii=False)
        row["payload_compact"] = compact[:160] + ("..." if len(compact) > 160 else "")
        rows.append(row)

    return ReportData(
        generated_utc=generated_utc,
        verify=verify,
        evidence=evidence,
        parser_runs=parser_runs,
        timeline_rows=rows,
        timeline_truncated=timeline_truncated,
        timeline_summary=summarize(case),
        tracks=build_tracks(case) + rid_tracks(case),
        media=media_points(case),
        flights=reconstruct(case),
        correlations=correlate_media(case),
        findings=run_checks(case),
    )


def make_track_svg(tracks: list[Track], media: list[MediaPoint]) -> str | None:
    points = [(p.lat, p.lon) for t in tracks for p in t.points]
    points += [(m.lat, m.lon) for m in media]
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat0, lat1, lon0, lon1 = min(lats), max(lats), min(lons), max(lons)
    # Equirectangular with latitude correction; keep aspect ratio.
    coslat = math.cos(math.radians((lat0 + lat1) / 2)) or 1e-9
    span_x = max((lon1 - lon0) * coslat, 1e-7)
    span_y = max(lat1 - lat0, 1e-7)
    scale = min((_SVG_W - 2 * _SVG_PAD) / span_x, (_SVG_H - 2 * _SVG_PAD) / span_y)

    def xy(lat: float, lon: float) -> tuple[float, float]:
        x = _SVG_PAD + (lon - lon0) * coslat * scale
        y = _SVG_H - _SVG_PAD - (lat - lat0) * scale
        return round(x, 1), round(y, 1)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_SVG_W} {_SVG_H}" '
        f'width="{_SVG_W}" height="{_SVG_H}" role="img" '
        'aria-label="Schematic track plot">',
        f'<text x="{_SVG_PAD}" y="{_SVG_H - 12}" font-size="11" fill="#555" '
        f'font-family="monospace">SW {lat0:.6f}, {lon0:.6f}</text>',
        f'<text x="{_SVG_PAD}" y="20" font-size="11" fill="#555" '
        f'font-family="monospace">NE {lat1:.6f}, {lon1:.6f}</text>',
    ]
    legend_y = 36
    for i, track in enumerate(tracks):
        color = _TRACK_COLORS[i % len(_TRACK_COLORS)]
        pts = " ".join(f"{x},{y}" for x, y in (xy(p.lat, p.lon) for p in track.points))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     'stroke-width="2"/>')
        if track.points:
            sx, sy = xy(track.points[0].lat, track.points[0].lon)
            ex, ey = xy(track.points[-1].lat, track.points[-1].lon)
            parts.append(f'<circle cx="{sx}" cy="{sy}" r="5" fill="none" '
                         f'stroke="{color}" stroke-width="2"/>')  # start: ring
            parts.append(f'<rect x="{ex - 4}" y="{ey - 4}" width="8" height="8" '
                         f'fill="{color}"/>')                      # end: square
        parts.append(f'<text x="{_SVG_W - _SVG_PAD - 320}" y="{legend_y}" font-size="11" '
                     f'fill="{color}" font-family="monospace">- '
                     f'{escape(track.label[:52])}</text>')
        legend_y += 15
    for m in media:
        x, y = xy(m.lat, m.lon)
        parts.append(f'<circle cx="{x}" cy="{y}" r="5" fill="#b8860b" fill-opacity="0.85"/>'
                     f'<title>media: {escape(m.evidence_name)}</title>')
    if media:
        parts.append(f'<text x="{_SVG_W - _SVG_PAD - 320}" y="{legend_y}" font-size="11" '
                     'fill="#b8860b" font-family="monospace">● media capture (EXIF '
                     'position)</text>')
    parts.append('<text x="8" y="14" font-size="10" fill="#888">start: ○  end: ■</text>')
    parts.append("</svg>")
    return "".join(parts)


def _finalize(case: CaseWorkspace, actor: str, out_path: Path, data: ReportData,
              fmt: str) -> GeneratedReport:
    digest = hash_file(out_path).sha256
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {out_path.name}\n", encoding="utf-8")
    case.audit.append(
        actor, "report.generate", target=str(out_path.name),
        params={"sha256": digest, "format": fmt,
                "timeline_rows": len(data.timeline_rows), "verify_ok": data.verify.ok},
    )
    return GeneratedReport(path=out_path, sha256=digest, sidecar_path=sidecar)


def _default_out(case: CaseWorkspace, generated_utc: str, ext: str) -> Path:
    stamp = generated_utc.replace(":", "").replace("-", "")[:15]
    return case.root / "reports" / f"report-{stamp}.{ext}"


def generate_html_report(
    case: CaseWorkspace, *, actor: str, out_path: Path | None = None
) -> GeneratedReport:
    data = collect_report_data(case, actor)

    env = Environment(autoescape=True)
    template = env.from_string(
        (files("droneforen") / "templates" / "report.html.j2").read_text(encoding="utf-8")
    )
    html = template.render(
        meta=case.meta,
        actor=actor,
        generated_utc=data.generated_utc,
        tool_version=__version__,
        python_version=".".join(map(str, sys.version_info[:3])),
        verify=data.verify,
        evidence=data.evidence,
        flights=data.flights,
        svg_map=make_track_svg(data.tracks, data.media),
        correlations=data.correlations,
        findings=data.findings,
        timeline_summary=data.timeline_summary,
        timeline_entries=data.timeline_rows,
        timeline_truncated=data.timeline_truncated,
        parser_runs=data.parser_runs,
    )

    if out_path is None:
        out_path = _default_out(case, data.generated_utc, "html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8", newline="\n")
    return _finalize(case, actor, out_path, data, "html")

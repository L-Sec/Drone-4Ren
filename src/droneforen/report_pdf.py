"""PDF rendering of the case report (same collected data as the HTML report).

Uses fpdf2. Unicode: a system TTF is registered when one is found (Windows:
Segoe UI/Arial; Linux: DejaVu Sans); otherwise the core Helvetica font is used
and non-Latin-1 characters are replaced - the substitution is noted in the
document footer so no one mistakes replaced text for source content.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

from fpdf import FPDF

from . import __version__
from .case import CaseWorkspace
from .report import (
    GeneratedReport,
    ReportData,
    _default_out,
    _finalize,
    collect_report_data,
)

_TTF_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_PDF_TIMELINE_CAP = 200
_PLOT_W, _PLOT_H = 180.0, 110.0  # mm


class _FontSubsetNoiseFilter(logging.Filter):
    """Hide the known notice for optional font tables dropped by fontTools."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "NOT subset; don't know how to subset; dropped" not in record.getMessage()


_FONT_SUBSET_NOISE_FILTER = _FontSubsetNoiseFilter()


class _Doc(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=16)
        self.unicode_ok = False
        for candidate in _TTF_CANDIDATES:
            if Path(candidate).exists():
                try:
                    self.add_font("body", fname=candidate)
                    # same face registered for bold: table headings request "B"
                    self.add_font("body", style="B", fname=candidate)
                    self.unicode_ok = True
                    break
                except Exception:
                    continue
        self.body_family = "body" if self.unicode_ok else "helvetica"

    def clean(self, text: object) -> str:
        s = "" if text is None else str(text)
        if self.unicode_ok:
            return s
        return s.encode("latin-1", "replace").decode("latin-1")

    def heading(self, text: str, size: int = 12) -> None:
        self.set_font("helvetica", style="B", size=size)
        self.ln(3)
        self.cell(0, 7, self.clean(text), new_x="LMARGIN", new_y="NEXT")
        self.set_font(self.body_family, size=8)

    def para(self, text: str, size: int = 8) -> None:
        self.set_font(self.body_family, size=size)
        self.multi_cell(0, 4, self.clean(text), new_x="LMARGIN", new_y="NEXT")

    def kv_table(self, pairs: list[tuple[str, str]]) -> None:
        self.set_font(self.body_family, size=8)
        with self.table(col_widths=(45, 135), line_height=5,
                        borders_layout="ALL") as table:
            for key, value in pairs:
                row = table.row()
                row.cell(self.clean(key))
                row.cell(self.clean(value))

    def data_table(self, header: list[str], rows: list[list[str]],
                   widths: tuple | None = None) -> None:
        self.set_font(self.body_family, size=7)
        kwargs = {"line_height": 4.2, "borders_layout": "ALL"}
        if widths:
            kwargs["col_widths"] = widths
        with self.table(**kwargs) as table:
            head = table.row()
            for h in header:
                head.cell(self.clean(h))
            for r in rows:
                row = table.row()
                for value in r:
                    row.cell(self.clean(value))


def _fmt(value, spec: str = "", none: str = "-") -> str:
    if value is None:
        return none
    return format(value, spec) if spec else str(value)


def _draw_track_plot(pdf: _Doc, data: ReportData) -> None:
    points = [(p.lat, p.lon) for t in data.tracks for p in t.points]
    points += [(m.lat, m.lon) for m in data.media]
    if not points:
        pdf.para("No positions to plot.")
        return
    lat0, lat1 = min(p[0] for p in points), max(p[0] for p in points)
    lon0, lon1 = min(p[1] for p in points), max(p[1] for p in points)
    coslat = math.cos(math.radians((lat0 + lat1) / 2)) or 1e-9
    span_x = max((lon1 - lon0) * coslat, 1e-7)
    span_y = max(lat1 - lat0, 1e-7)
    pad = 8.0
    scale = min((_PLOT_W - 2 * pad) / span_x, (_PLOT_H - 2 * pad) / span_y)

    if pdf.get_y() + _PLOT_H + 20 > pdf.h - pdf.b_margin:
        pdf.add_page()
    x_origin, y_origin = pdf.l_margin, pdf.get_y() + 2
    pdf.set_draw_color(150, 150, 150)
    pdf.rect(x_origin, y_origin, _PLOT_W, _PLOT_H)

    def xy(lat: float, lon: float) -> tuple[float, float]:
        x = x_origin + pad + (lon - lon0) * coslat * scale
        y = y_origin + _PLOT_H - pad - (lat - lat0) * scale
        return x, y

    colors = [(31, 78, 121), (122, 46, 46), (46, 107, 46), (106, 74, 138)]
    legend = []
    for i, track in enumerate(data.tracks):
        r, g, b = colors[i % len(colors)]
        pdf.set_draw_color(r, g, b)
        pdf.set_line_width(0.5)
        for a, c in zip(track.points, track.points[1:], strict=False):
            x1, y1 = xy(a.lat, a.lon)
            x2, y2 = xy(c.lat, c.lon)
            pdf.line(x1, y1, x2, y2)
        if track.points:
            sx, sy = xy(track.points[0].lat, track.points[0].lon)
            pdf.ellipse(sx - 1.2, sy - 1.2, 2.4, 2.4)
        legend.append((track.label, (r, g, b)))
    pdf.set_draw_color(184, 134, 11)
    pdf.set_fill_color(184, 134, 11)
    for m in data.media:
        x, y = xy(m.lat, m.lon)
        pdf.ellipse(x - 1.0, y - 1.0, 2.0, 2.0, style="F")

    pdf.set_y(y_origin + _PLOT_H + 2)
    pdf.set_font(pdf.body_family, size=7)
    pdf.cell(0, 4, pdf.clean(f"SW {lat0:.6f}, {lon0:.6f}   NE {lat1:.6f}, {lon1:.6f}   "
                             "(start of each track: circle; media: filled dot)"),
             new_x="LMARGIN", new_y="NEXT")
    for label, (r, g, b) in legend:
        pdf.set_text_color(r, g, b)
        pdf.cell(0, 4, pdf.clean(f"- {label}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.para("Schematic plot of parsed position fixes (no basemap; report is "
             "offline and self-contained). Use KML/GPX/GeoJSON exports for GIS overlay.")


def generate_pdf_report(
    case: CaseWorkspace, *, actor: str, out_path: Path | None = None
) -> GeneratedReport:
    data = collect_report_data(case, actor)
    pdf = _Doc()
    pdf.add_page()

    pdf.set_font("helvetica", style="B", size=15)
    pdf.cell(0, 9, "UAS Flight Data Report", new_x="LMARGIN", new_y="NEXT")
    pdf.kv_table([
        ("Case number", case.meta.case_number),
        ("Case id", case.meta.case_id),
        ("Context", case.meta.context),
        ("Operator label", case.meta.collector),
        ("Case created (UTC)", case.meta.created_utc),
        ("Report generated (UTC)", data.generated_utc),
        ("Activity label", actor),
        ("Tool", f"droneforen {__version__} (Python "
                 f"{'.'.join(map(str, sys.version_info[:3]))})"),
    ])
    pdf.para("Derived statements below are computed from source events by the stated "
             "method and are working hypotheses, not conclusions. This PDF's SHA-256 "
             "is written to a sidecar file at generation time.")

    pdf.heading("1. Integrity verification at generation time")
    ok = data.verify.ok
    pdf.para(f"Evidence items rehashed: {data.verify.store.item_count} - "
             f"{'all hashes match' if data.verify.store.ok else 'MISMATCHES FOUND'}. "
             f"Audit log: {data.verify.audit.record_count} records - "
             f"{'chain intact' if data.verify.audit.ok else 'CHAIN BROKEN'}. "
             f"Chain head: {data.verify.audit.head_sha256}"
             + ("" if ok else "  *** VERIFICATION FAILED - see problems below ***"))
    for problem in data.verify.problems:
        pdf.para(f"PROBLEM: {problem}")

    pdf.heading("2. Evidence inventory  [source-derived]")
    pdf.data_table(
        ["Item", "SHA-256", "Size", "Acquisition", "Origin", "Added (UTC)"],
        [[e["original_name"], e["sha256"], str(e["size_bytes"]),
          e["acquisition_method"], e["data_origin"], e["added_utc"]]
         for e in data.evidence],
        widths=(28, 62, 12, 20, 24, 34),
    )

    pdf.heading("3. Flight reconstruction  [analyst-derived]")
    if not data.flights:
        pdf.para("No flight telemetry sources in this case.")
    for f in data.flights:
        pdf.set_font("helvetica", style="B", size=9)
        pdf.cell(0, 5, pdf.clean(f"{f.evidence_name} ({f.evidence_sha256[:12]}, "
                                 f"{f.parser})"), new_x="LMARGIN", new_y="NEXT")
        alt = (f"{f.max_alt_agl_m:.1f} m AGL" if f.max_alt_agl_m is not None else
               f"{f.max_alt_msl_m:.1f} m MSL" if f.max_alt_msl_m is not None else "-")
        pdf.kv_table([
            ("First / last fix (UTC)", f"{_fmt(f.first_fix_utc)}  /  "
                                       f"{_fmt(f.last_fix_utc)}"),
            ("Duration / path", f"{_fmt(f.duration_s, '.1f')} s  /  "
                                f"{_fmt(f.path_distance_m, '.0f')} m"),
            ("Max altitude / speed", f"{alt}  /  {_fmt(f.max_speed_ms, '.1f')} m/s"),
            ("Position fixes", f"{f.position_count} (after parser downsampling)"),
        ])
        pdf.data_table(
            ["Derived item", "Time (UTC)", "Detail", "Method"],
            [[i.kind, _fmt(i.ts_utc), i.description, i.method] for i in f.items],
            widths=(20, 38, 62, 60),
        )

    pdf.heading("4. Track plot  [analyst-derived]")
    _draw_track_plot(pdf, data)

    pdf.heading("5. Media correlation  [analyst-derived]")
    if data.correlations:
        pdf.data_table(
            ["Media", "Capture (UTC)", "On track", "Track position", "Offset", "Notes"],
            [[c.media_name, _fmt(c.ts_utc), "yes" if c.correlated else "no",
              (f"{c.track_lat:.6f}, {c.track_lon:.6f}" if c.correlated else "-"),
              f"{c.offset_m} m" if c.offset_m is not None else "-",
              c.reason or ""] for c in data.correlations],
            widths=(26, 34, 12, 40, 16, 52),
        )
    else:
        pdf.para("No media capture events in this case.")

    pdf.heading("6. Plausibility & integrity findings  [analyst-derived]")
    if data.findings:
        pdf.data_table(
            ["Severity", "Check", "Description"],
            [[f.severity.upper(), f.check, f.description] for f in data.findings],
            widths=(16, 34, 130),
        )
    else:
        pdf.para("No findings. Absence of findings is not proof of authenticity.")

    pdf.heading("7. Unified timeline  [source-derived]")
    shown = data.timeline_rows[:_PDF_TIMELINE_CAP]
    pdf.para(f"{data.timeline_summary.total_events} events total "
             f"({data.timeline_summary.anchored_events} UTC-anchored, "
             f"{data.timeline_summary.unanchored_events} unanchored); showing "
             f"{len(shown)}. Full timeline: droneforen analyze timeline / CSV export.")
    pdf.data_table(
        ["Time (UTC)", "Event", "Position", "Source"],
        [[r["ts_utc"] or f"({r['ts_original'] or 'no time'})", r["event_type"],
          (f"{r['lat']:.6f}, {r['lon']:.6f}" if r["lat"] is not None else ""),
          f"{r['evidence_name']}:{r['source_offset'] or '-'}"] for r in shown],
        widths=(40, 26, 40, 74),
    )

    pdf.heading("8. Reproducibility appendix")
    pdf.data_table(
        ["Run id", "Input SHA-256", "Parser", "Status", "Events", "Started (UTC)"],
        [[r["id"], r["input_sha256"], f"{r['parser_name']} {r['parser_version']}",
          r["status"], _fmt(r["event_count"]), r["started_utc"]]
         for r in data.parser_runs],
        widths=(34, 50, 26, 14, 12, 44),
    )
    pdf.para("To reproduce: intake the evidence in section 2 (verify SHA-256), run "
             "each parser at the listed version, compare event output. The case "
             "database is derived and rebuildable from evidence plus these runs.")
    footer = (f"Generated by droneforen {__version__} on {data.generated_utc} by "
              f"{actor}. Audit chain head: {data.verify.audit.head_sha256}.")
    if not pdf.unicode_ok:
        footer += (" Note: no Unicode font was available on this system; non-Latin-1 "
                   "characters in source text are shown as '?' in this PDF (the HTML "
                   "report and database preserve them exactly).")
    pdf.para(footer, size=7)

    if out_path is None:
        out_path = _default_out(case, data.generated_utc, "pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subset_logger = logging.getLogger("fontTools.subset")
    subset_logger.addFilter(_FONT_SUBSET_NOISE_FILTER)
    try:
        pdf.output(str(out_path))
    finally:
        subset_logger.removeFilter(_FONT_SUBSET_NOISE_FILTER)
    return _finalize(case, actor, out_path, data, "pdf")

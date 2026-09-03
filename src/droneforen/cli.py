"""Drone 4Ren command-line interface.

Commands that touch a case append an activity record, including read-only views.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import __version__
from .case import CaseError, CaseWorkspace
from .evidence import EvidenceStoreError
from .models import DataOrigin, WriteBlockerInfo
from .parsers import detect_parsers, load_registry
from .parsers.runner import run_parser

app = typer.Typer(help="Drone 4Ren: open-source UAS forensic workspace.", no_args_is_help=True)
case_app = typer.Typer(help="Case lifecycle: create, inspect, verify, audit log.",
                       no_args_is_help=True)
evidence_app = typer.Typer(help="Evidence intake, listing, and parsing.", no_args_is_help=True)
parsers_app = typer.Typer(help="Parser plugin registry.", no_args_is_help=True)
analyze_app = typer.Typer(help="Derived analysis: timeline, flight, checks.",
                          no_args_is_help=True)
export_app = typer.Typer(help="Geospatial exports (KML, GPX, GeoJSON, CSV).",
                         no_args_is_help=True)
report_app = typer.Typer(help="Report generation.", no_args_is_help=True)
readiness_app = typer.Typer(
    help="Forensic readiness mode: continuous observation logging separate "
         "from case evidence.", no_args_is_help=True)
app.add_typer(case_app, name="case")
app.add_typer(evidence_app, name="evidence")
app.add_typer(parsers_app, name="parsers")
app.add_typer(analyze_app, name="analyze")
app.add_typer(export_app, name="export")
app.add_typer(report_app, name="report")
app.add_typer(readiness_app, name="readiness")


def _default_actor() -> str:
    return "operator"


def _open_case(path: Path) -> CaseWorkspace:
    try:
        return CaseWorkspace.open(path)
    except CaseError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.callback()
def _root() -> None:
    """Drone 4Ren: open-source UAS forensic workspace."""


@app.command()
def version() -> None:
    """Show the droneforen version."""
    typer.echo(f"droneforen {__version__}")


@app.command()
def serve(
    case_path: Path = typer.Argument(..., help="Case directory to serve."),
    port: int = typer.Option(8321, "--port", "-p"),
    actor: str = typer.Option(None, "--actor",
                              help="Name recorded for GUI/API actions."),
) -> None:
    """Start the localhost web GUI and API for a case (127.0.0.1 only).

    No authentication: single-user tool. Do not port-forward or proxy
    this service onto a network.
    """
    import uvicorn

    from .api import create_app

    case = _open_case(case_path)  # fail fast with a clear error
    typer.echo(f"serving case {case.meta.case_number} at http://127.0.0.1:{port}")
    typer.echo("localhost only; all actions attributed to "
               f"{actor or _default_actor()!r}")
    uvicorn.run(create_app(case_path, actor or _default_actor()),
                host="127.0.0.1", port=port, log_level="warning")


# -- case ---------------------------------------------------------------------


@case_app.command("create")
def case_create(
    path: Path = typer.Argument(..., help="Directory to create for the case."),
    case_number: str = typer.Option(..., "--case-number", "-n"),
    context: str = typer.Option("local", "--context",
                                help="Optional context label."),
    collector: str = typer.Option(
        "operator", "--collector", help="Optional operator label."
    ),
    description: str = typer.Option(None, "--description", "-d"),
    scope_start: str = typer.Option(None, "--scope-start",
                                    help="Analysis window start (ISO-8601 UTC)."),
    scope_end: str = typer.Option(None, "--scope-end",
                                  help="Analysis window end (ISO-8601 UTC)."),
    scope_center: str = typer.Option(None, "--scope-center",
                                     help="Analysis area center as 'lat,lon'."),
    scope_radius_m: float = typer.Option(None, "--scope-radius-m",
                                         help="Analysis area radius in meters."),
    actor: str = typer.Option(None, "--actor", help="Activity label."),
) -> None:
    """Create a new case directory with evidence store, database, and audit log."""
    from .models import CaseScope

    center_lat = center_lon = None
    if scope_center:
        try:
            lat_s, lon_s = scope_center.split(",", 1)
            center_lat, center_lon = float(lat_s), float(lon_s)
        except ValueError as exc:
            typer.echo("error: --scope-center must be 'lat,lon'", err=True)
            raise typer.Exit(code=2) from exc
    scope = CaseScope(start_utc=scope_start, end_utc=scope_end,
                      area_center_lat=center_lat, area_center_lon=center_lon,
                      area_radius_m=scope_radius_m)
    try:
        case = CaseWorkspace.create(
            path,
            case_number=case_number,
            collector=collector,
            context=context,
            description=description,
            scope=scope,
            actor=actor or _default_actor(),
        )
    except CaseError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"created case {case.meta.case_number} at {case.root}")
    typer.echo(f"case id: {case.meta.case_id}")


@case_app.command("info")
def case_info(
    path: Path = typer.Argument(...),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Show case metadata."""
    case = _open_case(path)
    typer.echo(json.dumps(case.meta.model_dump(mode="json"), indent=2, sort_keys=True))
    case.audit.append(actor or _default_actor(), "case.view", target=case.meta.case_number)


@case_app.command("verify")
def case_verify(
    path: Path = typer.Argument(...),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Rehash all evidence and verify the audit-log hash chain."""
    case = _open_case(path)
    result = case.verify(actor or _default_actor())
    typer.echo(f"evidence items checked: {result.store.item_count}")
    typer.echo(f"audit records checked:  {result.audit.record_count}")
    typer.echo(f"audit chain head:       {result.audit.head_sha256}")
    typer.echo("record the chain head out-of-band; end-truncation is otherwise undetectable")
    if result.ok:
        typer.echo("VERIFY OK: evidence hashes match, audit chain intact")
    else:
        typer.echo("VERIFY FAILED:", err=True)
        for problem in result.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=1)


@case_app.command("log")
def case_log(
    path: Path = typer.Argument(...),
    limit: int = typer.Option(0, "--limit", help="Show only the last N records (0 = all)."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Print the audit log."""
    case = _open_case(path)
    records = case.audit.records()
    shown = records[-limit:] if limit > 0 else records
    for rec in shown:
        params = json.dumps(rec["params"], sort_keys=True, ensure_ascii=False)
        typer.echo(
            f"[{rec['seq']:>4}] {rec['ts_utc']}  {rec['actor']:<16} "
            f"{rec['action']:<16} {rec['target']}  {params}"
        )
    case.audit.append(
        actor or _default_actor(), "audit.view",
        target=case.meta.case_number, params={"records_shown": len(shown)},
    )


# -- evidence -------------------------------------------------------------------


@evidence_app.command("add")
def evidence_add(
    case_path: Path = typer.Argument(...),
    file: Path = typer.Argument(..., help="Evidence file to intake (image, log, export...)."),
    description: str = typer.Option(None, "--description", "-d"),
    acquisition_method: str = typer.Option(
        "logical", "--acquisition-method",
        help="logical | physical | chip-off | jtag | uart | isp | cloud | "
             "manufacturer-export | image-import",
    ),
    data_origin: DataOrigin = typer.Option(DataOrigin.ORIGINAL_RAW, "--data-origin"),
    source_device: str = typer.Option(None, "--source-device",
                                      help="Device the data came from (e.g. 'DJI Mavic 3 SD')."),
    wb_make: str = typer.Option(None, "--wb-make", help="Write blocker make."),
    wb_model: str = typer.Option(None, "--wb-model"),
    wb_serial: str = typer.Option(None, "--wb-serial"),
    wb_firmware: str = typer.Option(None, "--wb-firmware"),
    wb_connection: str = typer.Option(None, "--wb-connection"),
    wb_test_result: str = typer.Option(None, "--wb-test-result"),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Import a file, record hashes, and store a read-only copy."""
    case = _open_case(case_path)
    write_blocker = None
    wb_fields = dict(make=wb_make, model=wb_model, serial=wb_serial, firmware=wb_firmware,
                     connection=wb_connection, test_result=wb_test_result)
    if any(v is not None for v in wb_fields.values()):
        write_blocker = WriteBlockerInfo(**wb_fields)
    try:
        added = case.add_evidence(
            file,
            actor=actor or _default_actor(),
            description=description,
            acquisition_method=acquisition_method,
            data_origin=data_origin,
            source_device=source_device,
            write_blocker=write_blocker,
        )
    except (CaseError, EvidenceStoreError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"added evidence {added.manifest.original_name}")
    typer.echo(f"  sha256: {added.manifest.sha256}")
    typer.echo(f"  md5:    {added.manifest.md5}")
    typer.echo(f"  size:   {added.manifest.size_bytes} bytes")
    typer.echo(f"  root artifact id: {added.artifact_id}")
    if write_blocker is None:
        typer.echo("  note: no write-blocker documented for this intake", err=True)


@evidence_app.command("list")
def evidence_list(
    case_path: Path = typer.Argument(...),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """List intaken evidence items."""
    case = _open_case(case_path)
    with case.connect_db() as conn:
        rows = conn.execute(
            "SELECT sha256, original_name, size_bytes, acquisition_method, data_origin, "
            "added_utc FROM evidence_items ORDER BY added_utc"
        ).fetchall()
    if not rows:
        typer.echo("no evidence items")
    for row in rows:
        typer.echo(
            f"{row['sha256'][:12]}  {row['added_utc']}  {row['size_bytes']:>10}  "
            f"{row['acquisition_method']:<10} {row['data_origin']:<22} {row['original_name']}"
        )
    case.audit.append(actor or _default_actor(), "evidence.view",
                      target=case.meta.case_number, params={"items": len(rows)})


@evidence_app.command("parse")
def evidence_parse(
    case_path: Path = typer.Argument(...),
    sha_prefix: str = typer.Argument(..., help="SHA-256 (or unique prefix) of the evidence item."),
    parser: str = typer.Option(None, "--parser",
                               help="Parser name; auto-detected when omitted."),
    timeout: int = typer.Option(300, "--timeout", help="Parser timeout in seconds."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Run a parser over an evidence item's root artifact."""
    case = _open_case(case_path)
    try:
        sha256 = case.store.resolve_prefix(sha_prefix)
    except EvidenceStoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    with case.connect_db() as conn:
        row = conn.execute(
            "SELECT a.id FROM artifacts a JOIN evidence_items e ON a.evidence_id = e.id "
            "WHERE e.sha256 = ? AND a.parent_artifact_id IS NULL",
            (sha256,),
        ).fetchone()
    if row is None:
        typer.echo(f"error: no root artifact for evidence {sha256}", err=True)
        raise typer.Exit(code=2)
    artifact_id = row["id"]

    parser_name = parser
    if parser_name is None:
        candidates = detect_parsers(case.store.data_path(sha256))
        if not candidates:
            typer.echo("error: no registered parser detects this format; "
                       "use --parser to force one", err=True)
            raise typer.Exit(code=2)
        parser_name = candidates[0]
        if len(candidates) > 1:
            typer.echo(f"multiple parsers match ({', '.join(candidates)}); "
                       f"using {parser_name}")

    outcome = run_parser(case, artifact_id, parser_name,
                         actor=actor or _default_actor(), timeout=timeout)
    typer.echo(f"parser:  {outcome.parser_name} {outcome.parser_version}")
    typer.echo(f"run id:  {outcome.run_id}")
    typer.echo(f"status:  {outcome.status}")
    if outcome.status == "completed":
        typer.echo(f"events:  {outcome.event_count}")
    else:
        typer.echo(f"error:   {outcome.error}", err=True)
        raise typer.Exit(code=1)


@evidence_app.command("events")
def evidence_events(
    case_path: Path = typer.Argument(...),
    event_type: str = typer.Option(None, "--type"),
    limit: int = typer.Option(20, "--limit"),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """List normalized events with their provenance."""
    case = _open_case(case_path)
    query = (
        "SELECT ev.event_type, ev.ts_utc, ev.source_offset, ev.payload_json, "
        "pr.parser_name, pr.parser_version, ei.sha256 "
        "FROM events ev "
        "JOIN parser_runs pr ON ev.parser_run_id = pr.id "
        "JOIN artifacts a ON ev.artifact_id = a.id "
        "JOIN evidence_items ei ON a.evidence_id = ei.id "
    )
    params: list = []
    if event_type:
        query += "WHERE ev.event_type = ? "
        params.append(event_type)
    query += "ORDER BY ev.ts_utc, ev.source_offset LIMIT ?"
    params.append(limit)
    with case.connect_db() as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        typer.echo("no events")
    for row in rows:
        typer.echo(
            f"{row['event_type']:<18} ts={row['ts_utc'] or '-':<28} "
            f"off={row['source_offset'] or '-':<10} "
            f"src={row['sha256'][:12]} via {row['parser_name']} {row['parser_version']}  "
            f"{row['payload_json']}"
        )
    case.audit.append(actor or _default_actor(), "events.view",
                      target=case.meta.case_number,
                      params={"shown": len(rows), "filter_type": event_type})


# -- analyze --------------------------------------------------------------------


@analyze_app.command("timeline")
def analyze_timeline(
    case_path: Path = typer.Argument(...),
    event_type: str = typer.Option(None, "--type"),
    source: str = typer.Option(None, "--source", help="Evidence SHA-256 prefix."),
    limit: int = typer.Option(50, "--limit", help="0 = all"),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Unified cross-source timeline with provenance."""
    from .analysis.timeline import load_timeline, summarize

    case = _open_case(case_path)
    summary = summarize(case)
    typer.echo(
        f"{summary.total_events} events ({summary.anchored_events} UTC-anchored, "
        f"{summary.unanchored_events} unanchored) across {len(summary.sources)} sources"
    )
    entries = load_timeline(case, event_type=event_type, evidence_sha_prefix=source,
                            limit=limit or None)
    for e in entries:
        ts = e.ts_utc or f"({e.ts_original or 'no time'} {e.clock_confidence or ''})"
        pos = ""
        if e.lat is not None:
            pos = f" [{e.lat:.6f},{e.lon:.6f}]"
        typer.echo(
            f"{ts:<35} {e.event_type:<18}{pos} "
            f"{e.evidence_name}:{e.source_offset or '-'} via {e.parser_name}"
        )
    case.audit.append(actor or _default_actor(), "analysis.timeline.view",
                      target=case.meta.case_number,
                      params={"shown": len(entries), "filter_type": event_type})


@analyze_app.command("flight")
def analyze_flight(
    case_path: Path = typer.Argument(...),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Flight reconstruction per telemetry source (analyst-derived)."""
    from .analysis.reconstruction import reconstruct

    case = _open_case(case_path)
    flights = reconstruct(case)
    if not flights:
        typer.echo("no flight telemetry sources in this case")
    for f in flights:
        typer.echo(f"=== {f.evidence_name} ({f.evidence_sha256[:12]}, {f.parser})")
        typer.echo(f"  fixes: {f.position_count}  first: {f.first_fix_utc}  "
                   f"last: {f.last_fix_utc}")
        if f.duration_s is not None:
            typer.echo(f"  duration: {f.duration_s:.1f} s   "
                       f"path: {f.path_distance_m:.0f} m")
        alt = (f"{f.max_alt_agl_m:.1f} m AGL" if f.max_alt_agl_m is not None
               else f"{f.max_alt_msl_m:.1f} m MSL" if f.max_alt_msl_m is not None else "-")
        spd = f"{f.max_speed_ms:.1f} m/s" if f.max_speed_ms is not None else "-"
        typer.echo(f"  max altitude: {alt}   max speed: {spd}")
        for item in f.items:
            typer.echo(f"  [{item.kind:<11}] {item.ts_utc or '-':<33} {item.description}")
            typer.echo(f"                derived by: {item.method}")
    case.audit.append(actor or _default_actor(), "analysis.flight.view",
                      target=case.meta.case_number, params={"sources": len(flights)})


@analyze_app.command("checks")
def analyze_checks(
    case_path: Path = typer.Argument(...),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Plausibility and integrity findings (analyst-derived; exit 1 on alerts)."""
    from .analysis.checks import run_checks

    case = _open_case(case_path)
    findings = run_checks(case)
    if not findings:
        typer.echo("no findings (absence of findings is not proof of authenticity)")
    for f in findings:
        typer.echo(f"[{f.severity.upper():<7}] {f.check}: {f.description}")
    case.audit.append(actor or _default_actor(), "analysis.checks.view",
                      target=case.meta.case_number, params={"findings": len(findings)})
    if any(f.severity == "alert" for f in findings):
        raise typer.Exit(code=1)


@analyze_app.command("entities")
def analyze_entities(
    case_path: Path = typer.Argument(...),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Entity link graph: identifiers, devices, accounts, sites (leads only)."""
    from .analysis.entities import build_entity_graph

    case = _open_case(case_path)
    graph = build_entity_graph(case)
    typer.echo(f"CAVEAT: {graph.caveat}")
    typer.echo(f"{len(graph.nodes)} entities, {len(graph.edges)} links")
    for n in graph.nodes:
        typer.echo(f"[{n.attribution_level:<8}] {n.type:<18} {n.label}  "
                   f"({n.observations} obs in {len(n.evidence)} item(s))")
        if n.note:
            typer.echo(f"           note: {n.note}")
    for e in graph.edges:
        typer.echo(f"LINK {e.relation}: {e.a}  <->  {e.b}  ({e.detail})")
    case.audit.append(actor or _default_actor(), "analysis.entities.view",
                      target=case.meta.case_number,
                      params={"nodes": len(graph.nodes), "edges": len(graph.edges)})


@analyze_app.command("scenario")
def analyze_scenario(
    case_path: Path = typer.Argument(...),
    name: str = typer.Argument(None, help="Scenario template; omit to list."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Evaluate a scenario worksheet (crash, flyaway, surveillance, intrusion)."""
    from .analysis.scenarios import evaluate_scenario, list_scenarios

    case = _open_case(case_path)
    if name is None:
        for s in list_scenarios():
            typer.echo(f"{s['scenario']:<14} {s['description']} "
                       f"({s['signal_count']} signals)")
        return
    try:
        result = evaluate_scenario(case, name)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"=== {result.scenario}: {result.description}")
    typer.echo(f"CAVEAT: {result.caveat}")
    for s in result.signals:
        typer.echo(f"[{s.status.upper():<7}] {s.name}")
        typer.echo(f"          {s.detail}")
    case.audit.append(actor or _default_actor(), "analysis.scenario.view",
                      target=case.meta.case_number, params={"scenario": name})


# -- export ---------------------------------------------------------------------


@export_app.command("track")
def export_track(
    case_path: Path = typer.Argument(...),
    fmt: str = typer.Option("kml", "--format", "-f", help="kml | gpx | geojson | csv"),
    out: Path = typer.Option(None, "--out", help="Defaults to the case reports/ dir."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Export per-source flight tracks and media points."""
    from .analysis.geospatial import EXPORTERS, build_tracks, media_points, rid_tracks
    from .hashing import hash_file

    case = _open_case(case_path)
    if fmt not in EXPORTERS:
        typer.echo(f"error: unknown format {fmt!r}; choose from "
                   f"{', '.join(sorted(EXPORTERS))}", err=True)
        raise typer.Exit(code=2)
    tracks = build_tracks(case) + rid_tracks(case)
    media = media_points(case)
    if not tracks and not media:
        typer.echo("error: no positions or media points to export", err=True)
        raise typer.Exit(code=2)
    content = EXPORTERS[fmt](tracks, media, case.meta.case_number)
    if out is None:
        out = case.root / "reports" / f"track.{fmt}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8", newline="\n")
    digest = hash_file(out).sha256
    typer.echo(f"wrote {out} ({len(content)} chars)")
    typer.echo(f"sha256: {digest}")
    case.audit.append(actor or _default_actor(), "export.track",
                      target=str(out.name),
                      params={"format": fmt, "sha256": digest,
                              "tracks": len(tracks), "media_points": len(media)})


# -- report ---------------------------------------------------------------------


@report_app.command("html")
def report_html(
    case_path: Path = typer.Argument(...),
    out: Path = typer.Option(None, "--out", help="Defaults to reports/report-<ts>.html."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Generate an HTML data-review report with parser details."""
    from .report import generate_html_report

    case = _open_case(case_path)
    result = generate_html_report(case, actor=actor or _default_actor(), out_path=out)
    typer.echo(f"report:  {result.path}")
    typer.echo(f"sha256:  {result.sha256}")
    typer.echo(f"sidecar: {result.sidecar_path}")


@report_app.command("pdf")
def report_pdf(
    case_path: Path = typer.Argument(...),
    out: Path = typer.Option(None, "--out", help="Defaults to reports/report-<ts>.pdf."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Generate the PDF report (same content as the HTML report)."""
    from .report_pdf import generate_pdf_report

    case = _open_case(case_path)
    result = generate_pdf_report(case, actor=actor or _default_actor(), out_path=out)
    typer.echo(f"report:  {result.path}")
    typer.echo(f"sha256:  {result.sha256}")
    typer.echo(f"sidecar: {result.sidecar_path}")


@export_app.command("bundle")
def export_bundle_cmd(
    case_path: Path = typer.Argument(...),
    out: Path = typer.Option(None, "--out",
                             help="Defaults to <case>-bundle.zip next to the case dir."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Export the whole case as a verifiable ZIP bundle."""
    from .bundle import export_bundle

    case = _open_case(case_path)
    try:
        result = export_bundle(case, actor=actor or _default_actor(), out_path=out)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"bundle: {result.path} ({result.file_count} files)")
    typer.echo(f"sha256: {result.sha256}")
    typer.echo("keep the bundle hash with your records")


@analyze_app.command("feasibility")
def analyze_feasibility(
    case_path: Path = typer.Argument(...),
    mass_kg: float = typer.Option(..., "--mass-kg",
                                  help="All-up mass incl. battery and payload."),
    battery_wh: float = typer.Option(..., "--battery-wh"),
    rotors: int = typer.Option(4, "--rotors"),
    rotor_diameter_m: float = typer.Option(0.24, "--rotor-diameter-m"),
    duration_s: float = typer.Option(None, "--duration-s",
                                     help="Override observed duration (defaults to "
                                          "longest reconstructed track)."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Endurance feasibility model (interval + assumptions, not a conclusion)."""
    from .analysis.feasibility import FeasibilityInputs, assess_flight_feasibility

    case = _open_case(case_path)
    try:
        result = assess_flight_feasibility(
            case,
            FeasibilityInputs(mass_kg=mass_kg, battery_wh=battery_wh, rotors=rotors,
                              rotor_diameter_m=rotor_diameter_m),
            observed_duration_s=duration_s,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"hover power (model): {result.hover_power_w_best}-"
               f"{result.hover_power_w_worst} W")
    typer.echo(f"endurance interval:  {result.endurance_s_min / 60:.1f}-"
               f"{result.endurance_s_max / 60:.1f} min")
    if result.observed_duration_s is not None:
        typer.echo(f"observed duration:   {result.observed_duration_s / 60:.1f} min "
                   f"({result.observed_source})")
    typer.echo(f"verdict: {result.verdict}")
    typer.echo("assumptions:")
    for a in result.assumptions:
        typer.echo(f"  - {a}")
    typer.echo(f"CAVEAT: {result.caveat}")
    case.audit.append(actor or _default_actor(), "analysis.feasibility.view",
                      target=case.meta.case_number,
                      params={"verdict": result.verdict, "mass_kg": mass_kg,
                              "battery_wh": battery_wh})


# -- readiness ------------------------------------------------------------------


def _open_readiness(path: Path):
    from .readiness import ReadinessError, ReadinessStore

    try:
        return ReadinessStore.open(path)
    except ReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@readiness_app.command("init")
def readiness_init(
    path: Path = typer.Argument(..., help="Directory to create for the store."),
    site: str = typer.Option(..., "--site", help="Site or exercise name."),
    context: str = typer.Option("local", "--context",
                                help="Optional context label."),
    collection_purpose: str = typer.Option(..., "--purpose"),
    retention_days: int = typer.Option(30, "--retention-days"),
    notes: str = typer.Option(None, "--notes"),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Create an experimental observation-log store."""
    from .readiness import ReadinessError, ReadinessStore

    try:
        store = ReadinessStore.create(
            path, site=site, context=context,
            collection_purpose=collection_purpose, retention_days=retention_days,
            notes=notes, actor=actor or _default_actor())
    except ReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"created readiness store for site '{store.meta.site}' at {store.root}")
    typer.echo(f"retention: {retention_days} days; observations are hash-chained "
               "on write")


@readiness_app.command("ingest")
def readiness_ingest(
    path: Path = typer.Argument(...),
    file: Path = typer.Argument(..., help="JSON file: array or "
                                          "{'observations': [...]}."),
    sensor: str = typer.Option("unknown-sensor", "--sensor",
                               help="Receiver/sensor identity for the records."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Append observations from a JSON file to the current segment."""
    store = _open_readiness(path)
    doc = json.loads(file.read_text(encoding="utf-8-sig"))
    if isinstance(doc, dict):
        doc = doc.get("observations", [])
    if not isinstance(doc, list):
        typer.echo("error: expected a JSON array or an 'observations' array", err=True)
        raise typer.Exit(code=2)
    count = store.ingest(doc, actor=actor or _default_actor(), sensor=sensor)
    typer.echo(f"ingested {count} observation(s) into "
               f"{store.active_segment_path().name}")


@readiness_app.command("verify")
def readiness_verify(
    path: Path = typer.Argument(...),
) -> None:
    """Verify every segment chain, closed-segment hashes, and the store audit."""
    store = _open_readiness(path)
    typer.echo(f"site: {store.meta.site}  context: {store.meta.context}")
    typer.echo(f"purpose: {store.meta.collection_purpose}  "
               f"retention: {store.meta.retention_days} days")
    result = store.verify()
    typer.echo(f"segments checked: {result.segments_checked}  "
               f"records: {result.records_total}")
    if result.ok:
        typer.echo("READINESS VERIFY OK")
    else:
        typer.echo("READINESS VERIFY FAILED:", err=True)
        for p in result.problems:
            typer.echo(f"  - {p}", err=True)
        raise typer.Exit(code=1)


@readiness_app.command("prune")
def readiness_prune(
    path: Path = typer.Argument(...),
    archive_dir: Path = typer.Option(None, "--archive-dir",
                                     help="Move expired segments here (recommended)."),
    delete: bool = typer.Option(False, "--delete",
                                help="Discard expired segments instead of archiving."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Enforce the retention window (archive by default; deletion is explicit)."""
    from .readiness import ReadinessError

    store = _open_readiness(path)
    try:
        affected = store.prune(actor=actor or _default_actor(),
                               archive_dir=archive_dir, delete=delete)
    except ReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if affected:
        verb = "archived" if archive_dir else "deleted"
        for name in affected:
            typer.echo(f"{verb} {name} (hash recorded in store audit)")
    else:
        typer.echo("no segments beyond the retention window")


@readiness_app.command("promote")
def readiness_promote(
    path: Path = typer.Argument(...),
    segment: str = typer.Argument(..., help="Segment file name to promote."),
    case_path: Path = typer.Argument(..., help="Case directory to intake into."),
    actor: str = typer.Option(None, "--actor"),
) -> None:
    """Promote a readiness segment into a case as evidence (explicit intake)."""
    from .readiness import ReadinessError

    store = _open_readiness(path)
    case = _open_case(case_path)
    try:
        added = store.promote(segment, case, actor=actor or _default_actor())
    except ReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"promoted {segment} into case {case.meta.case_number}")
    typer.echo(f"  sha256: {added.manifest.sha256}")
    typer.echo(f"  parse with: droneforen evidence parse {case_path} "
               f"{added.manifest.sha256[:12]}")


# -- parsers --------------------------------------------------------------------


@parsers_app.command("list")
def parsers_list() -> None:
    """List registered parser plugins and their manifests."""
    for name, cls in sorted(load_registry().items()):
        m = cls.manifest
        formats = ", ".join(f"{f.vendor}/{f.artifact_type}" for f in m.formats) or "-"
        typer.echo(f"{name}  v{m.version}  [{m.support_level}]  formats: {formats}  "
                   f"origin: {m.data_origin.value}")
        for limitation in m.known_limitations:
            typer.echo(f"    limitation: {limitation}")


@parsers_app.command("matrix")
def parsers_matrix(
    fmt: str = typer.Option("md", "--format", "-f", help="md | json"),
    out: Path = typer.Option(None, "--out", help="Write to file instead of stdout."),
    validation_dir: Path = typer.Option(
        None, "--validation-dir",
        help="Validation suite directory for golden coverage (default: ./validation)."),
) -> None:
    """Generate the parser compatibility matrix from manifests + validation coverage."""
    from .compat import build_matrix, to_markdown

    if validation_dir is None:
        candidate = Path.cwd() / "validation"
        validation_dir = candidate if candidate.is_dir() else None
    matrix = build_matrix(validation_dir)
    if fmt == "json":
        content = json.dumps(matrix, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    elif fmt == "md":
        content = to_markdown(matrix)
    else:
        typer.echo(f"error: unknown format {fmt!r}; choose md or json", err=True)
        raise typer.Exit(code=2)
    if out:
        out.write_text(content, encoding="utf-8", newline="\n")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(content)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

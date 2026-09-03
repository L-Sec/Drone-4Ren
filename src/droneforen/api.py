"""Localhost API service over the core library - the "API mode" of
ARCHITECTURE.md §2, and the backend for the bundled web GUI.

Current scope and security limits:

- The app opens one case and the CLI binds it to 127.0.0.1. There is no user
  authentication. Do not expose it on a network.
- All mutating actions (intake, parse, verify, report, export) are audited by
  the underlying core calls, attributed to the actor the server was started
  as. Dashboard polling is not audited per request. CLI view commands still
  write audit records.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .analysis.checks import run_checks
from .analysis.correlation import correlate_media
from .analysis.geospatial import build_tracks, media_points, rid_tracks, to_geojson
from .analysis.reconstruction import reconstruct
from .analysis.timeline import load_timeline, summarize
from .case import CaseError, CaseWorkspace
from .compat import build_matrix
from .evidence import DuplicateEvidenceError, EvidenceStoreError
from .models import DataOrigin, WriteBlockerInfo
from .parsers import detect_parsers
from .parsers.runner import run_parser

# Shown by the intake wizard before a file is copied into the workspace.
PRE_ACTION_WARNINGS = [
    "Powering on a drone can overwrite logs and trigger network sync.",
    "Inserting or changing batteries can alter battery-state evidence.",
    "Connecting vendor software may modify or sync device data.",
    "Joining the device to a network can trigger cloud sync or remote wipe.",
    "Pairing or rebinding a controller alters binding records.",
    "Removing storage media without documenting its state loses context.",
    "Chemical/fingerprint processing before digital access can destroy data paths.",
    "This intake copies a file; acquiring the source device itself must follow "
    "your write-blocking and imaging SOPs first.",
]

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data: https://tile.openstreetmap.org; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'"
)


class ParseRequest(BaseModel):
    sha_prefix: str
    parser: str | None = None
    timeout: int = 300


class ReportRequest(BaseModel):
    format: str = "html"


class ExportRequest(BaseModel):
    format: str = "kml"


def create_app(case_root: Path, actor: str) -> FastAPI:
    case = CaseWorkspace.open(Path(case_root))
    app = FastAPI(title="Drone 4Ren", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def local_browser_boundary(request: Request, call_next):
        """Reject foreign browser origins and harden all local responses."""
        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            parsed = urlsplit(origin)
            request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
            origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            same_origin = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in _LOOPBACK_HOSTS
                and parsed.hostname == request.url.hostname
                and origin_port == request_port
            )
            if not same_origin:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "cross-origin mutation rejected"},
                )

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    case.audit.append(actor, "api.serve.start", target=case.meta.case_number,
                      params={"tool_version": __version__})

    # ---------------------------------------------------------------- reads

    @app.get("/api/case")
    def get_case():
        return {
            "meta": case.meta.model_dump(mode="json"),
            "tool_version": __version__,
            "actor": actor,
            "view_auditing": ("GUI reads are not per-request audited; mutations, "
                              "verify, reports and exports are"),
        }

    @app.get("/api/warnings")
    def get_warnings():
        return {"pre_action_warnings": PRE_ACTION_WARNINGS}

    @app.get("/api/evidence")
    def get_evidence():
        with case.connect_db() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT ei.*, COUNT(pr.id) AS parser_run_count "
                "FROM evidence_items ei "
                "LEFT JOIN artifacts a ON a.evidence_id = ei.id "
                "LEFT JOIN parser_runs pr ON pr.artifact_id = a.id "
                "GROUP BY ei.id ORDER BY ei.added_utc")]
        return {"items": items}

    @app.get("/api/timeline")
    def get_timeline(event_type: str | None = None, source: str | None = None,
                     limit: int = 100, offset: int = 0):
        entries = load_timeline(case, event_type=event_type or None,
                                evidence_sha_prefix=source or None,
                                limit=limit, offset=offset)
        return {"entries": [asdict(e) for e in entries],
                "summary": asdict(summarize(case))}

    @app.get("/api/tracks")
    def get_tracks():
        tracks = build_tracks(case) + rid_tracks(case)
        return json.loads(to_geojson(tracks, media_points(case),
                                     case.meta.case_number))

    @app.get("/api/flights")
    def get_flights():
        return {"flights": [asdict(f) for f in reconstruct(case)]}

    @app.get("/api/checks")
    def get_checks():
        return {"findings": [asdict(f) for f in run_checks(case)]}

    @app.get("/api/correlations")
    def get_correlations():
        return {"correlations": [asdict(c) for c in correlate_media(case)]}

    @app.get("/api/parsers")
    def get_parsers():
        return {"parsers": build_matrix(None)}

    @app.get("/api/entities")
    def get_entities():
        from .analysis.entities import build_entity_graph

        return build_entity_graph(case).as_dict()

    @app.get("/api/scenarios")
    def get_scenarios():
        from .analysis.scenarios import list_scenarios

        return {"scenarios": list_scenarios()}

    @app.get("/api/scenarios/{name}")
    def get_scenario(name: str):
        from .analysis.scenarios import evaluate_scenario

        try:
            return evaluate_scenario(case, name).as_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/audit")
    def get_audit(limit: int = 100):
        records = case.audit.records()
        return {"total": len(records),
                "records": records[-limit:] if limit else records}

    @app.get("/api/reports")
    def get_reports():
        reports_dir = case.root / "reports"
        out = []
        if reports_dir.is_dir():
            for f in sorted(reports_dir.iterdir()):
                if f.is_file():
                    out.append({"name": f.name, "size_bytes": f.stat().st_size})
        return {"reports": out}

    # ------------------------------------------------------------- mutations

    @app.post("/api/verify")
    def post_verify():
        result = case.verify(actor)  # audited by core
        return {"ok": result.ok, "problems": result.problems,
                "store": asdict(result.store), "audit": asdict(result.audit)}

    @app.post("/api/evidence")
    async def post_evidence(
        file: UploadFile = File(...),
        acknowledged: bool = Form(...),
        description: str = Form(None),
        acquisition_method: str = Form("logical"),
        data_origin: str = Form("original_raw"),
        source_device: str = Form(None),
        wb_make: str = Form(None),
        wb_model: str = Form(None),
        wb_serial: str = Form(None),
        wb_test_result: str = Form(None),
    ):
        if not acknowledged:
            raise HTTPException(
                status_code=400,
                detail={"error": "pre-action warnings must be acknowledged",
                        "warnings": PRE_ACTION_WARNINGS})
        write_blocker = None
        if any(v for v in (wb_make, wb_model, wb_serial, wb_test_result)):
            write_blocker = WriteBlockerInfo(make=wb_make, model=wb_model,
                                             serial=wb_serial,
                                             test_result=wb_test_result)
        with tempfile.TemporaryDirectory(prefix="droneforen-intake-") as tmp:
            dest = Path(tmp) / Path(file.filename or "upload.bin").name
            with open(dest, "wb") as fh:
                shutil.copyfileobj(file.file, fh)
            try:
                added = case.add_evidence(
                    dest, actor=actor, description=description,
                    acquisition_method=acquisition_method,
                    data_origin=DataOrigin(data_origin),
                    source_device=source_device, write_blocker=write_blocker)
            except DuplicateEvidenceError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (CaseError, EvidenceStoreError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            detected = detect_parsers(case.store.data_path(added.manifest.sha256))
        return JSONResponse(status_code=201, content={
            "sha256": added.manifest.sha256,
            "original_name": added.manifest.original_name,
            "size_bytes": added.manifest.size_bytes,
            "artifact_id": added.artifact_id,
            "detected_parsers": detected,
            "write_blocker_documented": write_blocker is not None,
        })

    @app.post("/api/parse")
    def post_parse(req: ParseRequest):
        try:
            sha256 = case.store.resolve_prefix(req.sha_prefix)
        except EvidenceStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with case.connect_db() as conn:
            row = conn.execute(
                "SELECT a.id FROM artifacts a JOIN evidence_items e "
                "ON a.evidence_id = e.id "
                "WHERE e.sha256 = ? AND a.parent_artifact_id IS NULL",
                (sha256,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no root artifact")
        parser_name = req.parser
        if parser_name is None:
            candidates = detect_parsers(case.store.data_path(sha256))
            if not candidates:
                raise HTTPException(status_code=422,
                                    detail="no parser detects this format")
            parser_name = candidates[0]
        try:
            outcome = run_parser(case, row["id"], parser_name, actor=actor,
                                 timeout=req.timeout)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return asdict(outcome)

    @app.post("/api/report")
    def post_report(req: ReportRequest):
        if req.format == "html":
            from .report import generate_html_report as generate
        elif req.format == "pdf":
            from .report_pdf import generate_pdf_report as generate
        else:
            raise HTTPException(status_code=422, detail="format must be html or pdf")
        result = generate(case, actor=actor)
        return {"name": result.path.name, "sha256": result.sha256}

    @app.post("/api/export")
    def post_export(req: ExportRequest):
        from .analysis.geospatial import EXPORTERS
        from .hashing import hash_file

        if req.format not in EXPORTERS:
            raise HTTPException(status_code=422,
                                detail=f"format must be one of {sorted(EXPORTERS)}")
        tracks = build_tracks(case) + rid_tracks(case)
        media = media_points(case)
        if not tracks and not media:
            raise HTTPException(status_code=422, detail="nothing to export")
        content = EXPORTERS[req.format](tracks, media, case.meta.case_number)
        out = case.root / "reports" / f"track.{req.format}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8", newline="\n")
        digest = hash_file(out).sha256
        case.audit.append(actor, "export.track", target=out.name,
                          params={"format": req.format, "sha256": digest,
                                  "tracks": len(tracks), "media_points": len(media)})
        return {"name": out.name, "sha256": digest}

    # ------------------------------------------------------- static frontend

    reports_dir = case.root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/files", StaticFiles(directory=str(reports_dir)), name="files")

    webui_dir = files("droneforen") / "webui"
    app.mount("/static", StaticFiles(directory=str(webui_dir)), name="static")

    # Asset URLs in index.html carry ?v=<version> so a tool upgrade busts any
    # cached CSS/JS; without it, users can see stale UI after updating.
    index_html = (webui_dir / "index.html").read_text(encoding="utf-8").replace(
        "__DRONEFOREN_VERSION__", __version__)

    @app.get("/", include_in_schema=False)
    def index():
        return HTMLResponse(index_html)

    return app

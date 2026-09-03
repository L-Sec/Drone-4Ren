# Architecture

This document describes the current design. [REQUIREMENTS.md](REQUIREMENTS.md) records
the broader project scope; [ROADMAP.md](ROADMAP.md) lists work that is not finished.

## 1. Deployment Model

The current deployment model is one user, one local process, and one workspace directory.

Rationale:

- Requirements section 15 mandates offline-first; forensic workstations are routinely
  air-gapped.
- A local, file-based format keeps imported files, metadata, activity records, and
  reports together for one analysis session.
- Multi-user access would require authentication, authorization, and a different threat
  model. The `actor` field is only a label; it is not an authentication control.

## 2. Technology Stack

The project uses a Python 3.12+ core library, a Typer CLI, and a FastAPI web interface.

- **Core (`droneforen`)**: case storage, audit records, parsers, analysis, and reports.
- **CLI**: Typer commands over the core library.
- **Web interface**: FastAPI serves a bundled HTML/CSS/JavaScript client on loopback.
- **Parsing and reports**: `pymavlink`, `pyulog`, `pydjirecord`, Pillow, Jinja2, and
  fpdf2. Pydantic validates application models; SQLite stores derived case data.
- **Packaging**: Hatchling builds the Python package and wheel.

## 3. Case Storage Layout

Each case is one directory with a content-addressed evidence store, a SQLite database,
and a hash-chained JSONL audit log.

```
CASE-2026-0042/
├── case.json              # workspace metadata and optional analysis bounds
├── evidence/              # ORIGINALS - read-only, never modified after intake
│   └── sha256/ab/abcd.../   #   content-addressed: path derived from file hash
│       ├── data           #   the original bytes (or E01/AFF4 container as imported)
│       └── manifest.json  #   sidecar: source device, acquisition method, hashes,
│                          #   source notes and timestamps
├── db/
│   └── case.sqlite        # ALL derived data: artifacts, events, entities, findings
├── audit/
│   └── audit.jsonl        # append-only, hash-chained (each record embeds the
│                          #   SHA-256 of the previous record) - tamper-evident
├── notes/                 # user notes
└── reports/               # generated outputs, each with its own hash manifest
```

Invariants:

1. **Nothing under `evidence/` is ever written twice.** Intake computes SHA-256 (and
   MD5 for legacy interoperability), stores the bytes, writes the manifest, and marks
   the tree read-only. A standard command rehashes stored files for verification.
2. **`case.sqlite` is disposable.** Every row in it is derived from imported files plus
   a recorded parser run; deleting the database and re-running the parsers should
   reproduce it.
3. **The audit log is append-only and hash-chained.** Every action - intake, parse,
   view, export, report, deletion - appends a record `{seq, timestamp_utc, actor,
   action, target, params, prev_sha256}`. Verification walks the chain.

## 4. Core Data Model

Entities (SQLite tables, pydantic models in code):

| Entity | Purpose |
|---|---|
| `Case` | Workspace number, description, and optional analysis bounds |
| `EvidenceItem` | One intaken source: SD image, log file, phone extraction, RID capture. Hashes, acquisition method, data-origin class (raw / manufacturer-exported / decrypted / cloud-derived / ...) |
| `CustodyEvent` | Import activity records; reserved for fuller transfer tracking later |
| `ParserRun` | Parser name + version + settings + input hash + start/end + outcome. Every derived row points at exactly one ParserRun |
| `Artifact` | A recognized structure inside evidence (a DJI TXT log, an app database, a photo) with offset/path within its EvidenceItem |
| `Event` | Normalized parser output used by analysis and reports |
| `Entity` | Devices, accounts, serial numbers, batteries, launch sites - nodes for link analysis |
| `Finding` | A calculated or user-recorded observation referencing supporting Events |
| `Annotation` | Analyst notes attached to anything, never mutating source-derived rows |
| `AuditRecord` | Mirror of the JSONL audit log for querying |

### The normalized Event schema

Every parser emits Events in one shape:

```
Event {
  id, artifact_id, parser_run_id,          # provenance - mandatory, non-null
  source_offset,                            # record offset / row id in the artifact
  ts_utc, ts_original, ts_source, clock_confidence,
  event_type,                               # controlled taxonomy: flight.takeoff,
                                            #   flight.rth, media.capture, link.loss,
                                            #   rid.observation, battery.warning, ...
  position (lat, lon, alt_msl, alt_agl, h_acc, v_acc),
  attitude (pitch, roll, yaw), velocity, heading,
  device_state (battery, satellites, gnss_quality, signal, flight_mode, ...),
  confidence,                               # per-event, set by parser rules
  payload_json                              # parser-specific fields, preserved verbatim
}
```

Design consequences:

- Timeline, map, correlation, tamper detection, and reporting are all consumers of
  the Event table - they need no format-specific knowledge.
- A raw value the schema doesn't model is never dropped; it goes to `payload_json`.
- UTC normalization keeps the original timestamp and timezone/clock source alongside;
  clock-drift detection compares `ts_source` populations.

## 5. Parser Plugin Framework

Parsers are discovered through Python entry points (`droneforen.parsers`). Each parser
declares a manifest:

```
ParserManifest {
  name, version,
  formats: [{vendor, artifact_type, detection: magic/extension/structure}],
  data_origin: raw | manufacturer_exported | decrypted | ...,
  confidence_notes, known_limitations,       # feeds the compatibility matrix
}
```

Contract: a parser receives the path to a stored evidence file and returns Events. It
does not receive the case database or audit log. The runner records the ParserRun and
validates returned events before inserting them.

Format detection is content-based (magic bytes / structural probe), never
extension-only - evidence files are routinely misnamed.

## 6. Analysis engines

- **Timeline**: filter/sort/merge across sources; conflict and clock-drift flags.
- **Geospatial**: track reconstruction, KML/GPX/GeoJSON/CSV export, plausibility
  checks (impossible speed, teleporting coordinates, altitude jumps).
- **Correlation**: match Remote ID observations ↔ aircraft logs ↔ media timestamps.
- **Integrity/tamper**: log-gap detection, truncation detection, sequence-number
  analysis, and cross-source timeline inconsistency flags.
- **Reporting**: Jinja2 → HTML → PDF, with mandatory reproducibility appendix
  (source hashes, parser versions/settings, output hashes) generated automatically
  from ParserRun records - not hand-assembled.

Feasibility modeling, link/attribution graphs, and RF flow analysis (requirements
sections 8, 11, 13) are later consumers of the same tables - see ROADMAP.md.

## 7. Security posture for untrusted evidence

Evidence must be treated as untrusted input. The current controls are:

- parsers run in a child process with a timeout;
- parser output is decoded as JSON and validated with Pydantic before database insert;
- evidence is copied into the case store and marked read-only after intake;
- the local web service accepts loopback hosts and rejects cross-origin mutations;
- uploaded filenames are reduced to their final path component before temporary use.

Process separation is not a security sandbox. The parser process currently inherits the
user's privileges and network access. Encrypted DJI decoding may contact DJI's
keychain service through `pydjirecord`. Operating-system isolation, parser memory limits,
and fuzz testing remain open work.

## 8. Validation

- `validation/` contains synthetic inputs and expected JSON output.
- CI compares current parser output with those expected files.
- The compatibility matrix is built from parser manifests and validation results.
- Field validation against known flights is required before stable support claims.

## 9. Licensing

The project uses Apache-2.0 and Developer Certificate of Origin sign-off.

## 10. Open design decisions

| Topic | Current position |
|---|---|
| Multi-user service | Not supported; requires authentication, authorization, and a new deployment review |
| RF/SDR and C-UAS input | Event model can represent observations, but formats and validation data are undecided |
| Mobile app artifacts | Prefer an iLEAPP/aLEAPP import path before adding vendor database parsers |
| Disk images and containers | Artifact model supports children; discovery and extraction controls are not implemented |
| Parser isolation | Child process and timeout today; OS sandbox and resource limits still need a design |

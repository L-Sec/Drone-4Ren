# Requirements

## Purpose

Drone 4Ren is a local tool for working with drone flight data. A user should be able to
import a file, identify its format, inspect parsed events, plot a flight, run basic
checks, and export a report.

## Basic workflow

1. Create a local workspace using a session number.
2. Import one or more sample files.
3. Record the file name, size, and hashes.
4. Detect or choose a parser.
5. Review parser status and known limitations.
6. Inspect the timeline, position data, and reconstructed flight.
7. Run basic plausibility and consistency checks.
8. Export a track or report.
9. Verify that stored file hashes and the activity log still match.

## Core functions

- Store imported files by SHA-256 without modifying the source file.
- Record SHA-256 and MD5 values, file size, import time, and original file name.
- Keep parser output separate from stored source data.
- Record parser name, version, input hash, status, and error text.
- Normalize common timestamps, positions, flight state, and device fields.
- Preserve format-specific values in an event payload.
- Display a merged timeline without treating unanchored timestamps as UTC.
- Build per-source tracks and simple takeoff, landing, RTH, and gap markers.
- Export KML, GPX, GeoJSON, and CSV.
- Generate HTML and PDF summaries.
- Rehash stored files and verify the activity-log chain.

## Parser coverage

The release should handle at least these inputs:

- MAVLink `.tlog`;
- ArduPilot `.BIN`;
- PX4 `.ulg`;
- DJI `.SRT`;
- DJI FlightRecord `.TXT` recognition and test decoding;
- JPEG EXIF/XMP;
- Remote ID JSON;
- plain text as a fallback example.

The compatibility matrix must state the parser version, support level, validated
fixtures, and known limitations. A parser passing synthetic tests does not imply support
for every aircraft or firmware version.

## Local operation and privacy

- Bind the web interface to loopback only.
- Do not require an account or cloud service for the basic local workflow.
- Do not use the Windows account name as the default activity label.
- Explain before any feature sends a network request.
- Keep real flight logs, credentials, case databases, and reports out of the repository.
- Do not include personal names or workstation paths in source, fixtures, or examples.

The optional OpenStreetMap view sends map-tile requests. Encrypted DJI decoding may
contact DJI's keychain service through `pydjirecord`. Both behaviors must be documented.

## Validation

- Run parser fixtures in CI on supported Python versions.
- Compare parser output with checked-in expected JSON.
- Test case creation, import, parsing, reports, exports, and verification.
- Test malformed and unsupported input paths where practical.
- Scan tracked files for credentials and local path disclosure before publication.

## Later work

The following items are outside the current release:

- authenticated multi-user access and role-based permissions;
- signed releases and reproducible-build attestations;
- full disk-image, mobile-app, cloud, RF, and counter-UAS acquisition workflows.

Some experimental fields and commands for these areas already exist. They are not part
of the supported local workflow and may change.

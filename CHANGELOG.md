# Changelog

## Unreleased (1.0.0b2.dev)

- Changed the displayed project name to Drone 4Ren. The package and command remain
  `droneforen`.
- Reworked the default workspace setup. Case creation now needs only a workspace
  number and uses non-identifying operator labels unless the user supplies others.
- Reworked the documentation and reports around the local workflow.
- Added loopback host checks, same-origin checks for write requests, no-store API
  responses, and browser security headers.
- Added ignore rules for local data and credentials, a security policy, and Dependabot
  configuration.
- Added optional `pydjirecord` telemetry decoding for DJI FlightRecord TXT files.
  Encrypted v13+ logs require `DJI_API_KEY`. Samples use relative flight time unless a
  reliable UTC source is available.
- Added scale, north arrow, grid, and labels to the local map.
- Added an optional OpenStreetMap basemap with a coordinate-disclosure warning and tile
  status messages.

## 1.0.0b1 - 2026-07-09

Initial beta:

- local workspace with hashed file copies, SQLite derived data, and a hash-chained
  activity log;
- MAVLink, ArduPilot, PX4, DJI SRT, image metadata, Remote ID JSON, and plain-text
  parsers;
- DJI FlightRecord TXT recognition;
- timeline, flight reconstruction, plausibility checks, source correlation, and track
  exports;
- HTML/PDF reports and ZIP bundles with hash manifests;
- command-line interface and loopback web interface;
- experimental readiness logs, entity links, scenario worksheets, scope checks, and
  endurance estimates.

The beta was tested with synthetic fixtures. It was not field-validated across aircraft
and firmware versions.

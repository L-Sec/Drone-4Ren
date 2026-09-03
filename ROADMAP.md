# Roadmap

This file records what is working, what still needs validation, and what is not yet
implemented. Release history belongs in [CHANGELOG.md](CHANGELOG.md).

## Current beta

The current code includes:

- case creation, evidence intake, hashing, and audit-chain verification;
- parsers for MAVLink, ArduPilot, PX4, DJI SRT/TXT, image metadata, Remote ID JSON,
  and readiness JSONL;
- timeline, track, correlation, plausibility, scope, and reconstruction tools;
- KML, GPX, GeoJSON, and CSV exports;
- HTML/PDF reports and evidence bundles;
- a loopback web interface;
- readiness logs, entity links, scenario worksheets, and endurance estimates.

The validation fixtures are synthetic. Passing the test suite shows that parser output
matches those fixtures; it does not establish support for every aircraft, firmware, or
application version.

## Before a stable release

1. Validate supported parsers against known flights and document the source, aircraft,
   firmware, and expected result for each sample.
2. Test DJI TXT decoding across pre-v13 and encrypted v13+ records. Record when the
   decoder contacts DJI's keychain service and what data leaves the workstation.
3. Add malformed-input and resource-limit tests for every binary parser.
4. Reconcile the parser sandbox documentation with its actual controls. The current
   runner provides process separation and a timeout; it is not an operating-system
   security sandbox.
5. Test installation and the local interface on supported Windows and Linux versions.
6. Review report wording and remove conclusions that are not directly supported by
   stored events.
7. Publish a versioned compatibility matrix with the release.

## Open implementation work

- artifact discovery inside disk images and application exports;
- import of iLEAPP/aLEAPP drone-app results;
- DJI DAT and raw Remote ID capture support where a validated decoding route exists;
- richer time/area filters and redaction workflows;
- firmware and configuration comparison beyond version-string extraction;
- RF/SDR and counter-UAS sensor imports;
- multi-user service mode, authentication, and role-based access;
- packaged sample files and operator notes.

Items in this section are not promised for a particular release. Work should be ordered
by user need and availability of shareable validation data.

## Stable-release criteria

A 1.0 release should have:

- no known high-severity dependency or application vulnerabilities;
- documented network behavior and deployment limits;
- field-checked parser results for every format marked supported;
- reproducible release builds and a clean install test;
- passing tests on the supported Python and operating-system matrix;
- an operator guide that distinguishes parsed values from calculated results.

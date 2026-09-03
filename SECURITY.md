# Security Policy

## Supported versions

Drone 4Ren is beta software. Security fixes are applied to the latest code on the
`main` branch and the newest published beta release. Older development snapshots are
not supported.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository's
**Security** tab and **Report a vulnerability** to submit a private GitHub Security
Advisory. Include the affected version, reproduction steps, impact, and any suggested
mitigation. Please avoid including real evidence or personally identifying case data.

If private vulnerability reporting is not enabled on the repository, open a public
issue containing only a request for a private security contact. Do not include exploit
details in that issue.

## Deployment boundary

The bundled web service is a local, single-examiner interface. It must bind only to a
loopback address and must not be exposed to a network. It does not provide user
authentication or multi-user isolation.

## Evidence and secrets

Real flight logs, case directories, reports, databases, API keys, and examiner or
subject identifiers must not be committed to this source repository. Use synthetic
fixtures for tests. Supply optional decoder credentials through the process environment
and rotate any credential that may have entered Git history.

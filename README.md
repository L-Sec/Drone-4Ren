# Drone 4Ren

Drone 4Ren is a local tool for importing drone flight data, recording hashes, parsing
supported formats, building timelines and tracks, and exporting reports.

The Python package and command are named `droneforen`.

## Features

- content-addressed file storage with SHA-256 and MD5 hashes;
- a hash-chained activity log and workspace verification command;
- flight timeline, track reconstruction, plausibility checks, and media correlation;
- KML, GPX, GeoJSON, and CSV exports;
- HTML and PDF reports;
- a local web interface bound to `127.0.0.1`;
- optional scope checks, link analysis, and endurance estimates.

## Supported input

| Parser | Formats | Notes |
|---|---|---|
| `mavlink-tlog` | MAVLink `.tlog` | GCS telemetry with wall-clock timestamps |
| `ardupilot-bin` | ArduPilot `.BIN` | Dataflash logs with GPS-derived UTC |
| `px4-ulog` | PX4 `.ulg` | ULog with UTC recovered from GPS data |
| `dji-srt` | DJI `.SRT` | Bracket and legacy subtitle telemetry |
| `dji-txt` | DJI `.TXT` | Recognition and test-build decoding through `pydjirecord`; v13+ requires `DJI_API_KEY` |
| `image-exif` | JPEG | EXIF GPS/timestamps and DJI XMP fields |
| `remoteid-json` | RID JSON | ASTM F3411 observation exports |
| `plaintext` | Text | Generic fallback |

Run `droneforen parsers matrix` for parser versions, fixture coverage, and format notes.
Expected fixture output is stored under [validation/](validation/).

## Install

Python 3.12 or newer is required. This release is tested on Python 3.12 and 3.13.

Download and extract the repository, or clone it with Git. Open a terminal in the
project directory, the folder containing `pyproject.toml`.

### Windows PowerShell

List the Python versions registered with the Windows launcher:

```powershell
py --list
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\droneforen.exe version
```

Use `py -3.13` instead if Python 3.13 is installed. If `py` is unavailable but
`python --version` reports Python 3.12 or 3.13, replace `py -3.12` with `python`.
Otherwise, install a supported Python version and the Windows launcher before
continuing. Do not create the environment with Python 3.11 or older.

The last command should print `droneforen 1.0.0b2.dev2`. Using the executable's full
path avoids PowerShell activation-policy problems.

### Kali Linux and other Debian-family distributions

Install the operating-system prerequisites:

```bash
sudo apt update
sudo apt install -y git python3-full python3-venv
python3 --version
```

Confirm that `python3 --version` reports Python 3.12 or 3.13. From the project
directory, run:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/droneforen version
```

The last command should print `droneforen 1.0.0b2.dev2`.

Do not run `sudo pip install`, install into the system Python, or use
`--break-system-packages`. Kali marks its system Python as externally managed. The
`.venv` directory keeps Drone 4Ren and its dependencies separate from Kali packages.

Common installation errors:

- `No module named venv`: install `python3-full` and `python3-venv`, then recreate
  `.venv`.
- `externally-managed-environment`: run pip through
  `./.venv/bin/python -m pip`, not through the system Python.
- `Requires-Python >=3.12`: the installed Python version is too old. Install a
  distribution-supported Python 3.12 or 3.13 interpreter before continuing.

### Other Linux distributions and macOS

Install Python 3.12 or 3.13, its `venv` support, and Git through the operating
system's package manager. Then run these commands from the project directory:

```bash
python3 --version
python3 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/droneforen version
```

### Contributor install

Install the project in editable mode with the test and lint dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

On Linux or macOS, replace `.\.venv\Scripts\python.exe` with
`./.venv/bin/python`.

## Basic use

The examples below use `droneforen`. If the virtual environment is not activated,
use `.\.venv\Scripts\droneforen.exe` on Windows or `./.venv/bin/droneforen` on
Linux and macOS.

```console
$ droneforen case create ./FLIGHT-01 --case-number FLIGHT-01
$ droneforen evidence add ./FLIGHT-01 ./flight_log.txt \
      --description "SD card root, flight log" --acquisition-method logical
$ droneforen evidence list ./FLIGHT-01
$ droneforen evidence parse ./FLIGHT-01 <sha256-prefix>
$ droneforen analyze timeline ./FLIGHT-01
$ droneforen analyze flight ./FLIGHT-01
$ droneforen analyze checks ./FLIGHT-01
$ droneforen export track ./FLIGHT-01 --format kml
$ droneforen report html ./FLIGHT-01
$ droneforen case verify ./FLIGHT-01
```

Start the local interface with:

```console
$ droneforen serve ./FLIGHT-01
```

## Case layout

```text
FLIGHT-01/
|-- case.json          # case metadata and scope
|-- evidence/          # content-addressed evidence copies
|-- db/case.sqlite     # parsed and derived data
|-- audit/audit.jsonl  # hash-chained activity log
|-- notes/             # user notes
`-- reports/           # generated reports and exports
```

## Network behavior

The application binds its web interface to loopback and does not require a network for
normal local use. Two features can make outbound requests:

- the optional street basemap fetches tiles from OpenStreetMap after confirmation;
- encrypted DJI decoding may use `pydjirecord` to contact DJI's keychain service when
  `DJI_API_KEY` is set.

Do not expose the web service to a LAN or the public internet. It is a single-user
interface and has no account system.

## Project documents

- [REQUIREMENTS.md](REQUIREMENTS.md): scope and requirements
- [ARCHITECTURE.md](ARCHITECTURE.md): storage, data model, and design decisions
- [ROADMAP.md](ROADMAP.md): completed work and open items
- [SECURITY.md](SECURITY.md): vulnerability reporting and deployment limits

## Development checks

```console
$ pytest
$ ruff check .
```

## Sensitive data

Keep working data outside the source tree or in one of the ignored local data
directories. Do not commit imported flight logs, databases, generated reports,
credentials, or personal information. Set `DJI_API_KEY` in the process environment
rather than in a repository file.

## License

Apache-2.0. Contributions use Developer Certificate of Origin sign-off.

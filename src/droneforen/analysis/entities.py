"""Entity extraction and link graph.

Derives typed entities from source events - identifiers, devices, accounts,
positions, sites - and links them by co-occurrence. The graph produces
*investigative leads*, and every node carries an explicit attribution level:

- ``device``   - identifies a piece of hardware (serial, MAC, Remote ID UAS id)
- ``account``  - identifies a registered/declared identity (operator id)
- ``operator`` - bears on where a person was (operator position broadcasts)
- ``location`` - a place the aircraft used (launch/landing sites)

Nothing here reaches the ``owner`` level: no output of this module establishes
who owned or who was flying the aircraft. That caveat is embedded in the graph
itself so it survives into reports and API responses.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..case import CaseWorkspace
from .geospatial import build_tracks, haversine_m, rid_tracks
from .timeline import load_timeline

ATTRIBUTION_CAVEAT = (
    "Entities are investigative leads with device/account/operator/location "
    "attribution levels. None of them establishes ownership or who piloted the "
    "aircraft; identity attribution requires corroborating evidence outside "
    "this tool."
)

SAME_SITE_RADIUS_M = 30.0
_SERIAL_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{8,23}$")
_FIRMWARE_RE = re.compile(
    r"\b(ArduCopter|ArduPlane|ArduRover|PX4|Betaflight|INAV)\s+V?([\w.\-]+)",
    re.IGNORECASE,
)


@dataclass
class EntityNode:
    id: str                     # stable slug: "<type>:<value>"
    type: str                   # uas_id | serial_candidate | mac | operator_id | ...
    label: str
    attribution_level: str      # device | account | operator | location
    observations: int = 0
    evidence: dict[str, str] = field(default_factory=dict)  # sha256 -> name
    note: str | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass
class EntityEdge:
    a: str
    b: str
    relation: str               # observed_together | same_site
    detail: str | None = None


@dataclass
class EntityGraph:
    nodes: list[EntityNode]
    edges: list[EntityEdge]
    caveat: str = ATTRIBUTION_CAVEAT

    def as_dict(self) -> dict:
        return {"nodes": [asdict(n) for n in self.nodes],
                "edges": [asdict(e) for e in self.edges],
                "caveat": self.caveat}


class _Builder:
    def __init__(self):
        self.nodes: dict[str, EntityNode] = {}

    def touch(self, type_: str, value: str, attribution: str, evidence_sha: str,
              evidence_name: str, note: str | None = None,
              lat: float | None = None, lon: float | None = None) -> EntityNode:
        node_id = f"{type_}:{value}"
        node = self.nodes.get(node_id)
        if node is None:
            node = EntityNode(id=node_id, type=type_, label=value,
                              attribution_level=attribution, note=note,
                              lat=lat, lon=lon)
            self.nodes[node_id] = node
        node.observations += 1
        node.evidence[evidence_sha] = evidence_name
        return node


def build_entity_graph(case: CaseWorkspace) -> EntityGraph:
    b = _Builder()

    for e in load_timeline(case):
        sha, name = e.evidence_sha256, e.evidence_name
        p = e.payload

        if e.event_type == "rid.observation":
            if p.get("uas_id"):
                b.touch("uas_id", str(p["uas_id"]), "device", sha, name,
                        note="Remote ID broadcast id: unauthenticated, spoofable")
            if p.get("operator_id"):
                b.touch("operator_id", str(p["operator_id"]), "account", sha, name,
                        note="operator-declared Remote ID; account-level only")
            if p.get("mac"):
                b.touch("mac", str(p["mac"]), "device", sha, name,
                        note="broadcast MAC; may be randomized per session")
            if p.get("operator_lat") is not None and p.get("operator_lon") is not None:
                b.touch("operator_position",
                        f"{p['operator_lat']:.5f},{p['operator_lon']:.5f}",
                        "operator", sha, name,
                        note="broadcast operator/take-off position",
                        lat=float(p["operator_lat"]), lon=float(p["operator_lon"]))

        elif e.event_type == "artifact.recognized":
            for s in p.get("details_strings", []):
                if _SERIAL_RE.match(s) and any(c.isdigit() for c in s):
                    b.touch("serial_candidate", s, "device", sha, name,
                            note="string extracted from log details block; "
                                 "unverified candidate serial")

        elif e.event_type == "media.capture":
            make, model = p.get("make"), p.get("model")
            if make or model:
                b.touch("camera", f"{make or '?'} {model or '?'}".strip(), "device",
                        sha, name,
                        note="camera make/model identifies a device class, "
                             "not an individual unit")
            xmp = p.get("xmp_dji") or {}
            for key in ("SerialNumber", "DroneSerialNumber", "CameraSerialNumber"):
                if xmp.get(key):
                    b.touch("serial_candidate", str(xmp[key]), "device", sha, name,
                            note=f"from XMP {key}")

        elif e.event_type == "log.message":
            text = str(p.get("text", ""))
            m = _FIRMWARE_RE.search(text)
            if m:
                b.touch("firmware", f"{m.group(1)} {m.group(2)}", "device", sha, name,
                        note="firmware string from vehicle log messages")

    # Launch / landing sites from position tracks (flight logs and RID).
    for track in build_tracks(case) + rid_tracks(case):
        if not track.points:
            continue
        first, last = track.points[0], track.points[-1]
        b.touch("launch_site", f"{first.lat:.5f},{first.lon:.5f}", "location",
                track.evidence_sha256, track.evidence_name,
                note="first recorded fix in this source", lat=first.lat, lon=first.lon)
        b.touch("landing_site", f"{last.lat:.5f},{last.lon:.5f}", "location",
                track.evidence_sha256, track.evidence_name,
                note="last recorded fix in this source", lat=last.lat, lon=last.lon)

    nodes = sorted(b.nodes.values(), key=lambda n: (n.type, n.label))

    edges: list[EntityEdge] = []
    # Same-site: location nodes from different evidence within radius.
    sites = [n for n in nodes if n.attribution_level == "location"]
    for i, na in enumerate(sites):
        for nb in sites[i + 1:]:
            if na.id == nb.id or set(na.evidence) & set(nb.evidence):
                continue
            if na.lat is None or nb.lat is None:
                continue
            dist = haversine_m(na.lat, na.lon, nb.lat, nb.lon)
            if dist <= SAME_SITE_RADIUS_M:
                edges.append(EntityEdge(
                    a=na.id, b=nb.id, relation="same_site",
                    detail=f"{dist:.0f} m apart across different evidence sources"))
    # Co-occurrence: identifier entities sharing an evidence item.
    identifiers = [n for n in nodes if n.attribution_level in ("device", "account",
                                                               "operator")]
    for i, na in enumerate(identifiers):
        for nb in identifiers[i + 1:]:
            shared = set(na.evidence) & set(nb.evidence)
            if shared:
                edges.append(EntityEdge(
                    a=na.id, b=nb.id, relation="observed_together",
                    detail=f"co-occur in {len(shared)} evidence item(s)"))

    return EntityGraph(nodes=nodes, edges=edges)

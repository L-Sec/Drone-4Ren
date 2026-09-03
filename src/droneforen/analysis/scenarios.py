"""Structured worksheets for common UAS scenarios.

Signals are evaluated from the event table where a simple rule exists and marked
``manual`` where user judgment or outside data is required.

A scenario evaluation is a *worksheet*, not a classification: "3 of 4 flyaway
signals present" never becomes "this was a flyaway"."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..case import CaseWorkspace
from .geospatial import build_tracks, haversine_m, parse_iso
from .reconstruction import reconstruct
from .timeline import load_timeline

FINAL_DESCENT_ALERT_MS = 5.0
FAR_FROM_LAUNCH_M = 200.0
LOITER_SPEED_MS = 1.0
LOITER_FRACTION = 0.3


@dataclass
class SignalResult:
    name: str
    status: str            # present | absent | manual
    detail: str


@dataclass
class ScenarioResult:
    scenario: str
    description: str
    signals: list[SignalResult] = field(default_factory=list)
    caveat: str = ("A scenario worksheet organizes evidence review; signal counts "
                   "are not conclusions and absence of a signal is not exoneration.")

    def as_dict(self) -> dict:
        return asdict(self)


class _Ctx:
    """Shared, lazily computed analysis context for signal evaluators."""

    def __init__(self, case: CaseWorkspace):
        self.case = case
        self.tracks = [t for t in build_tracks(case) if t.points]
        self.flights = reconstruct(case)
        self.media = load_timeline(case, event_type="media.capture")
        self.rid = load_timeline(case, event_type="rid.observation")

    def item_kinds(self) -> set[str]:
        return {i.kind for f in self.flights for i in f.items}


def _sig_ends_without_disarm(ctx: _Ctx) -> SignalResult:
    kinds = ctx.item_kinds()
    if not ctx.flights:
        return SignalResult("log ends without disarm", "manual",
                            "no flight telemetry sources to evaluate")
    if "armed" in kinds and "disarmed" not in kinds:
        return SignalResult("log ends without disarm", "present",
                            "an arm event was recorded but no disarm event - "
                            "consistent with power loss, crash, or log truncation")
    return SignalResult("log ends without disarm", "absent",
                        "disarm events present (or no arming recorded at all)")


def _sig_final_descent(ctx: _Ctx) -> SignalResult:
    name = "high final descent rate"
    worst = None
    for track in ctx.tracks:
        if len(track.points) < 2:
            continue
        a, b = track.points[-2], track.points[-1]
        ta, tb = parse_iso(a.ts_utc), parse_iso(b.ts_utc)
        alt_a = a.alt_msl if a.alt_msl is not None else a.alt_agl
        alt_b = b.alt_msl if b.alt_msl is not None else b.alt_agl
        if not ta or not tb or alt_a is None or alt_b is None:
            continue
        dt = (tb - ta).total_seconds()
        if dt <= 0:
            continue
        rate = (alt_a - alt_b) / dt
        if worst is None or rate > worst[0]:
            worst = (rate, track.evidence_name)
    if worst is None:
        return SignalResult(name, "manual", "no timed altitude data at end of track")
    rate, source = worst
    if rate >= FINAL_DESCENT_ALERT_MS:
        return SignalResult(name, "present",
                            f"{source}: {rate:.1f} m/s descent between final fixes")
    return SignalResult(name, "absent",
                        f"max final descent {rate:.1f} m/s (< {FINAL_DESCENT_ALERT_MS})")


def _sig_rth(ctx: _Ctx) -> SignalResult:
    if "rth" in ctx.item_kinds():
        return SignalResult("return-to-home engaged", "present",
                            "RTL/RTH mode change recorded")
    return SignalResult("return-to-home engaged", "absent", "no RTH mode change found")


def _sig_signal_gaps(ctx: _Ctx) -> SignalResult:
    gaps = [i for f in ctx.flights for i in f.items if i.kind == "signal_gap"]
    if gaps:
        return SignalResult("link/recording gaps", "present",
                            f"{len(gaps)} position gap(s) derived; see reconstruction")
    return SignalResult("link/recording gaps", "absent", "no position gaps >= 5 s")


def _sig_far_from_launch(ctx: _Ctx) -> SignalResult:
    name = "track ends far from launch"
    worst = None
    for track in ctx.tracks:
        first, last = track.points[0], track.points[-1]
        dist = haversine_m(first.lat, first.lon, last.lat, last.lon)
        if worst is None or dist > worst[0]:
            worst = (dist, track.evidence_name)
    if worst is None:
        return SignalResult(name, "manual", "no position tracks")
    dist, source = worst
    if dist >= FAR_FROM_LAUNCH_M:
        return SignalResult(name, "present", f"{source}: ends {dist:.0f} m from launch")
    return SignalResult(name, "absent", f"ends {dist:.0f} m from launch "
                                        f"(< {FAR_FROM_LAUNCH_M:.0f} m)")


def _sig_loiter(ctx: _Ctx) -> SignalResult:
    name = "sustained hover/loiter"
    best = None
    for track in ctx.tracks:
        speeds = [p.speed for p in track.points if p.speed is not None]
        if len(speeds) < 4:
            continue
        frac = sum(1 for s in speeds if s <= LOITER_SPEED_MS) / len(speeds)
        if best is None or frac > best[0]:
            best = (frac, track.evidence_name)
    if best is None:
        return SignalResult(name, "manual", "insufficient speed data")
    frac, source = best
    if frac >= LOITER_FRACTION:
        return SignalResult(name, "present",
                            f"{source}: {frac:.0%} of fixes at <= {LOITER_SPEED_MS} m/s")
    return SignalResult(name, "absent", f"max loiter fraction {frac:.0%}")


def _sig_media_captured(ctx: _Ctx) -> SignalResult:
    n = len(ctx.media)
    if n:
        return SignalResult("media captured during activity", "present",
                            f"{n} media capture event(s) in the case")
    return SignalResult("media captured during activity", "absent",
                        "no media capture events parsed")


def _sig_rid_at_scene(ctx: _Ctx) -> SignalResult:
    if ctx.rid:
        ids = {str(e.payload.get("uas_id")) for e in ctx.rid if e.payload.get("uas_id")}
        return SignalResult("Remote ID observed at scene", "present",
                            f"{len(ctx.rid)} observation(s), UAS ids: "
                            f"{', '.join(sorted(ids)) or 'none decoded'}")
    return SignalResult("Remote ID observed at scene", "absent",
                        "no Remote ID observations imported")


def _manual(name: str, guidance: str):
    def evaluator(_ctx: _Ctx) -> SignalResult:
        return SignalResult(name, "manual", guidance)
    return evaluator


SCENARIOS: dict[str, dict] = {
    "crash": {
        "description": "Uncontrolled descent / impact investigation",
        "signals": [
            _sig_ends_without_disarm,
            _sig_final_descent,
            _sig_signal_gaps,
            _manual("physical impact evidence",
                    "document propeller strikes, frame damage, soil/paint transfer "
                    "before further handling (REQUIREMENTS §5 order of operations)"),
            _manual("battery state at recovery",
                    "record voltage, damage, and cycle data; compare with last "
                    "battery.status events"),
        ],
    },
    "flyaway": {
        "description": "Loss-of-control / lost-link departure",
        "signals": [
            _sig_signal_gaps,
            _sig_rth,
            _sig_far_from_launch,
            _manual("wind and interference conditions",
                    "obtain weather and RF environment data for the flight window"),
            _manual("geofence and unlock records",
                    "check vendor account for geofence overrides or unlock licenses"),
        ],
    },
    "surveillance": {
        "description": "Persistent observation of a location",
        "signals": [
            _sig_loiter,
            _sig_media_captured,
            _sig_rid_at_scene,
            _manual("camera orientation toward area of concern",
                    "use gimbal pitch/heading and media correlation to assess what "
                    "was observable; camera-footprint analysis is a later phase"),
            _manual("repeat visits over time",
                    "compare launch/landing site entities across multiple cases or "
                    "log files"),
        ],
    },
    "intrusion": {
        "description": "Unauthorized presence over a protected site",
        "signals": [
            _sig_rid_at_scene,
            _sig_signal_gaps,
            _sig_media_captured,
            _manual("track versus analysis boundary",
                    "overlay exported KML/GeoJSON on the analysis boundary"),
            _manual("time-of-day and lighting",
                    "convert UTC times to site-local time; note lighting conditions"),
        ],
    },
}


def list_scenarios() -> list[dict]:
    return [{"scenario": name, "description": spec["description"],
             "signal_count": len(spec["signals"])}
            for name, spec in SCENARIOS.items()]


def evaluate_scenario(case: CaseWorkspace, name: str) -> ScenarioResult:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; available: "
                       f"{', '.join(sorted(SCENARIOS))}")
    spec = SCENARIOS[name]
    ctx = _Ctx(case)
    return ScenarioResult(
        scenario=name,
        description=spec["description"],
        signals=[evaluator(ctx) for evaluator in spec["signals"]],
    )

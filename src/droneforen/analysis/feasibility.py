"""Flight-endurance feasibility model.

Estimates whether an observed flight duration is achievable for a multirotor
with the given mass and battery, using ideal-rotor momentum theory:

    hover power  P = T^1.5 / (FoM * sqrt(2 * rho * A))
    with thrust T = m*g and total disk area A = rotors * pi * (d/2)^2

Real figures of merit, battery health, and maneuvering overhead are uncertain,
so the model is evaluated over parameter *ranges* and reports an endurance
interval. The output includes its assumptions; "exceeds model" is a reason to
re-examine inputs and evidence, not proof of tampering.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from ..case import CaseWorkspace
from .reconstruction import reconstruct

G = 9.80665
AIR_DENSITY_SEA_LEVEL = 1.225

# Conservative-to-optimistic parameter ranges for small multirotors.
FIGURE_OF_MERIT_RANGE = (0.45, 0.70)     # rotor+ESC+motor system efficiency
USABLE_BATTERY_RANGE = (0.65, 0.85)      # usable fraction of rated capacity
OVERHEAD_POWER_W_RANGE = (5.0, 20.0)     # avionics, video link, camera
MANEUVER_FACTOR_RANGE = (1.0, 1.35)      # cruise/climb/wind overhead vs hover

MODEL_CAVEAT = (
    "Endurance interval from ideal-rotor momentum theory over stated parameter "
    "ranges. It is a screening model: an observed duration outside the interval "
    "means the inputs (mass, battery, aircraft identity) or the evidence deserve "
    "re-examination - it is not, by itself, proof of tampering or of a different "
    "aircraft."
)


@dataclass
class FeasibilityInputs:
    mass_kg: float
    battery_wh: float
    rotors: int = 4
    rotor_diameter_m: float = 0.24
    air_density: float = AIR_DENSITY_SEA_LEVEL


@dataclass
class FeasibilityResult:
    inputs: FeasibilityInputs
    hover_power_w_best: float
    hover_power_w_worst: float
    endurance_s_min: float
    endurance_s_max: float
    observed_duration_s: float | None
    observed_source: str | None
    verdict: str          # within_model | marginal | exceeds_model | no_observation
    assumptions: list[str] = field(default_factory=list)
    caveat: str = MODEL_CAVEAT

    def as_dict(self) -> dict:
        return asdict(self)


def _hover_power_w(inputs: FeasibilityInputs, figure_of_merit: float) -> float:
    thrust_n = inputs.mass_kg * G
    disk_area = inputs.rotors * math.pi * (inputs.rotor_diameter_m / 2) ** 2
    return thrust_n ** 1.5 / (figure_of_merit
                              * math.sqrt(2 * inputs.air_density * disk_area))


def estimate_endurance(inputs: FeasibilityInputs) -> tuple[float, float, float, float]:
    """Return (best hover W, worst hover W, min endurance s, max endurance s)."""
    if inputs.mass_kg <= 0 or inputs.battery_wh <= 0 or inputs.rotors <= 0 \
            or inputs.rotor_diameter_m <= 0:
        raise ValueError("mass, battery, rotors and rotor diameter must be positive")
    p_best = _hover_power_w(inputs, FIGURE_OF_MERIT_RANGE[1])
    p_worst = _hover_power_w(inputs, FIGURE_OF_MERIT_RANGE[0])
    energy_min = inputs.battery_wh * USABLE_BATTERY_RANGE[0] * 3600.0
    energy_max = inputs.battery_wh * USABLE_BATTERY_RANGE[1] * 3600.0
    draw_max = p_worst * MANEUVER_FACTOR_RANGE[1] + OVERHEAD_POWER_W_RANGE[1]
    draw_min = p_best * MANEUVER_FACTOR_RANGE[0] + OVERHEAD_POWER_W_RANGE[0]
    return p_best, p_worst, energy_min / draw_max, energy_max / draw_min


def assess_flight_feasibility(
    case: CaseWorkspace | None,
    inputs: FeasibilityInputs,
    observed_duration_s: float | None = None,
) -> FeasibilityResult:
    """Compare modeled endurance against the longest reconstructed track
    duration in the case (or an explicitly supplied duration)."""
    source = None
    if observed_duration_s is None and case is not None:
        longest = None
        for flight in reconstruct(case):
            if flight.duration_s is not None and (
                    longest is None or flight.duration_s > longest[0]):
                longest = (flight.duration_s, flight.evidence_name)
        if longest:
            observed_duration_s, source = longest
    elif observed_duration_s is not None:
        source = "supplied by user"

    p_best, p_worst, e_min, e_max = estimate_endurance(inputs)

    if observed_duration_s is None:
        verdict = "no_observation"
    elif observed_duration_s <= e_min:
        verdict = "within_model"
    elif observed_duration_s <= e_max * 1.1:
        verdict = "marginal"
    else:
        verdict = "exceeds_model"

    assumptions = [
        f"all-up mass {inputs.mass_kg} kg (must include payload and battery)",
        f"battery energy {inputs.battery_wh} Wh at rated capacity; usable fraction "
        f"{USABLE_BATTERY_RANGE[0]:.0%}-{USABLE_BATTERY_RANGE[1]:.0%} "
        "(health/temperature unknown)",
        f"{inputs.rotors} rotors of {inputs.rotor_diameter_m} m diameter; "
        f"system figure of merit {FIGURE_OF_MERIT_RANGE[0]}-{FIGURE_OF_MERIT_RANGE[1]}",
        f"maneuvering/wind overhead {MANEUVER_FACTOR_RANGE[0]}-"
        f"{MANEUVER_FACTOR_RANGE[1]}x hover power; avionics "
        f"{OVERHEAD_POWER_W_RANGE[0]}-{OVERHEAD_POWER_W_RANGE[1]} W",
        f"air density {inputs.air_density} kg/m^3 (sea-level standard unless set)",
        "observed duration is track time between first and last fix, which may "
        "understate total powered time",
    ]
    return FeasibilityResult(
        inputs=inputs,
        hover_power_w_best=round(p_best, 1),
        hover_power_w_worst=round(p_worst, 1),
        endurance_s_min=round(e_min, 1),
        endurance_s_max=round(e_max, 1),
        observed_duration_s=observed_duration_s,
        observed_source=source,
        verdict=verdict,
        assumptions=assumptions,
    )

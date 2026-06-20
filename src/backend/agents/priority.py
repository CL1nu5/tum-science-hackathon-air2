from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import SAFETY_RESERVE_PCT
from .state import AgentState, AgentStatus, SlotStage, HARD_LOCK_SLOT_STAGES

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Constraint metric
# ---------------------------------------------------------------------------

def compute_priority_metric(
    battery_pct: float,
    reachable_options: int,
    max_options: int = 10,
) -> float:
    """
    Returns [0, 1] — higher means more constrained → higher priority.
    Drives all preemption ordering (concept §4).
    """
    battery_factor = 1.0 - (battery_pct / 100.0)
    options_factor = 1.0 - (min(reachable_options, max_options) / max_options)
    return 0.6 * battery_factor + 0.4 * options_factor


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------

@dataclass
class VertiportInfo:
    vertiport_id: str
    distance_m: float          # straight-line from agent position (recomputed per tick)
    has_free_slot: bool
    is_own_operator: bool
    surface_type: str          # "vertiport" | "light_red" | "dark_red" | "trailing"
    x: float = 0.0             # projected position (metres east of reference)
    y: float = 0.0             # projected position (metres north of reference)
    name: str = ""


def energy_to_reach(distance_m: float, speed_ms: float, drain_per_s: float) -> float:
    """Estimate battery % consumed flying distance_m at speed_ms."""
    flight_time_s = distance_m / max(speed_ms, 1.0)
    return flight_time_s * drain_per_s


def is_reachable(
    battery_pct: float,
    distance_m: float,
    speed_ms: float,
    drain_per_s: float,
) -> bool:
    """True if battery covers the trip plus SAFETY_RESERVE_PCT."""
    cost = energy_to_reach(distance_m, speed_ms, drain_per_s)
    return (battery_pct - cost) >= SAFETY_RESERVE_PCT


def filter_reachable(
    battery_pct: float,
    speed_ms: float,
    drain_per_s: float,
    candidates: list[VertiportInfo],
) -> list[VertiportInfo]:
    return [
        v for v in candidates
        if is_reachable(battery_pct, v.distance_m, speed_ms, drain_per_s)
    ]


# ---------------------------------------------------------------------------
# Preemption guard  (concept §5)
# ---------------------------------------------------------------------------

def can_preempt(
    attacker: AgentState,
    victim: AgentState,
    victim_reachable_after: int,
) -> bool:
    """
    Returns True iff attacker is allowed to take victim's slot.
    Concept invariants enforced:
      - attacker must be EMERGENCY
      - victim must NOT be in a hard-lock slot stage
      - victim must still have ≥1 reachable option after losing its slot
      - attacker must be more constrained (higher metric)
    """
    if attacker.status != AgentStatus.EMERGENCY:
        return False
    if victim.slot_stage in HARD_LOCK_SLOT_STAGES:
        return False
    if victim_reachable_after < 1:
        return False
    if attacker.priority_metric <= victim.priority_metric:
        return False
    return True


# ---------------------------------------------------------------------------
# Emergency landing cascade  (concept §5)
# ---------------------------------------------------------------------------

@dataclass
class LandingOption:
    vertiport_id: str
    surface_type: str   # "vertiport" | "light_red" | "dark_red" | "trailing"
    requires_preemption: bool
    preempt_victim_id: str | None
    distance_m: float
    suitability_score: float   # 0-1, higher is better; used for tie-breaking trailing spots


def find_emergency_landing(
    state: AgentState,
    candidates: list[VertiportInfo],
    *,
    occupied_slots: dict[str, str],   # vertiport_id → occupying agent_id
    agent_states: dict[str, AgentState],
) -> LandingOption | None:
    """
    Runs the emergency landing cascade (concept §5 air-side escalation).
    Returns the best LandingOption, or None if human takeover is required.

    Cascade order (stops at first found):
      1. Own destination slot (still reachable)
      2. Slot-in to gap (non-destructive, free window at any vertiport)
      3. Free slot at any reachable vertiport
      4. Preempt a lower-priority slot
      5. Other operator's vertiport (reachable)
      6. Light-red prepared surface
      7. Dark-red open field
      8. Trailing spot (lowest suitability)
      9. None → human
    """
    from .config import BATTERY_DRAIN_PER_S, CRUISE_SPEED_MS

    reachable = filter_reachable(
        state.battery_pct, state.speed or CRUISE_SPEED_MS, BATTERY_DRAIN_PER_S, candidates
    )
    reachable_ids = {v.vertiport_id for v in reachable}

    # --- 1. Own destination ---
    if state.destination_vertiport and state.destination_vertiport in reachable_ids:
        own = next(v for v in reachable if v.vertiport_id == state.destination_vertiport)
        return LandingOption(
            vertiport_id=own.vertiport_id,
            surface_type=own.surface_type,
            requires_preemption=False,
            preempt_victim_id=None,
            distance_m=own.distance_m,
            suitability_score=1.0,
        )

    # --- 2 & 3. Slot-in or free slot at any reachable vertiport ---
    own_op_free = [v for v in reachable if v.is_own_operator and v.has_free_slot and v.surface_type == "vertiport"]
    if own_op_free:
        best = min(own_op_free, key=lambda v: v.distance_m)
        return LandingOption(
            vertiport_id=best.vertiport_id,
            surface_type=best.surface_type,
            requires_preemption=False,
            preempt_victim_id=None,
            distance_m=best.distance_m,
            suitability_score=0.9,
        )

    # --- 4. Preempt a lower-priority slot ---
    preemptable: list[tuple[VertiportInfo, AgentState]] = []
    for v in reachable:
        if v.vertiport_id in occupied_slots and v.surface_type == "vertiport":
            victim_id = occupied_slots[v.vertiport_id]
            victim = agent_states.get(victim_id)
            if victim is None:
                continue
            # Count victim's remaining options excluding this vertiport
            victim_remaining = len([
                r for r in victim.reachable_options if r != v.vertiport_id
            ])
            if can_preempt(state, victim, victim_remaining):
                preemptable.append((v, victim))

    if preemptable:
        # pick victim with lowest priority metric (least constrained)
        best_v, best_victim = min(preemptable, key=lambda pair: pair[1].priority_metric)
        return LandingOption(
            vertiport_id=best_v.vertiport_id,
            surface_type=best_v.surface_type,
            requires_preemption=True,
            preempt_victim_id=best_victim.agent_id,
            distance_m=best_v.distance_m,
            suitability_score=0.7,
        )

    # --- 5. Other operator's vertiport ---
    other_op = [v for v in reachable if not v.is_own_operator and v.surface_type == "vertiport"]
    if other_op:
        best = min(other_op, key=lambda v: v.distance_m)
        return LandingOption(
            vertiport_id=best.vertiport_id,
            surface_type=best.surface_type,
            requires_preemption=False,
            preempt_victim_id=None,
            distance_m=best.distance_m,
            suitability_score=0.6,
        )

    # --- 6. Light-red prepared surface ---
    light_red = [v for v in reachable if v.surface_type == "light_red"]
    if light_red:
        best = min(light_red, key=lambda v: v.distance_m)
        return LandingOption(
            vertiport_id=best.vertiport_id,
            surface_type="light_red",
            requires_preemption=False,
            preempt_victim_id=None,
            distance_m=best.distance_m,
            suitability_score=0.4,
        )

    # --- 7. Dark-red open field ---
    dark_red = [v for v in reachable if v.surface_type == "dark_red"]
    if dark_red:
        best = min(dark_red, key=lambda v: v.distance_m)
        return LandingOption(
            vertiport_id=best.vertiport_id,
            surface_type="dark_red",
            requires_preemption=False,
            preempt_victim_id=None,
            distance_m=best.distance_m,
            suitability_score=0.2,
        )

    # --- 8. Trailing (ad-hoc spots injected by sim) ---
    trailing = [v for v in reachable if v.surface_type == "trailing"]
    if trailing:
        best = max(trailing, key=lambda v: v.suitability_score)
        return LandingOption(
            vertiport_id=best.vertiport_id,
            surface_type="trailing",
            requires_preemption=False,
            preempt_victim_id=None,
            distance_m=best.distance_m,
            suitability_score=best.suitability_score,
        )

    # --- 9. No option → human ---
    return None


# ---------------------------------------------------------------------------
# Tie-break for two emergencies on the last free spot (concept §5)
# ---------------------------------------------------------------------------

def emergency_tie_break(a: AgentState, b: AgentState) -> AgentState | None:
    """
    Returns the agent that wins the last spot, or None if human must decide.
    Lower remaining energy wins (more constrained).
    """
    if abs(a.battery_pct - b.battery_pct) < 0.5:
        return None   # too close to call → human
    return a if a.battery_pct < b.battery_pct else b

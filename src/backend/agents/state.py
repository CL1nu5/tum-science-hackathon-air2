from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FlightStage(Enum):
    PARKED          = auto()
    PRE_FLIGHT      = auto()   # computing / requesting route
    AWAITING_TAKEOFF = auto()  # route confirmed, waiting for clearance
    CLIMBING        = auto()
    EN_ROUTE        = auto()
    DESCENDING      = auto()
    FINAL_APPROACH  = auto()
    ON_PAD          = auto()
    EMERGENCY       = auto()   # override stage; normal stage still tracked separately


class SlotStage(Enum):
    """Lifecycle of the landing-pad slot reservation (concept §3)."""
    NONE            = auto()
    TENTATIVE       = auto()
    FIRM_FAR        = auto()   # ETA > threshold far
    FIRM_NEAR       = auto()   # ETA within near window
    FINAL_APPROACH  = auto()   # locked in — hard-lock begins here
    ON_PAD          = auto()   # hard-lock
    PARKED          = auto()   # hard-lock


# Hard-lock stages: no preemption, no slot change allowed (concept §7)
HARD_LOCK_SLOT_STAGES: frozenset[SlotStage] = frozenset({
    SlotStage.FINAL_APPROACH,
    SlotStage.ON_PAD,
    SlotStage.PARKED,
})


class AgentStatus(Enum):
    NORMAL    = "NORMAL"
    EMERGENCY = "EMERGENCY"


# ---------------------------------------------------------------------------
# Waypoint
# ---------------------------------------------------------------------------

class Waypoint(NamedTuple):
    x: float   # metres east of reference point
    y: float   # metres north of reference point
    z: float   # altitude in metres
    t: float   # absolute unix timestamp (seconds) at which agent should be here


# ---------------------------------------------------------------------------
# AgentState
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    agent_id: str

    # Position / kinematics
    position: tuple[float, float] = (0.0, 0.0)   # (x, y) metres
    altitude: float = 0.0
    velocity: tuple[float, float] = (0.0, 0.0)   # (vx, vy) m/s
    speed: float = 0.0

    # Power
    battery_pct: float = 100.0

    # Flight lifecycle
    flight_stage: FlightStage = FlightStage.PARKED
    slot_stage: SlotStage = SlotStage.NONE
    status: AgentStatus = AgentStatus.NORMAL

    # Route
    destination_vertiport: str | None = None
    assigned_route: list[Waypoint] = field(default_factory=list)
    current_waypoint_idx: int = 0
    corridor_id: str | None = None

    # Slot
    slot_vertiport: str | None = None
    slot_time: str | None = None    # ISO timestamp of reserved slot
    stand_id: str | None = None

    # Energy / option awareness
    reachable_options: list[str] = field(default_factory=list)  # vertiport IDs
    priority_metric: float = 0.0

    # Reroute immunity (concept §5: bumped taxi gets max 1 reroute)
    reroute_count: int = 0

    # Lateral offset applied during V2V deconfliction (metres, perpendicular)
    lateral_offset: float = 0.0

    def is_hard_locked(self) -> bool:
        return self.slot_stage in HARD_LOCK_SLOT_STAGES

    def can_preempt(self) -> bool:
        return self.status == AgentStatus.EMERGENCY

    def to_api_dict(self) -> dict:
        """Shape matching response-example.json for the frontend API."""
        from datetime import datetime, timezone
        eta_str = self.slot_time or datetime.now(timezone.utc).strftime("%d-%m-%Y:%H-%M-%S")
        return {
            "agent-id": self.agent_id,
            "position": list(self.position),
            "velocity": self.speed,
            "batterie-pecentage": round(self.battery_pct, 2),
            "path": [list(wp[:2]) for wp in self.assigned_route],
            "status": self.status.value,
            "Eta": eta_str,
        }

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from .config import (
    CRUISE_SPEED_MS,
    APPROACH_SPEED_MS,
    DESCEND_RADIUS_M,
    FINAL_APPROACH_RADIUS_M,
    CLIMB_ALTITUDE_M,
    SIM_TICK_S,
)
from .state import AgentState, FlightStage, Waypoint

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def distance_2d(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def distance_3d(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    return math.sqrt(
        (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2
    )


def unit_vector_2d(
    frm: tuple[float, float], to: tuple[float, float]
) -> tuple[float, float]:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    mag = math.hypot(dx, dy)
    if mag < 1e-9:
        return (0.0, 0.0)
    return (dx / mag, dy / mag)


def perpendicular_2d(v: tuple[float, float]) -> tuple[float, float]:
    """90° counter-clockwise rotation."""
    return (-v[1], v[0])


def lateral_offset_position(
    position: tuple[float, float],
    velocity: tuple[float, float],
    offset_m: float,
) -> tuple[float, float]:
    """Compute position shifted laterally (perpendicular to velocity) by offset_m."""
    ux, uy = velocity
    mag = math.hypot(ux, uy)
    if mag < 1e-9:
        return position
    ux, uy = ux / mag, uy / mag
    px, py = perpendicular_2d((ux, uy))
    return (position[0] + px * offset_m, position[1] + py * offset_m)


# ---------------------------------------------------------------------------
# RouteFollower — advances agent state along assigned 4D waypoints
# ---------------------------------------------------------------------------

def interpolate_route_4d(
    route: list[Waypoint], now: float
) -> tuple[tuple[float, float, float], int]:
    """Position on the reserved 4D corridor at wall-clock time ``now``.

    Returns ((x, y, z), segment_index). Clamps to the endpoints outside the
    schedule window. This is the single source of truth for where a taxi is, and
    it mirrors the dashboard's own corridor interpolation so the tower view and
    the simulated motion never disagree.
    """
    if not route:
        return (0.0, 0.0, 0.0), 0
    if now <= route[0].t:
        w = route[0]
        return (w.x, w.y, w.z), 0
    last = route[-1]
    if now >= last.t:
        return (last.x, last.y, last.z), max(0, len(route) - 2)
    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        if a.t <= now <= b.t:
            span = (b.t - a.t) or 1.0
            f = (now - a.t) / span
            return (
                a.x + (b.x - a.x) * f,
                a.y + (b.y - a.y) * f,
                a.z + (b.z - a.z) * f,
            ), i
    return (last.x, last.y, last.z), max(0, len(route) - 2)


class RouteFollower:
    """
    Advances an agent along its reserved 4D corridor by interpolating the
    corridor at the current wall-clock time.

    Why time-interpolation instead of "chase the next waypoint at some speed":
    the previous chase model paced speed = remaining_distance / remaining_time
    with a 0.5 m/s floor, so an on-schedule taxi decelerated asymptotically and
    *froze* mid-air without ever reaching a waypoint — taxis never landed, stands
    never freed and the whole fleet jammed. Following the schedule by time makes
    motion always progress, keeps altitude a smooth ramp, and matches the
    dashboard exactly. Holding (delaying the approach) is expressed by sliding the
    unflown tail of the schedule forward in time — see EvtolAgent._hold.
    """

    LATERAL_OFFSET_M: float = 18.0   # yielding offset during V2V deconfliction

    def step(self, state: AgentState, dt: float) -> dict:
        """Return a dict of fields to update on AgentState. Does not mutate state."""
        route = state.assigned_route
        if not route or state.flight_stage in (
            FlightStage.PARKED, FlightStage.PRE_FLIGHT,
            FlightStage.AWAITING_TAKEOFF, FlightStage.ON_PAD,
        ):
            return {}
        if len(route) < 2:
            return {}

        now = time.time()
        (cx, cy, cz), seg = interpolate_route_4d(route, now)

        # Heading + ground speed come from the active corridor segment.
        a = route[seg]
        b = route[min(seg + 1, len(route) - 1)]
        dx, dy = b.x - a.x, b.y - a.y
        hmag = math.hypot(dx, dy)
        if hmag > 1e-6:
            ux, uy = dx / hmag, dy / hmag
        else:
            ux, uy = 0.0, 0.0
        seg_dur = (b.t - a.t) or 1.0
        speed = hmag / seg_dur
        vx, vy = ux * speed, uy * speed

        # Lateral offset is a *fixed* perpendicular shift off the corridor
        # centreline, recomputed each tick from the clean interpolated point — it
        # never accumulates onto a drifting position (the old bug sent taxis
        # kilometres off course), and the agent decays it back to 0 once clear.
        if state.lateral_offset != 0.0 and (ux or uy):
            px, py = -uy, ux
            cx += px * state.lateral_offset
            cy += py * state.lateral_offset

        # End of the (possibly hold-extended) schedule → touch down.
        if now >= route[-1].t - 1e-3:
            dest = route[-1]
            return {
                "position": (dest.x, dest.y),
                "altitude": dest.z,
                "velocity": (0.0, 0.0),
                "speed": 0.0,
                "current_waypoint_idx": len(route),
                "flight_stage": FlightStage.ON_PAD,
            }

        # Stage transitions by distance to the destination pad.
        new_stage = state.flight_stage
        dest = route[-1]
        dist_to_dest = distance_2d((cx, cy), (dest.x, dest.y))
        if new_stage == FlightStage.EN_ROUTE and dist_to_dest < DESCEND_RADIUS_M:
            new_stage = FlightStage.DESCENDING
        elif new_stage == FlightStage.DESCENDING and dist_to_dest < FINAL_APPROACH_RADIUS_M:
            new_stage = FlightStage.FINAL_APPROACH

        return {
            "position": (cx, cy),
            "altitude": cz,
            "velocity": (vx, vy),
            "speed": speed,
            "current_waypoint_idx": seg + 1,
            "flight_stage": new_stage,
        }


# ---------------------------------------------------------------------------
# V2V Deconfliction — negotiate yield between two agents
# ---------------------------------------------------------------------------

class V2VDeconfliction:
    """
    Implements the local rerouting rule (concept §6):
    lower-priority agent yields; higher-priority holds course.
    """

    OFFSET_M: float = RouteFollower.LATERAL_OFFSET_M

    @staticmethod
    def should_yield(
        my_metric: float,
        peer_metric: float,
        my_id: str = "",
        peer_id: str = "",
    ) -> bool:
        """I yield when my constraint metric is lower (I'm less critical). On an
        exact tie a deterministic rule (higher agent_id yields) breaks the
        symmetry so both sides agree and neither holds into the other."""
        if my_metric != peer_metric:
            return my_metric < peer_metric
        return my_id > peer_id

    @staticmethod
    def compute_offset(
        my_velocity: tuple[float, float],
        yield_side: str = "left",
    ) -> float:
        """
        Returns the signed lateral offset to apply.
        Positive = left of direction of travel, negative = right.
        """
        return V2VDeconfliction.OFFSET_M if yield_side == "left" else -V2VDeconfliction.OFFSET_M

    @staticmethod
    def negotiate(
        my_state: AgentState,
        peer_metric: float,
        peer_id: str = "",
    ) -> float:
        """
        Returns the lateral offset this agent should adopt.
        0.0 means hold course.
        """
        if V2VDeconfliction.should_yield(
            my_state.priority_metric, peer_metric, my_state.agent_id, peer_id
        ):
            return V2VDeconfliction.compute_offset(my_state.velocity)
        return 0.0


# ---------------------------------------------------------------------------
# RouteRequestBuilder — assemble RouteRequest payload from current state
# ---------------------------------------------------------------------------

def build_route_request_payload(state: AgentState, destination_vertiport: str) -> dict:
    """
    Returns a dict suitable for RouteRequest fields.
    Called by the agent before it asks the tower for a route.
    """
    from datetime import datetime, timezone
    return {
        "agent_id": state.agent_id,
        "origin": [state.position[0], state.position[1], state.altitude],
        "destination_vertiport": destination_vertiport,
        "departure_time": datetime.now(timezone.utc).isoformat(),
        "battery_pct": state.battery_pct,
        "speed_capability": CRUISE_SPEED_MS,
    }


def waypoints_from_assignment(raw: list[list[float]]) -> list[Waypoint]:
    """Convert raw [[x,y,z,t], ...] from RouteAssignment into Waypoint list."""
    result = []
    for pt in raw:
        if len(pt) >= 4:
            result.append(Waypoint(x=pt[0], y=pt[1], z=pt[2], t=pt[3]))
        elif len(pt) == 3:
            result.append(Waypoint(x=pt[0], y=pt[1], z=pt[2], t=time.time()))
        elif len(pt) == 2:
            result.append(Waypoint(x=pt[0], y=pt[1], z=CLIMB_ALTITUDE_M, t=time.time()))
    return result

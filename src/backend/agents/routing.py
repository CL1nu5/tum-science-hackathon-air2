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

class RouteFollower:
    """
    Stateless helper: given the current AgentState and elapsed time dt,
    returns the updated position, velocity, altitude, and flight stage.
    Call step() each simulation tick.
    """

    LATERAL_OFFSET_M: float = 18.0   # yielding offset during V2V deconfliction

    def step(self, state: AgentState, dt: float) -> dict:
        """
        Returns a dict of fields to update on AgentState.
        Pure function — does not mutate state.
        """
        route = state.assigned_route
        if not route or state.flight_stage in (
            FlightStage.PARKED, FlightStage.PRE_FLIGHT,
            FlightStage.AWAITING_TAKEOFF, FlightStage.ON_PAD,
        ):
            return {}

        idx = state.current_waypoint_idx
        if idx >= len(route):
            return {"flight_stage": FlightStage.ON_PAD}

        target = route[idx]
        tx, ty, tz = target.x, target.y, target.z
        cx, cy = state.position
        cz = state.altitude

        # Distance to next waypoint
        dist_2d = distance_2d((cx, cy), (tx, ty))
        dist_3d = distance_3d((cx, cy, cz), (tx, ty, tz))

        # Follow the reserved 4D schedule rather than only its geometry.
        remaining_s = target.t - time.time()
        if remaining_s > dt:
            speed = max(0.5, min(dist_3d / remaining_s, CRUISE_SPEED_MS * 1.3))
            if state.flight_stage == FlightStage.FINAL_APPROACH:
                speed = min(speed, APPROACH_SPEED_MS)
        elif state.flight_stage == FlightStage.FINAL_APPROACH:
            speed = APPROACH_SPEED_MS
        elif state.flight_stage == FlightStage.CLIMBING:
            speed = CRUISE_SPEED_MS * 0.6
        elif state.flight_stage == FlightStage.DESCENDING:
            speed = CRUISE_SPEED_MS * 0.8
        else:
            speed = state.speed if state.speed > 0 else CRUISE_SPEED_MS

        step_dist = speed * dt

        if dist_3d < step_dist:
            # Reached this waypoint — advance to next
            next_idx = idx + 1
            if next_idx >= len(route):
                return {
                    "position": (tx, ty),
                    "altitude": tz,
                    "velocity": (0.0, 0.0),
                    "speed": 0.0,
                    "current_waypoint_idx": next_idx,
                    "flight_stage": FlightStage.ON_PAD,
                }
            return {
                "position": (tx, ty),
                "altitude": tz,
                "current_waypoint_idx": next_idx,
                "speed": speed,
            }

        # Move toward waypoint
        ratio = step_dist / dist_3d
        new_x = cx + (tx - cx) * ratio
        new_y = cy + (ty - cy) * ratio
        new_z = cz + (tz - cz) * ratio

        vx = (tx - cx) / dist_3d * speed
        vy = (ty - cy) / dist_3d * speed

        # Apply lateral offset if in deconfliction mode
        if state.lateral_offset != 0.0:
            new_x, new_y = lateral_offset_position(
                (new_x, new_y), (vx, vy), state.lateral_offset
            )

        # Determine flight stage transitions based on distance to final destination
        new_stage = state.flight_stage
        if route:
            dest = route[-1]
            dist_to_dest = distance_2d((new_x, new_y), (dest.x, dest.y))
            if (
                new_stage == FlightStage.EN_ROUTE
                and dist_to_dest < DESCEND_RADIUS_M
            ):
                new_stage = FlightStage.DESCENDING
            elif (
                new_stage == FlightStage.DESCENDING
                and dist_to_dest < FINAL_APPROACH_RADIUS_M
            ):
                new_stage = FlightStage.FINAL_APPROACH

        updates: dict = {
            "position": (new_x, new_y),
            "altitude": new_z,
            "velocity": (vx, vy),
            "speed": speed,
            "flight_stage": new_stage,
        }
        return updates


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
    def should_yield(my_metric: float, peer_metric: float) -> bool:
        """I yield when my constraint metric is strictly lower (I'm less critical)."""
        return my_metric < peer_metric

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
    ) -> float:
        """
        Returns the lateral offset this agent should adopt.
        0.0 means hold course.
        """
        if V2VDeconfliction.should_yield(my_state.priority_metric, peer_metric):
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

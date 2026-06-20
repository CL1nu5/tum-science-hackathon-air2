from __future__ import annotations

import copy
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..agents.messages import RouteRequest
from .config import Settings
from .conflicts import CorridorMargins, point_to_segment_distance, routes_conflict
from .slot_scheduler import PadSlotScheduler, SlotUnavailable, parse_time
from .store import JsonStore, new_id, now_iso


class RoutePlanningError(RuntimeError):
    pass


@dataclass
class FlightPlan:
    route: dict
    slot: dict


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RoutePlanner:
    def __init__(
        self,
        settings: Settings,
        store: JsonStore,
        slots: PadSlotScheduler,
    ) -> None:
        self.settings = settings
        self.store = store
        self.slots = slots

    async def reserve_flight_plan(self, request: RouteRequest) -> FlightPlan:
        async with self.store.lock:
            # The locked planner mutates aircraft/stand state before it may fail;
            # snapshot so a rejected request leaves no partial changes behind.
            original = copy.deepcopy(self.store.state)
            try:
                plan = self.reserve_flight_plan_locked(request)
            except Exception:
                self.store.state = original
                raise
            self.store.persist_locked()
            return plan

    def reserve_flight_plan_locked(self, request: RouteRequest) -> FlightPlan:
        started = time.monotonic()
        aircraft = self.store.state["aircraft"].setdefault(
            request.agent_id,
            {
                "agent_id": request.agent_id,
                "position": request.origin[:2],
                "altitude_m": request.origin[2],
                "velocity": [0.0, 0.0],
                "speed_ms": 0.0,
                "battery_pct": request.battery_pct,
                "status": "NORMAL",
                "flight_stage": "PARKED",
                "slot_stage": "NONE",
                "destination_vertiport": None,
                "route": [],
                "reachable_options": [],
                "priority_metric": 0.0,
                "reroute_count": 0,
                "connected": True,
                "last_seen_at": now_iso(),
                "revision": 1,
            },
        )
        aircraft["position"] = request.origin[:2]
        aircraft["altitude_m"] = request.origin[2]
        aircraft["battery_pct"] = request.battery_pct
        destination = self.store.state["vertiports"].get(
            request.destination_vertiport
        )
        if not destination or not destination["active"]:
            raise RoutePlanningError(
                f"Unknown or inactive destination {request.destination_vertiport}"
            )
        if not destination["pad_available"]:
            raise RoutePlanningError(f"Destination {request.destination_vertiport} failed")

        self.slots.release_departure_stand_locked(request.agent_id)
        departure = max(
            parse_timestamp(request.departure_time),
            datetime.now(timezone.utc) + timedelta(seconds=2),
        )
        infrastructure = list(self.store.state["vertiports"].values())
        existing_routes = [
            route
            for route in self.store.state["routes"].values()
            if route.get("active", True) and route["agent_id"] != request.agent_id
        ]
        weather = [
            cell
            for cell in self.store.state["weather"].values()
            if cell.get("active", True)
        ]
        airspace_zones = [
            zone
            for zone in self.store.state.get("airspace_zones", {}).values()
            if zone.get("active", True)
        ]
        noise_zones = [
            zone
            for zone in self.store.state["noise_zones"].values()
            if zone.get("active", True)
        ]

        is_vertiport = destination["surface_type"] == "vertiport"
        feasible: list[tuple[float, list, datetime]] = []
        for candidate in self._candidate_routes(request, destination, departure):
            if time.monotonic() - started > self.settings.route_planning_timeout_s:
                break  # honour the hard timeout; pick the best found so far
            if not self._route_has_emergency_coverage(
                candidate,
                infrastructure,
                request.battery_pct,
                request.speed_capability,
            ):
                continue
            if self._has_conflict(candidate, existing_routes):
                continue
            if self._violates_no_fly(candidate, airspace_zones):
                continue
            if not self._weather_safe(candidate, weather):
                continue
            if not self._respects_noise_limits(
                candidate, noise_zones, existing_routes
            ):
                continue

            eta = datetime.fromtimestamp(candidate[-1][3], timezone.utc)
            if is_vertiport:
                start_at, end_at = self.slots.interval_for_eta(eta)
                if not self.slots.is_interval_free_locked(
                    destination["vertiport_id"],
                    start_at,
                    end_at,
                    exclude_agent_id=request.agent_id,
                ):
                    continue
            feasible.append(
                (
                    self._score(
                        candidate,
                        weather,
                        noise_zones,
                        existing_routes,
                        airspace_zones,
                    ),
                    candidate,
                    eta,
                )
            )

        if not feasible:
            raise RoutePlanningError("No conflict-free route and landing slot found")

        # The tower hands out an already-conflict-free, optimal corridor: lowest
        # cost first (least delay, least residential noise, clearest of weather).
        feasible.sort(key=lambda item: item[0])
        for score, candidate, eta in feasible:
            try:
                slot = self.slots.reserve_exact_locked(
                    agent_id=request.agent_id,
                    vertiport_id=destination["vertiport_id"],
                    eta=eta,
                    emergency_standby=not is_vertiport,
                    reroute_count=aircraft["reroute_count"],
                )
            except SlotUnavailable:
                continue

            for route in self.store.state["routes"].values():
                if route["agent_id"] == request.agent_id and route.get("active", True):
                    route["active"] = False
                    route["revision"] += 1

            reservation_id = new_id()
            route = {
                "reservation_id": reservation_id,
                "corridor_id": f"C-{uuid.uuid4().hex[:10].upper()}",
                "agent_id": request.agent_id,
                "destination_vertiport": destination["vertiport_id"],
                "departure_at": datetime.fromtimestamp(
                    candidate[0][3], timezone.utc
                ).isoformat(),
                "eta_at": eta.isoformat(),
                "waypoints": candidate,
                "horizontal_margin_m": self.settings.route_horizontal_margin_m,
                "vertical_margin_m": self.settings.route_vertical_margin_m,
                "temporal_buffer_s": self.settings.route_time_buffer_s,
                "score": score,
                "lease_expires_at": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.settings.route_lease_seconds)
                ).isoformat(),
                "active": True,
                "locally_modified": False,
                "revision": 1,
                "created_at": now_iso(),
            }
            self.store.state["routes"][reservation_id] = route
            aircraft.update(
                {
                    "destination_vertiport": destination["vertiport_id"],
                    "route": candidate,
                    "flight_stage": "AWAITING_TAKEOFF",
                    "slot_stage": "TENTATIVE",
                    "revision": aircraft["revision"] + 1,
                }
            )
            self.store.event_locked(
                "FLIGHT_PLAN_RESERVED",
                f"Route and landing slot reserved for {request.agent_id}",
                agent_id=request.agent_id,
                payload={
                    "route_reservation_id": reservation_id,
                    "slot_reservation_id": slot["reservation_id"],
                },
            )
            return FlightPlan(route=route, slot=slot)
        raise RoutePlanningError("No conflict-free route and landing slot found")

    def _candidate_routes(self, request, destination, departure):
        speed = max(15.0, min(request.speed_capability, self.settings.cruise_speed_ms))
        altitudes = (150.0, 195.0, 240.0, 285.0, 330.0, 375.0)
        doglegs = (-0.16, 0.16, -0.30, 0.30, -0.48, 0.48, -0.66, 0.66)
        # 4D corridors mean two taxis can share a path at a different altitude or
        # slightly offset lane. We exhaust those zero-delay options first (so the
        # demo keeps taxis moving) and only then start delaying take-off, per the
        # concept's resolution order: delay -> altitude -> speed -> reroute.
        delays = (0, 0.15, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 14, 18, 20)
        for delay in delays:
            if delay > self.settings.max_route_delay_minutes:
                break
            departs = departure + timedelta(minutes=delay)
            for altitude in altitudes:
                yield self._build_route(
                    request.origin, destination, departs, altitude, speed, 0.0
                )
            for dogleg in doglegs:
                yield self._build_route(
                    request.origin, destination, departs, 150.0, speed, dogleg
                )
        for factor in (0.85, 1.15):
            scaled = max(15.0, min(speed * factor, self.settings.cruise_speed_ms))
            yield self._build_route(
                request.origin, destination, departure, 150.0, scaled, 0.0
            )

    def _build_route(self, origin, destination, departure, altitude, speed, dogleg):
        ox, oy, oz = origin
        tx, ty = destination["position"]
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        # Brisk climb/descent so taxis don't sit motionless on the 2D map while
        # changing only altitude (~12 s for a 150 m climb).
        climb_s = max(6.0, abs(altitude - oz) / 12.0)
        descent_s = max(
            6.0, abs(altitude - destination.get("elevation_m", 0.0)) / 12.0
        )
        cruise_s = distance / max(speed, 1.0)
        start = departure.timestamp()
        midpoint = (
            ox + dx * 0.5 - dy * dogleg,
            oy + dy * 0.5 + dx * dogleg,
        )
        return [
            [ox, oy, oz, start],
            [ox, oy, altitude, start + climb_s],
            [midpoint[0], midpoint[1], altitude, start + climb_s + cruise_s * 0.5],
            [tx, ty, altitude, start + climb_s + cruise_s],
            [
                tx,
                ty,
                destination.get("elevation_m", 0.0),
                start + climb_s + cruise_s + descent_s,
            ],
        ]

    def _has_conflict(self, candidate, existing):
        margins = CorridorMargins(
            self.settings.route_horizontal_margin_m,
            self.settings.route_vertical_margin_m,
            self.settings.route_time_buffer_s,
        )
        return any(
            routes_conflict(
                candidate,
                route["waypoints"],
                first_margins=margins,
                second_margins=CorridorMargins(
                    route["horizontal_margin_m"],
                    route["vertical_margin_m"],
                    route["temporal_buffer_s"],
                ),
                sample_interval_s=self.settings.route_sample_interval_s,
            )
            for route in existing
        )

    def _route_has_emergency_coverage(
        self, route, infrastructure, battery_pct, speed_ms
    ):
        max_distance = self.settings.emergency_reachability_minutes * 60 * max(
            speed_ms, 1.0
        )
        for point in route:
            nearest = min(
                (
                    math.hypot(port["position"][0] - point[0], port["position"][1] - point[1])
                    for port in infrastructure
                    if port["active"] and port.get("pad_available", True)
                ),
                default=math.inf,
            )
            energy = (
                nearest / max(speed_ms, 1.0)
                * self.settings.nominal_battery_drain_per_s
            )
            if nearest > max_distance or battery_pct - energy < self.settings.safety_reserve_pct:
                return False
        return True

    def _weather_safe(self, route, weather):
        return not any(
            cell["severity"] >= 0.7
            and self._crosses_circle(route, cell["center"], cell["radius_m"])
            for cell in weather
        )

    def _respects_noise_limits(self, route, zones, existing):
        for zone in zones:
            if not self._crosses_circle(route, zone["center"], zone["radius_m"]):
                continue
            load = sum(
                self._crosses_circle(
                    item["waypoints"], zone["center"], zone["radius_m"]
                )
                for item in existing
            )
            if load >= zone["max_active_overflights"]:
                return False
        return True

    def _violates_no_fly(self, route, airspace_zones):
        return any(
            zone.get("kind") == "nofly"
            and self._crosses_polygon(
                route,
                zone.get("polygon", []),
                zone.get("avoidance_margin_m", 0.0),
            )
            for zone in airspace_zones
        )

    def _score(self, route, weather, zones, existing, airspace_zones):
        score = route[-1][3] - route[0][3] + len(existing) * 2.0
        for cell in weather:
            if self._crosses_circle(route, cell["center"], cell["radius_m"]):
                score += cell["severity"] * 1000.0
        for zone in airspace_zones:
            if zone.get("kind") == "restrict" and self._crosses_polygon(
                route,
                zone.get("polygon", []),
                zone.get("avoidance_margin_m", 0.0),
            ):
                score += zone.get("penalty_weight", 150.0)
        for zone in zones:
            if self._crosses_circle(route, zone["center"], zone["radius_m"]):
                load = sum(
                    self._crosses_circle(
                        item["waypoints"], zone["center"], zone["radius_m"]
                    )
                    for item in existing
                )
                score += zone["penalty_weight"] * (load + 1) ** 2
        return score

    @staticmethod
    def _crosses_circle(route, center, radius):
        return any(
            point_to_segment_distance(
                tuple(center),
                (start[0], start[1]),
                (end[0], end[1]),
            )
            < radius
            for start, end in zip(route, route[1:])
        )

    @classmethod
    def _crosses_polygon(cls, route, polygon, margin_m: float = 0.0):
        if len(polygon) < 3:
            return False
        for start, end in zip(route, route[1:]):
            a = (start[0], start[1])
            b = (end[0], end[1])
            if cls._point_in_polygon(a, polygon) or cls._point_in_polygon(b, polygon):
                return True
            for first, second in zip(polygon, polygon[1:] + polygon[:1]):
                c = (first[0], first[1])
                d = (second[0], second[1])
                if cls._segments_intersect(a, b, c, d):
                    return True
                if margin_m > 0 and cls._segment_distance(a, b, c, d) < margin_m:
                    return True
        return False

    @staticmethod
    def _point_in_polygon(point, polygon):
        x, y = point
        inside = False
        j = len(polygon) - 1
        for i in range(len(polygon)):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _segments_intersect(a, b, c, d):
        def orient(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        def on_segment(p, q, r):
            return (
                min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
                and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
            )

        o1 = orient(a, b, c)
        o2 = orient(a, b, d)
        o3 = orient(c, d, a)
        o4 = orient(c, d, b)
        if o1 == 0 and on_segment(a, c, b):
            return True
        if o2 == 0 and on_segment(a, d, b):
            return True
        if o3 == 0 and on_segment(c, a, d):
            return True
        if o4 == 0 and on_segment(c, b, d):
            return True
        return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)

    @classmethod
    def _segment_distance(cls, a, b, c, d):
        if cls._segments_intersect(a, b, c, d):
            return 0.0
        return min(
            point_to_segment_distance(a, c, d),
            point_to_segment_distance(b, c, d),
            point_to_segment_distance(c, a, b),
            point_to_segment_distance(d, a, b),
        )

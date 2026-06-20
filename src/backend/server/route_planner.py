from __future__ import annotations

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
            plan = self.reserve_flight_plan_locked(request)
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
        noise_zones = [
            zone
            for zone in self.store.state["noise_zones"].values()
            if zone.get("active", True)
        ]

        for candidate in self._candidate_routes(request, destination, departure):
            if time.monotonic() - started > self.settings.route_planning_timeout_s:
                raise RoutePlanningError("Route planning exceeded the hard timeout")
            if not self._route_has_emergency_coverage(
                candidate,
                infrastructure,
                request.battery_pct,
                request.speed_capability,
            ):
                continue
            if self._has_conflict(candidate, existing_routes):
                continue
            if not self._weather_safe(candidate, weather):
                continue
            if not self._respects_noise_limits(
                candidate, noise_zones, existing_routes
            ):
                continue

            eta = datetime.fromtimestamp(candidate[-1][3], timezone.utc)
            try:
                slot = self.slots.reserve_exact_locked(
                    agent_id=request.agent_id,
                    vertiport_id=destination["vertiport_id"],
                    eta=eta,
                    emergency_standby=destination["surface_type"] != "vertiport",
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
                "score": self._score(
                    candidate, weather, noise_zones, existing_routes
                ),
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
        for delay in range(0, self.settings.max_route_delay_minutes + 1, 2):
            yield self._build_route(
                request.origin,
                destination,
                departure + timedelta(minutes=delay),
                150.0,
                speed,
                0.0,
            )
        for altitude in (210.0, 270.0, 330.0):
            yield self._build_route(
                request.origin, destination, departure, altitude, speed, 0.0
            )
        for factor in (0.8, 1.15):
            yield self._build_route(
                request.origin, destination, departure, 150.0, speed * factor, 0.0
            )
        for dogleg in (-0.22, 0.22, -0.38, 0.38):
            yield self._build_route(
                request.origin, destination, departure, 150.0, speed, dogleg
            )

    def _build_route(self, origin, destination, departure, altitude, speed, dogleg):
        ox, oy, oz = origin
        tx, ty = destination["position"]
        dx, dy = tx - ox, ty - oy
        distance = math.hypot(dx, dy)
        climb_s = max(8.0, abs(altitude - oz) / 5.0)
        descent_s = max(
            8.0, abs(altitude - destination.get("elevation_m", 0.0)) / 5.0
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
                    if port["active"]
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

    def _score(self, route, weather, zones, existing):
        score = route[-1][3] - route[0][3] + len(existing) * 2.0
        for cell in weather:
            if self._crosses_circle(route, cell["center"], cell["radius_m"]):
                score += cell["severity"] * 1000.0
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

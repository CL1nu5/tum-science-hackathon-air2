from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from ..agents.messages import EmergencyDeclaration, RouteRequest
from .config import Settings
from .route_planner import FlightPlan, RoutePlanner, RoutePlanningError
from .slot_scheduler import HARD_LOCK_STAGES, PadSlotScheduler
from .state_service import TowerStateService
from .store import JsonStore


@dataclass
class EmergencyDecision:
    outcome: str
    reason: str
    target: dict | None = None
    plan: FlightPlan | None = None
    preempted_agent_id: str | None = None
    victim_backups: list[str] | None = None


class EmergencyCoordinator:
    def __init__(
        self,
        settings: Settings,
        store: JsonStore,
        slots: PadSlotScheduler,
        routes: RoutePlanner,
        state_service: TowerStateService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.slots = slots
        self.routes = routes
        self.state_service = state_service

    async def resolve(
        self, declaration: EmergencyDeclaration
    ) -> EmergencyDecision:
        async with self.store.lock:
            original = copy.deepcopy(self.store.state)
            try:
                decision = self._resolve_locked(declaration)
                self.store.persist_locked()
                return decision
            except Exception:
                self.store.state = original
                raise

    def _resolve_locked(
        self, declaration: EmergencyDeclaration
    ) -> EmergencyDecision:
        aircraft = self.state_service._aircraft_locked(declaration.agent_id)
        aircraft.update(
            {
                "status": "EMERGENCY",
                "battery_pct": declaration.battery_pct,
                "position": declaration.position[:2],
                "altitude_m": declaration.altitude,
            }
        )
        reachable = self.state_service.reachable_locked(aircraft)
        aircraft["reachable_options"] = [
            port["vertiport_id"] for port in reachable
        ]
        aircraft["priority_metric"] = self.state_service.priority(
            aircraft["battery_pct"], len(reachable)
        )
        aircraft["revision"] += 1
        if not reachable:
            return self._human_locked(
                aircraft, "No landing surface is reachable"
            )

        current = self.slots.active_for_agent_locked(aircraft["agent_id"])
        current_target = next(
            (
                port
                for port in reachable
                if current and port["vertiport_id"] == current["vertiport_id"]
            ),
            None,
        )
        own = sorted(
            (
                port
                for port in reachable
                if port["surface_type"] == "vertiport"
                and port["operator"] == "AIR2"
                and port is not current_target
            ),
            key=lambda port: self._distance(aircraft, port),
        )
        for target in ([current_target] if current_target else []) + own:
            try:
                plan = self.routes.reserve_flight_plan_locked(
                    self._request(aircraft, target)
                )
                return self._resolved_locked(aircraft, target, plan)
            except RoutePlanningError:
                continue

        vertiports = [
            port for port in reachable if port["surface_type"] == "vertiport"
        ]
        preemption = self._find_preemption_locked(aircraft, vertiports)
        if preemption:
            before_preemption = copy.deepcopy(self.store.state)
            target, slot, victim, backups = preemption
            slot["active"] = False
            slot["preempted_by"] = aircraft["agent_id"]
            slot["revision"] += 1
            victim["reroute_count"] += 1
            victim["slot_stage"] = "NONE"
            victim["destination_vertiport"] = backups[0]
            victim["revision"] += 1
            try:
                plan = self.routes.reserve_flight_plan_locked(
                    self._request(aircraft, target)
                )
            except RoutePlanningError:
                self.store.state = before_preemption
                aircraft = self.store.state["aircraft"][declaration.agent_id]
                return self._human_locked(
                    aircraft, "Preemption found, but no route could be reserved"
                )
            self.store.event_locked(
                "SLOT_PREEMPTED",
                f"{aircraft['agent_id']} preempted {victim['agent_id']}",
                severity="WARNING",
                agent_id=aircraft["agent_id"],
            )
            return self._resolved_locked(
                aircraft,
                target,
                plan,
                preempted_agent_id=victim["agent_id"],
                victim_backups=backups,
            )

        remaining = sorted(
            (
                port
                for port in reachable
                if port not in own and port is not current_target
            ),
            key=lambda port: (
                self._category(port),
                self._distance(aircraft, port),
            ),
        )
        for target in remaining:
            try:
                plan = self.routes.reserve_flight_plan_locked(
                    self._request(aircraft, target)
                )
                return self._resolved_locked(aircraft, target, plan)
            except RoutePlanningError:
                continue
        return self._human_locked(
            aircraft, "Automatic emergency cascade found no solution"
        )

    def _find_preemption_locked(self, attacker: dict, targets: list[dict]):
        if attacker["status"] != "EMERGENCY":
            return None
        options = []
        for target in targets:
            if target["surface_type"] != "vertiport":
                continue
            for slot in self.store.state["pad_reservations"].values():
                if (
                    not slot.get("active", True)
                    or slot["vertiport_id"] != target["vertiport_id"]
                    or slot["stage"] in HARD_LOCK_STAGES
                    or slot["agent_id"] == attacker["agent_id"]
                ):
                    continue
                victim = self.store.state["aircraft"].get(slot["agent_id"])
                if (
                    not victim
                    or victim["reroute_count"]
                    >= self.settings.max_reroutes_per_flight
                    or attacker["priority_metric"] <= victim["priority_metric"]
                ):
                    continue
                backups = [
                    port["vertiport_id"]
                    for port in self.state_service.reachable_locked(victim)
                    if port["vertiport_id"] != target["vertiport_id"]
                ]
                if backups:
                    options.append(
                        (
                            victim["priority_metric"],
                            target,
                            slot,
                            victim,
                            backups,
                        )
                    )
        if not options:
            return None
        _, target, slot, victim, backups = min(options, key=lambda item: item[0])
        return target, slot, victim, backups

    def _request(self, aircraft: dict, target: dict) -> RouteRequest:
        return RouteRequest(
            agent_id=aircraft["agent_id"],
            origin=[
                aircraft["position"][0],
                aircraft["position"][1],
                aircraft["altitude_m"],
            ],
            destination_vertiport=target["vertiport_id"],
            departure_time=datetime.now(timezone.utc).isoformat(),
            battery_pct=aircraft["battery_pct"],
            speed_capability=aircraft["speed_ms"] or self.settings.cruise_speed_ms,
        )

    def _resolved_locked(
        self,
        aircraft: dict,
        target: dict,
        plan: FlightPlan,
        *,
        preempted_agent_id: str | None = None,
        victim_backups: list[str] | None = None,
    ) -> EmergencyDecision:
        self.store.event_locked(
            "EMERGENCY_RESOLVED",
            f"{aircraft['agent_id']} secured {target['vertiport_id']}",
            severity="WARNING",
            agent_id=aircraft["agent_id"],
        )
        return EmergencyDecision(
            outcome="RESOLVED",
            reason="Safe landing option reserved",
            target=target,
            plan=plan,
            preempted_agent_id=preempted_agent_id,
            victim_backups=victim_backups,
        )

    def _human_locked(self, aircraft: dict, reason: str) -> EmergencyDecision:
        self.store.event_locked(
            "HUMAN_TAKEOVER_REQUIRED",
            reason,
            severity="CRITICAL",
            agent_id=aircraft["agent_id"],
        )
        return EmergencyDecision(outcome="HUMAN_REQUIRED", reason=reason)

    @staticmethod
    def _distance(aircraft: dict, target: dict) -> float:
        return math.hypot(
            target["position"][0] - aircraft["position"][0],
            target["position"][1] - aircraft["position"][1],
        )

    @staticmethod
    def _category(target: dict) -> int:
        return {
            "vertiport": 0 if target["operator"] == "AIR2" else 2,
            "light_red": 3,
            "dark_red": 4,
            "trailing": 5,
        }.get(target["surface_type"], 9)

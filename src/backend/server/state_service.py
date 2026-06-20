from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from ..agents.messages import StateUpdate
from .config import Settings
from .slot_scheduler import HARD_LOCK_STAGES
from .store import JsonStore, now_iso


class TowerStateService:
    def __init__(self, settings: Settings, store: JsonStore) -> None:
        self.settings = settings
        self.store = store

    async def mark_connected(self, agent_id: str, connected: bool) -> dict:
        async with self.store.lock:
            aircraft = self._aircraft_locked(agent_id)
            aircraft["connected"] = connected
            aircraft["last_seen_at"] = now_iso()
            aircraft["revision"] += 1
            self.store.persist_locked()
            return aircraft.copy()

    async def ingest(self, update: StateUpdate) -> dict:
        async with self.store.lock:
            aircraft = self._aircraft_locked(update.agent_id)
            aircraft.update(
                {
                    "position": update.position[:2],
                    "altitude_m": update.altitude,
                    "speed_ms": update.speed,
                    "battery_pct": update.battery_pct,
                    "flight_stage": update.intent,
                    "slot_stage": update.slot_stage,
                    "destination_vertiport": update.destination_vertiport,
                    "route": update.route,
                    "connected": True,
                    "last_seen_at": now_iso(),
                }
            )
            reachable = self.reachable_locked(aircraft)
            aircraft["reachable_options"] = [
                port["vertiport_id"] for port in reachable
            ]
            aircraft["priority_metric"] = self.priority(
                aircraft["battery_pct"], len(reachable)
            )
            aircraft["revision"] += 1

            route = self.active_route_locked(update.agent_id)
            if route:
                renewed = datetime.now(timezone.utc) + timedelta(
                    seconds=self.settings.route_lease_seconds
                )
                if update.intent in {
                    "CLIMBING",
                    "EN_ROUTE",
                    "DESCENDING",
                    "FINAL_APPROACH",
                    "ON_PAD",
                }:
                    renewed = max(
                        renewed,
                        datetime.fromisoformat(route["eta_at"]) + timedelta(minutes=5),
                    )
                route["lease_expires_at"] = renewed.isoformat()
                route["revision"] += 1

            slot = self.active_slot_locked(update.agent_id)
            if slot:
                if update.slot_stage in HARD_LOCK_STAGES:
                    slot["stage"] = update.slot_stage
                if slot["stage"] not in HARD_LOCK_STAGES:
                    slot["lease_expires_at"] = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.settings.slot_lease_seconds)
                    ).isoformat()
                slot["revision"] += 1
            self.store.persist_locked()
            return aircraft.copy()

    def _aircraft_locked(self, agent_id: str) -> dict:
        return self.store.state["aircraft"].setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "position": [0.0, 0.0],
                "altitude_m": 0.0,
                "velocity": [0.0, 0.0],
                "speed_ms": 0.0,
                "battery_pct": 100.0,
                "status": "NORMAL",
                "flight_stage": "PARKED",
                "slot_stage": "NONE",
                "destination_vertiport": None,
                "route": [],
                "reachable_options": [],
                "priority_metric": 0.0,
                "reroute_count": 0,
                "connected": False,
                "last_seen_at": now_iso(),
                "revision": 0,
            },
        )

    def reachable_locked(self, aircraft: dict) -> list[dict]:
        speed = aircraft["speed_ms"] or self.settings.cruise_speed_ms
        reachable = []
        for port in self.store.state["vertiports"].values():
            if not port["active"]:
                continue
            distance = math.hypot(
                port["position"][0] - aircraft["position"][0],
                port["position"][1] - aircraft["position"][1],
            )
            energy = (
                distance / max(speed, 1.0)
                * self.settings.nominal_battery_drain_per_s
            )
            if (
                aircraft["battery_pct"] - energy
                >= self.settings.safety_reserve_pct
            ):
                reachable.append(port)
        return reachable

    def active_route_locked(self, agent_id: str) -> dict | None:
        return next(
            (
                route
                for route in self.store.state["routes"].values()
                if route["agent_id"] == agent_id and route.get("active", True)
            ),
            None,
        )

    def active_slot_locked(self, agent_id: str) -> dict | None:
        return next(
            (
                slot
                for slot in self.store.state["pad_reservations"].values()
                if slot["agent_id"] == agent_id and slot.get("active", True)
            ),
            None,
        )

    @staticmethod
    def priority(battery_pct: float, reachable_count: int) -> float:
        return 0.6 * (1.0 - battery_pct / 100.0) + 0.4 * (
            1.0 - min(reachable_count, 10) / 10.0
        )

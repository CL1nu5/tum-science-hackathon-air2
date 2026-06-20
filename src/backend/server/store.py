from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def default_state() -> dict[str, Any]:
    vertiports = {
        "VP-01": {
            "vertiport_id": "VP-01",
            "name": "Olympiapark",
            "position": [-4200.0, 2100.0],
            "elevation_m": 0.0,
            "operator": "AIR2",
            "surface_type": "vertiport",
            "suitability_score": 1.0,
            "pad_available": True,
            "active": True,
        },
        "VP-02": {
            "vertiport_id": "VP-02",
            "name": "Messe Riem",
            "position": [6500.0, 1200.0],
            "elevation_m": 0.0,
            "operator": "AIR2",
            "surface_type": "vertiport",
            "suitability_score": 1.0,
            "pad_available": True,
            "active": True,
        },
        "VP-03": {
            "vertiport_id": "VP-03",
            "name": "Harlaching",
            "position": [1400.0, -5100.0],
            "elevation_m": 0.0,
            "operator": "AIR2",
            "surface_type": "vertiport",
            "suitability_score": 1.0,
            "pad_available": True,
            "active": True,
        },
        "VP-04": {
            "vertiport_id": "VP-04",
            "name": "Schwabing",
            "position": [-600.0, 3900.0],
            "elevation_m": 0.0,
            "operator": "OTHER",
            "surface_type": "vertiport",
            "suitability_score": 1.0,
            "pad_available": True,
            "active": True,
        },
        "EMER-01": {
            "vertiport_id": "EMER-01",
            "name": "Prepared emergency surface",
            "position": [2600.0, -800.0],
            "elevation_m": 0.0,
            "operator": "PUBLIC",
            "surface_type": "light_red",
            "suitability_score": 0.6,
            "pad_available": True,
            "active": True,
        },
        "FIELD-01": {
            "vertiport_id": "FIELD-01",
            "name": "Open emergency field",
            "position": [-5200.0, -3500.0],
            "elevation_m": 0.0,
            "operator": "PUBLIC",
            "surface_type": "dark_red",
            "suitability_score": 0.4,
            "pad_available": True,
            "active": True,
        },
    }
    stand_counts = {"VP-01": 4, "VP-02": 3, "VP-03": 3, "VP-04": 2}
    stands = {}
    for vertiport_id, count in stand_counts.items():
        for index in range(count):
            stand_id = f"{vertiport_id}-S{index + 1:02d}"
            stands[stand_id] = {
                "stand_id": stand_id,
                "vertiport_id": vertiport_id,
                "name": stand_id,
                "occupied_by": None,
                "active": True,
            }
    return {
        "aircraft": {},
        "vertiports": vertiports,
        "stands": stands,
        "routes": {},
        "pad_reservations": {},
        "weather": {},
        "noise_zones": {
            "central": {
                "zone_id": "central",
                "name": "Central residential zone",
                "center": [0.0, 0.0],
                "radius_m": 1500.0,
                "penalty_weight": 180.0,
                "max_active_overflights": 4,
                "active": True,
            }
        },
        "events": [],
    }


class JsonStore:
    """One-process state store persisted to a human-readable JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.state: dict[str, Any] = default_state()

    async def load(self) -> None:
        async with self.lock:
            if self.path.exists():
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self.persist_locked()

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict[str, Any]:
        state = copy.deepcopy(self.state)
        state["generated_at"] = now_iso()
        state["aircraft"] = list(state["aircraft"].values())
        state["vertiports"] = [
            {
                **port,
                "stands": [
                    stand
                    for stand in state["stands"].values()
                    if stand["vertiport_id"] == port["vertiport_id"]
                ],
            }
            for port in state["vertiports"].values()
        ]
        state.pop("stands", None)
        state["routes"] = [
            item for item in state["routes"].values() if item.get("active", True)
        ]
        state["pad_reservations"] = [
            item
            for item in state["pad_reservations"].values()
            if item.get("active", True)
        ]
        state["weather"] = [
            item for item in state["weather"].values() if item.get("active", True)
        ]
        state["noise_zones"] = [
            item
            for item in state["noise_zones"].values()
            if item.get("active", True)
        ]
        state["events"] = list(reversed(state["events"][-100:]))
        return state

    def persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def event_locked(
        self,
        event_type: str,
        message: str,
        *,
        severity: str = "INFO",
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": new_id(),
            "event_type": event_type,
            "severity": severity,
            "agent_id": agent_id,
            "message": message,
            "payload": payload or {},
            "created_at": now_iso(),
        }
        self.state["events"].append(event)
        self.state["events"] = self.state["events"][-500:]
        return event

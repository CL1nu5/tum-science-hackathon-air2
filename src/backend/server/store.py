from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# --- Shared geographic projection (must match the frontend) -----------------
# A local equirectangular projection around Munich's centre. Positions are
# stored in METRES with x east-positive and y north-positive. The dashboard
# converts these back to lon/lat and onto its Web-Mercator basemap, so the
# tower and the map agree on where everything is.
GEO_LAT0 = 48.137
GEO_LON0 = 11.576
GEO_M_PER_DEG_LAT = 111_320.0
GEO_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(GEO_LAT0))


def project(lat: float, lon: float) -> list[float]:
    """Real (lat, lon) -> local [x_east_m, y_north_m]."""
    return [
        round((lon - GEO_LON0) * GEO_M_PER_DEG_LON, 1),
        round((lat - GEO_LAT0) * GEO_M_PER_DEG_LAT, 1),
    ]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# Real Munich landing network (kept byte-compatible with
# src/frontend/landing-pads.csv) plus two dedicated emergency surfaces.
# Columns: id, name, short, lat, lon, stands, operator, surface_type, suitability
_INFRASTRUCTURE = [
    ("RDI", "Klinikum rechts der Isar", "RDI", 48.1370, 11.5996, 2, "AIR2", "vertiport", 1.0),
    ("GRH", "Klinikum Großhadern", "GRH", 48.1108, 11.4700, 2, "AIR2", "vertiport", 1.0),
    ("SWB", "München Klinik Schwabing", "SWB", 48.1786, 11.5707, 2, "AIR2", "vertiport", 1.0),
    ("BOG", "München Klinik Bogenhausen", "BOG", 48.1530, 11.6360, 2, "AIR2", "vertiport", 1.0),
    ("HAR", "München Klinik Harlaching", "HAR", 48.0915, 11.5565, 2, "AIR2", "vertiport", 1.0),
    ("TUM", "TUM City", "TUM", 48.1496, 11.5680, 4, "AIR2", "vertiport", 1.0),
    ("GAR", "TUM Garching", "GAR", 48.2648, 11.6715, 5, "AIR2", "vertiport", 1.0),
    ("BEG", "BMW Englischer Garten", "BEG", 48.1642, 11.6020, 3, "AIR2", "vertiport", 1.0),
    ("MSO", "Messestadt Ost", "MSO", 48.1340, 11.6960, 4, "AIR2", "vertiport", 1.0),
    ("LMU", "LMU", "LMU", 48.1505, 11.5805, 4, "AIR2", "vertiport", 1.0),
    ("BMT", "BMW Tower", "BMT", 48.1767, 11.5586, 4, "AIR2", "vertiport", 1.0),
    ("MUC", "Airport Munich", "MUC", 48.3538, 11.7861, 6, "OTHER", "vertiport", 1.0),
    ("ARE", "Allianz Arena", "ARE", 48.2188, 11.6247, 5, "OTHER", "vertiport", 1.0),
    ("PAS", "Pasing", "PAS", 48.1502, 11.4612, 4, "AIR2", "vertiport", 1.0),
    ("OST", "Ostbahnhof", "OST", 48.1270, 11.6048, 4, "AIR2", "vertiport", 1.0),
    ("SIE", "Siemens Werke", "SIE", 48.0853, 11.6363, 4, "AIR2", "vertiport", 1.0),
    ("GRW", "Grünwald", "GRW", 48.0707, 11.5267, 3, "AIR2", "vertiport", 1.0),
    # Dedicated emergency landing surfaces (no scheduled stands).
    ("EMER-01", "Prepared emergency surface", "EMR", 48.1310, 11.5470, 0, "PUBLIC", "light_red", 0.6),
    ("FIELD-01", "Open emergency field", "FLD", 48.0950, 11.6850, 0, "PUBLIC", "dark_red", 0.4),
]


def default_state() -> dict[str, Any]:
    vertiports: dict[str, Any] = {}
    stands: dict[str, Any] = {}
    for (vid, name, short, lat, lon, stand_count, operator, surface, suitability) in _INFRASTRUCTURE:
        vertiports[vid] = {
            "vertiport_id": vid,
            "name": name,
            "short": short,
            "lat": lat,
            "lon": lon,
            "position": project(lat, lon),
            "elevation_m": 0.0,
            "operator": operator,
            "surface_type": surface,
            "suitability_score": suitability,
            "pad_available": True,
            "active": True,
        }
        for index in range(stand_count):
            stand_id = f"{vid}-S{index + 1:02d}"
            stands[stand_id] = {
                "stand_id": stand_id,
                "vertiport_id": vid,
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

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
    ("RDI", "Klinikum rechts der Isar", "RDI", 48.1370, 11.5996, 4, "AIR2", "vertiport", 1.0),
    ("GRH", "Klinikum Großhadern", "GRH", 48.1108, 11.4700, 4, "AIR2", "vertiport", 1.0),
    ("SWB", "München Klinik Schwabing", "SWB", 48.1786, 11.5707, 4, "AIR2", "vertiport", 1.0),
    ("BOG", "München Klinik Bogenhausen", "BOG", 48.1530, 11.6360, 4, "AIR2", "vertiport", 1.0),
    ("HAR", "München Klinik Harlaching", "HAR", 48.0915, 11.5565, 4, "AIR2", "vertiport", 1.0),
    ("TUM", "TUM City", "TUM", 48.1496, 11.5680, 6, "AIR2", "vertiport", 1.0),
    ("GAR", "TUM Garching", "GAR", 48.2648, 11.6715, 7, "AIR2", "vertiport", 1.0),
    ("BEG", "BMW Englischer Garten", "BEG", 48.1642, 11.6020, 5, "AIR2", "vertiport", 1.0),
    ("MSO", "Messestadt Ost", "MSO", 48.1340, 11.6960, 6, "AIR2", "vertiport", 1.0),
    ("LMU", "LMU", "LMU", 48.1505, 11.5805, 6, "AIR2", "vertiport", 1.0),
    ("BMT", "BMW Tower", "BMT", 48.1767, 11.5586, 6, "AIR2", "vertiport", 1.0),
    ("MUC", "Airport Munich", "MUC", 48.3538, 11.7861, 8, "OTHER", "vertiport", 1.0),
    ("ARE", "Allianz Arena", "ARE", 48.2188, 11.6247, 7, "OTHER", "vertiport", 1.0),
    ("PAS", "Pasing", "PAS", 48.1502, 11.4612, 6, "AIR2", "vertiport", 1.0),
    ("OST", "Ostbahnhof", "OST", 48.1270, 11.6048, 6, "AIR2", "vertiport", 1.0),
    ("SIE", "Siemens Werke", "SIE", 48.0853, 11.6363, 6, "AIR2", "vertiport", 1.0),
    ("GRW", "Grünwald", "GRW", 48.0707, 11.5267, 5, "AIR2", "vertiport", 1.0),
    # Dedicated emergency landing surfaces (no scheduled stands).
    ("EMER-01", "Prepared emergency surface", "EMR", 48.1220, 11.5350, 0, "PUBLIC", "light_red", 0.6),
    ("FIELD-01", "Open emergency field", "FLD", 48.0950, 11.6850, 0, "PUBLIC", "dark_red", 0.4),
]


_AIRSPACE_ZONE_DEFS = [
    {
        "zone_id": "muc-north-runway",
        "name": "NO-FLY - MUC north runway",
        "kind": "nofly",
        "polygon_ll": [
            [48.3610, 11.7465],
            [48.3652, 11.7475],
            [48.3658, 11.8220],
            [48.3616, 11.8230],
        ],
        "avoidance_margin_m": 180.0,
        "penalty_weight": 0.0,
    },
    {
        "zone_id": "muc-south-runway",
        "name": "NO-FLY - MUC south runway",
        "kind": "nofly",
        "polygon_ll": [
            [48.3430, 11.7478],
            [48.3472, 11.7468],
            [48.3480, 11.8215],
            [48.3438, 11.8225],
        ],
        "avoidance_margin_m": 180.0,
        "penalty_weight": 0.0,
    },
    {
        "zone_id": "olympiapark",
        "name": "NO-FLY - Olympiapark event area",
        "kind": "nofly",
        "polygon_ll": [
            [48.1762, 11.5448],
            [48.1755, 11.5540],
            [48.1730, 11.5575],
            [48.1685, 11.5570],
            [48.1662, 11.5505],
            [48.1668, 11.5448],
            [48.1715, 11.5430],
        ],
        "avoidance_margin_m": 140.0,
        "penalty_weight": 0.0,
    },
    {
        "zone_id": "theresienwiese",
        "name": "NO-FLY - Theresienwiese event grounds",
        "kind": "nofly",
        "polygon_ll": [
            [48.1352, 11.5470],
            [48.1350, 11.5528],
            [48.1305, 11.5530],
            [48.1292, 11.5500],
            [48.1296, 11.5458],
            [48.1322, 11.5445],
        ],
        "avoidance_margin_m": 120.0,
        "penalty_weight": 0.0,
    },
    {
        "zone_id": "jva-stadelheim",
        "name": "NO-FLY - JVA Stadelheim",
        "kind": "nofly",
        "polygon_ll": [
            [48.1022, 11.5898],
            [48.1016, 11.5958],
            [48.0982, 11.5965],
            [48.0972, 11.5928],
            [48.0980, 11.5885],
            [48.1008, 11.5878],
        ],
        "avoidance_margin_m": 120.0,
        "penalty_weight": 0.0,
    },
    {
        "zone_id": "nymphenburg",
        "name": "NO-FLY - Nymphenburg palace gardens",
        "kind": "nofly",
        "polygon_ll": [
            [48.1620, 11.4908],
            [48.1638, 11.5060],
            [48.1580, 11.5120],
            [48.1510, 11.5100],
            [48.1490, 11.4938],
            [48.1548, 11.4865],
        ],
        "avoidance_margin_m": 140.0,
        "penalty_weight": 0.0,
    },
    {
        "zone_id": "englischer-garten",
        "name": "RESTRICT - Englischer Garten",
        "kind": "restrict",
        "polygon_ll": [
            [48.1438, 11.5862],
            [48.1455, 11.5985],
            [48.1570, 11.6005],
            [48.1690, 11.6008],
            [48.1800, 11.6005],
            [48.1810, 11.5950],
            [48.1730, 11.5895],
            [48.1600, 11.5872],
            [48.1495, 11.5865],
        ],
        "avoidance_margin_m": 0.0,
        "penalty_weight": 220.0,
    },
    {
        "zone_id": "altstadt",
        "name": "NOISE-CAP - Altstadt",
        "kind": "restrict",
        "polygon_ll": [
            [48.1395, 11.5655],
            [48.1422, 11.5690],
            [48.1427, 11.5776],
            [48.1379, 11.5840],
            [48.1352, 11.5793],
            [48.1335, 11.5675],
        ],
        "avoidance_margin_m": 0.0,
        "penalty_weight": 260.0,
    },
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
        "airspace_zones": default_airspace_zones(),
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


def default_airspace_zones() -> dict[str, Any]:
    return {
        zone["zone_id"]: {
            **zone,
            "polygon": [project(lat, lon) for lat, lon in zone["polygon_ll"]],
            "active": True,
        }
        for zone in _AIRSPACE_ZONE_DEFS
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
                zones = self.state.setdefault("airspace_zones", {})
                for zone_id, zone in default_airspace_zones().items():
                    zones.setdefault(zone_id, zone)
                self.persist_locked()
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
        state["airspace_zones"] = [
            item
            for item in state.get("airspace_zones", {}).values()
            if item.get("active", True)
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

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field


class VertiportUpsert(BaseModel):
    vertiport_id: str
    name: str
    position_x: float
    position_y: float
    elevation_m: float = 0.0
    operator: str = "AIR2"
    surface_type: str = "vertiport"
    suitability_score: float = Field(default=1.0, ge=0.0, le=1.0)
    pad_available: bool = True
    active: bool = True
    stand_count: int = Field(default=0, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WeatherCellCreate(BaseModel):
    center_x: float
    center_y: float
    radius_m: float = Field(gt=0)
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    active_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    active_until: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    )


class PadAvailabilityUpdate(BaseModel):
    available: bool


class NoiseZoneCreate(BaseModel):
    name: str
    center_x: float
    center_y: float
    radius_m: float = Field(gt=0)
    penalty_weight: float = Field(default=100.0, ge=0.0)
    max_active_overflights: int = Field(default=3, ge=0)

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.state_file = Path(os.getenv("AIR2_STATE_FILE", ".air2/state.json"))
        self.host = os.getenv("AIR2_HOST", "0.0.0.0")
        self.port = int(os.getenv("AIR2_PORT", "8000"))
        self.log_level = os.getenv("AIR2_LOG_LEVEL", "info")

        self.route_horizontal_margin_m = 120.0
        self.route_vertical_margin_m = 45.0
        self.route_time_buffer_s = 15.0
        self.route_sample_interval_s = 2.0
        self.route_lease_seconds = 180
        self.route_planning_timeout_s = 5.0
        self.max_route_delay_minutes = 20

        self.pad_occupancy_seconds = 90
        self.pad_buffer_seconds = 30
        self.slot_lease_seconds = 180

        self.heartbeat_interval_s = 2.0
        self.agent_stale_after_s = 10.0
        self.cleanup_interval_s = 5.0
        self.safety_reserve_pct = 15.0
        self.nominal_battery_drain_per_s = 0.004
        self.cruise_speed_ms = 55.0
        self.emergency_reachability_minutes = 30.0
        self.max_reroutes_per_flight = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()

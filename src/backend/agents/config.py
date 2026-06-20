from __future__ import annotations

import os

# --- V2V / proximity ---
# Munich vertiports sit ~10-20 km apart, so a 5 km handshake radius made *every*
# agent permanently "in conflict" with several peers and thrashed lateral offsets.
# Couple the trigger to a realistic near-miss distance instead.
CRITICAL_RADIUS_M: float = 600.0        # trigger V2V handshake below this distance
V2V_VERTICAL_SEP_M: float = 40.0        # ignore peers separated by more than this in altitude
BROADCAST_INTERVAL_S: float = 2.0       # how often agents send PositionBroadcast
STATE_UPDATE_INTERVAL_S: float = 2.0    # how often agents send StateUpdate to tower
V2V_PORT: int = 9000                    # UDP port for position broadcasts
V2V_WS_PORT: int = 9001                 # base TCP port for per-peer WS handshakes

# --- Battery ---
# Drain is sized so a cross-Munich leg (~4-8 min) uses a meaningful slice of the
# pack and long legs from a low start can dip into the emergency band. Must match
# Settings.nominal_battery_drain_per_s on the tower so reachability agrees.
BATTERY_DRAIN_PER_S: float = 0.08       # % per second, nominal flight
BATTERY_DRAIN_EMERGENCY: float = 0.12   # % per second, emergency mode
SAFETY_RESERVE_PCT: float = 15.0        # minimum battery required to count a vertiport as reachable
EMERGENCY_THRESHOLD_PCT: float = 20.0   # auto-declare Emergency below this level

# --- Route / approach ---
DESCEND_RADIUS_M: float = 2000.0        # switch to DESCENDING when this close to destination
FINAL_APPROACH_RADIUS_M: float = 500.0  # switch to FINAL_APPROACH
CLIMB_ALTITUDE_M: float = 150.0         # cruise altitude
APPROACH_SPEED_MS: float = 18.0         # speed during final approach (m/s)
CRUISE_SPEED_MS: float = 80.0           # nominal cruise speed (must match tower cruise_speed_ms)

# --- Holding (delay the landing approach when the pad/stand isn't ready) ---
# A taxi that reaches its approach fix without landing clearance loiters here
# (eVTOL hover-hold) rather than barging onto an occupied pad. Holding is
# implemented by sliding the unflown part of the reserved 4D schedule forward in
# time, so the taxi waits on its corridor and the dashboard stays consistent.
HOLD_ENTRY_RADIUS_M: float = 2100.0     # request clearance / begin holding just outside DESCEND_RADIUS
HOLD_RETRY_S: float = 3.0               # how often a holding taxi re-requests landing clearance
HOLD_MAX_S: float = 90.0               # divert to a backup vertiport after holding this long
LANDING_REQUEST_LEAD_S: float = 0.0     # (reserved) lead time before the fix to ask for clearance

# --- Tower ---
TOWER_WS_URL: str = "ws://localhost:8000/ws/agent/{agent_id}"
TOWER_RECONNECT_BACKOFF_S: float = 2.0  # initial reconnect backoff (doubles each attempt)
TOWER_RECONNECT_MAX_S: float = 30.0
TOWER_DEAD_TIMEOUT_S: float = 5.0       # seconds without ping before tower is considered down

# --- Emergency / preemption ---
HUMAN_TIMEOUT_S: float = 30.0           # hard timeout before escalating to human
EMERGENCY_REDECLARE_S: float = 12.0     # re-declare if a prior emergency stayed unresolved
MAX_REROUTE_PER_FLIGHT: int = 1         # bumped taxi is immune after this many reroutes
# Mean time between simulated in-flight faults PER taxi. The concept models
# "failure, low/empty battery, etc." as emergencies; with low organic battery
# pressure this keeps the emergency cascade visible in a short demo. Set to 0 to
# disable. ~1 fault every (MTBF / fleet_size) seconds across the fleet.
# Override with AIR2_FAULT_MTBF_S (e.g. 0 to disable, 60 for a busy demo).
# Default softened (1200 -> 3000) so a routine demo shows the cascade occasionally
# rather than dropping a third of the fleet into emergencies at once.
EMERGENCY_FAULT_MTBF_S: float = float(os.getenv("AIR2_FAULT_MTBF_S", "3000"))

# --- Continuous operation (keeps the live demo lively) ---
BATTERY_RECHARGE_PER_S: float = 2.5     # % per second while parked on a stand
REDISPATCH_MIN_BATTERY_PCT: float = 55.0  # wait until charged this much before flying again
DISPATCH_RETRY_S: float = 4.0           # backoff before retrying a rejected route request
PARKED_DWELL_S: float = 1.0             # minimum time parked before considering a new trip

# --- Simulation tick ---
SIM_TICK_S: float = 0.1                 # flight-loop resolution

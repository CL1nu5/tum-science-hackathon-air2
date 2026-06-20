from __future__ import annotations

import os

# --- V2V / proximity ---
CRITICAL_RADIUS_M: float = 5000.0       # trigger V2V handshake below this distance
BROADCAST_INTERVAL_S: float = 2.0       # how often agents send PositionBroadcast
STATE_UPDATE_INTERVAL_S: float = 5.0    # how often agents send StateUpdate to tower
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
EMERGENCY_FAULT_MTBF_S: float = float(os.getenv("AIR2_FAULT_MTBF_S", "1200"))

# --- Continuous operation (keeps the live demo lively) ---
BATTERY_RECHARGE_PER_S: float = 2.5     # % per second while parked on a stand
REDISPATCH_MIN_BATTERY_PCT: float = 55.0  # wait until charged this much before flying again
DISPATCH_RETRY_S: float = 4.0           # backoff before retrying a rejected route request
PARKED_DWELL_S: float = 3.0             # minimum time parked before considering a new trip

# --- Simulation tick ---
SIM_TICK_S: float = 0.1                 # flight-loop resolution

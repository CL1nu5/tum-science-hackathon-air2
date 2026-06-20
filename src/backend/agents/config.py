from __future__ import annotations

# --- V2V / proximity ---
CRITICAL_RADIUS_M: float = 5000.0       # trigger V2V handshake below this distance
BROADCAST_INTERVAL_S: float = 2.0       # how often agents send PositionBroadcast
STATE_UPDATE_INTERVAL_S: float = 5.0    # how often agents send StateUpdate to tower
V2V_PORT: int = 9000                    # UDP port for position broadcasts
V2V_WS_PORT: int = 9001                 # base TCP port for per-peer WS handshakes

# --- Battery ---
BATTERY_DRAIN_PER_S: float = 0.004      # % per second, nominal flight
BATTERY_DRAIN_EMERGENCY: float = 0.006  # % per second, emergency mode
SAFETY_RESERVE_PCT: float = 15.0        # minimum battery required to count a vertiport as reachable
EMERGENCY_THRESHOLD_PCT: float = 20.0   # auto-declare Emergency below this level

# --- Route / approach ---
DESCEND_RADIUS_M: float = 2000.0        # switch to DESCENDING when this close to destination
FINAL_APPROACH_RADIUS_M: float = 500.0  # switch to FINAL_APPROACH
CLIMB_ALTITUDE_M: float = 150.0         # cruise altitude
APPROACH_SPEED_MS: float = 15.0         # speed during final approach (m/s)
CRUISE_SPEED_MS: float = 55.0           # nominal cruise speed

# --- Tower ---
TOWER_WS_URL: str = "ws://localhost:8000/ws/agent/{agent_id}"
TOWER_RECONNECT_BACKOFF_S: float = 2.0  # initial reconnect backoff (doubles each attempt)
TOWER_RECONNECT_MAX_S: float = 30.0
TOWER_DEAD_TIMEOUT_S: float = 5.0       # seconds without ping before tower is considered down

# --- Emergency / preemption ---
HUMAN_TIMEOUT_S: float = 30.0           # hard timeout before escalating to human
MAX_REROUTE_PER_FLIGHT: int = 1         # bumped taxi is immune after this many reroutes

# --- Simulation tick ---
SIM_TICK_S: float = 0.1                 # flight-loop resolution

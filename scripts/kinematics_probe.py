"""Deterministic, network-free probe of RouteFollower.step() to pin the
altitude-hop and the mid-air freeze/divergence. We drive a single agent along a
realistic 4D route under several conditions and log altitude/position/speed.
"""
from __future__ import annotations

import time
import math

import src.backend.agents.routing as routing
from src.backend.agents.routing import RouteFollower
from src.backend.agents.state import AgentState, FlightStage, Waypoint

# Virtual clock so we can fast-forward the wall-clock the RouteFollower reads.
_VCLOCK = {"now": time.time()}
routing.time.time = lambda: _VCLOCK["now"]


def build_route(t0, origin=(0.0, 0.0, 0.0), dest=(5000.0, 0.0), alt=150.0, speed=80.0, delay_s=0.0):
    ox, oy, oz = origin
    tx, ty = dest
    dist = math.hypot(tx - ox, ty - oy)
    climb_s = max(6.0, abs(alt - oz) / 12.0)
    descent_s = max(6.0, abs(alt - 0.0) / 12.0)
    cruise_s = dist / max(speed, 1.0)
    start = t0 + delay_s
    return [
        Waypoint(ox, oy, oz, start),
        Waypoint(ox, oy, alt, start + climb_s),
        Waypoint((ox + tx) / 2, (oy + ty) / 2, alt, start + climb_s + cruise_s * 0.5),
        Waypoint(tx, ty, alt, start + climb_s + cruise_s),
        Waypoint(tx, ty, 0.0, start + climb_s + cruise_s + descent_s),
    ]


def run(label, *, lateral=0.0, start_offset=0.0, dt=0.1, ticks=1400):
    """start_offset: how far in the PAST the route's t0 is, i.e. agent is behind sched."""
    _VCLOCK["now"] = time.time()
    t0 = _VCLOCK["now"] - start_offset
    route = build_route(t0)
    s = AgentState(agent_id="T1")
    s.assigned_route = route
    s.position = (0.0, 0.0)
    s.altitude = 0.0
    s.flight_stage = FlightStage.CLIMBING
    s.speed = 80.0 * 0.6
    s.current_waypoint_idx = 0
    s.lateral_offset = lateral
    rf = RouteFollower()

    trace = []
    landed_tick = None
    for i in range(ticks):
        _VCLOCK["now"] += dt  # advance the virtual wall-clock the follower reads
        # mimic flight loop stage transitions (climb->en_route at cruise alt)
        if s.flight_stage == FlightStage.CLIMBING and s.altitude >= route[1].z - 1.0:
            s.flight_stage = FlightStage.EN_ROUTE
        upd = rf.step(s, dt)
        for k, v in upd.items():
            setattr(s, k, v)
        if i % 20 == 0:
            trace.append((round(i * dt, 1), round(s.altitude, 1), round(s.position[0], 0),
                          round(s.position[1], 0), round(s.speed, 1), s.flight_stage.name, s.current_waypoint_idx))
        if s.flight_stage == FlightStage.ON_PAD and landed_tick is None:
            landed_tick = i
            break
    print(f"\n===== {label} (lateral={lateral}, start_offset={start_offset}s) =====")
    print(" t(s)  alt   x      y      spd   stage           idx")
    for row in trace[:40]:
        print(f"{row[0]:5.1f} {row[1]:5.0f} {row[2]:6.0f} {row[3]:6.0f} {row[4]:5.1f}  {row[5]:14s} {row[6]}")
    if landed_tick is not None:
        print(f"  --> LANDED after {landed_tick*dt:.1f}s of sim time")
    else:
        print(f"  --> NEVER LANDED in {ticks*dt:.1f}s (FROZEN/DIVERGED). final pos=({s.position[0]:.0f},{s.position[1]:.0f}) alt={s.altitude:.0f} idx={s.current_waypoint_idx}")
    # altitude hop detection
    alts = [r[1] for r in trace]
    hops = sum(1 for j in range(1, len(alts)) if abs(alts[j] - alts[j-1]) > 50)
    print(f"  altitude >50m jumps between samples: {hops}; alt range [{min(alts):.0f}..{max(alts):.0f}]")


if __name__ == "__main__":
    # 1. Nominal: agent on schedule, no yielding -> should climb to 150 and land cleanly.
    run("NOMINAL on-schedule", lateral=0.0, start_offset=0.0)
    # 2. Behind schedule (took off 30s late relative to route t0) -> waypoints in past.
    run("BEHIND schedule by 30s", lateral=0.0, start_offset=30.0)
    # 3. Way behind schedule (route times all 200s in past, like EVX-102).
    run("BEHIND schedule by 200s", lateral=0.0, start_offset=200.0)
    # 4. Yielding (lateral offset stuck at 18m) near approach -> orbit?
    run("YIELDING lateral=18", lateral=18.0, start_offset=0.0)

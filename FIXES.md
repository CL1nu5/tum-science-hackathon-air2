# AIR2 eVTOL Tower — Deadlock, Landing & Altitude Fixes

This document is the concept + changelog for resolving the reported problems:
flight objects deadlocking with nowhere to land, no place to land before take-off,
no speed/holding control to delay an approach, and altitude "hopping" between 0 and
150 m. It records how each bug was diagnosed (with runtime evidence), the fix
concept, and the verification.

## TL;DR

| Symptom | Root cause | Fix | Verified |
|---|---|---|---|
| Fleet **deadlocks**, taxis pile up, nothing lands | RouteFollower paced speed = `distance / time_remaining` with a 0.5 m/s floor → an *on-schedule* taxi decelerates asymptotically and **freezes mid-air**, never reaching a waypoint, never landing, never freeing a stand | Follow the reserved 4D corridor by **time-interpolation** (same as the dashboard) — position is a continuous function of time, so motion always progresses | Telemetry: taxis now land & recycle continuously; kinematics probe lands in all cases |
| Taxis drift **kilometres off course** | V2V yield offset re-added (+18 m) to the *already-shifted* position every tick → unbounded lateral integration | Apply the offset as a **fixed perpendicular shift off the corridor centreline**, recomputed each tick; reset to 0 when no peer is near / on a new route | Lateral-offset probe lands on profile |
| **Altitude hops 0↔150 m** | Frozen taxis ran past their schedule; the dashboard clamps a past-schedule track to its last waypoint (z=0) while the backend held 150 m, then a fresh route jumped back to climb | Fixing the freeze makes taxis land & re-dispatch on time, so the corridor timeline stays continuous; altitude z interpolates smoothly | Headless browser: **0** rendered altitude teleports |
| **No place to land before take-off** | Planner reserved a pad time-slot but never checked stands; taxis launched toward full vertiports | **Stand-before-take-off admission control**: reject a route unless the destination has ≥1 projected free stand (free stands − inbound) | Capacity gate in `route_planner` |
| **Can't delay the landing approach** | Inbound taxis had no way to wait; they barged the pad (`ON_PAD→PARKED` unconditionally) | **Holding**: a taxi without a stand loiters (hover-hold by sliding the unflown schedule forward) and only descends once a stand is granted; diverts after a timeout | Telemetry: brief holds before every landing; no pad over-commit |
| Emergencies escalate to **human takeover** & batteries spiral to 0 | Low-battery emergencies couldn't reach any surface (full 15% reserve enforced) and re-declared every 12 s, resetting their route; airborne emergency routes got *delayed* departures and froze | Emergencies may dip into reserve to reach a surface; stop re-declaring once a landing route exists; **airborne reroutes never get a departure delay** | Stress test (20× faults): **11/11 emergencies resolved, 0 human takeovers** |
| `state.json` grows to ~500 KB | Inactive routes/slots were soft-deactivated but never deleted | Prune inactive routes/reservations in the cleanup loop | Stable at ~165 KB |

## 1. How the bugs were found

Two probes (kept under `scripts/`) made the failures reproducible:

- **`scripts/kinematics_probe.py`** — drives a single `RouteFollower` along a realistic
  4D route under a virtual clock. It showed the killer: an *on-schedule* taxi
  **never landed** — speed decayed to the 0.5 m/s floor and it stalled at the
  cruise waypoint. Paradoxically only a taxi 200 s *behind* schedule landed
  (it hit the fixed-speed fallback branch).
- **`scripts/telemetry_probe.py`** — polls the running tower and records per-agent
  altitude / stage / position. Before the fix: 21+ taxis frozen `EN_ROUTE`, ~0
  landings in 90 s, `pad_reservations` pinned at 30, one taxi drifted 12 km off
  route. Altitude traces showed the `^^^^___^^^^` 0↔150 hop.

A multi-agent code audit independently confirmed these root causes and surfaced
secondary bugs (stand double-allocation, no stand-free on disconnect, etc.).

## 2. The fix concept (matches `concept.md`)

1. **One source of truth for motion.** Both the tower simulation and the dashboard
   now place a taxi by interpolating its reserved 4D corridor `[x,y,z,t]` at the
   current time. There is no separate "chase" controller to desynchronise or
   stall. Climb/cruise/descent are a smooth ramp because z is interpolated.

2. **A place to land *before* take-off (concept §3/§4 guiding principle).** A taxi
   only launches toward a vertiport that has ≥1 reachable free place to land
   (`free_stands − inbound ≥ 1`). Otherwise the request is rejected and it retries
   another pad later — it never commits to a destination it can't clear into.

3. **Delay the approach with speed/holding (concept §3 pad-standby).** On arrival a
   taxi requests a stand. If none is free it **holds** (eVTOL hover-hold, realised
   by sliding the unflown tail of its schedule forward in time) instead of stacking
   onto an occupied pad, and only descends once a stand is granted. Held too long →
   it diverts to a backup vertiport (max one reroute, per concept §5).

4. **Land only with a stand.** `ON_PAD→PARKED` now requires an actual
   `LandingClearance` with a stand (or emergency standby). The pad stays clear.

5. **Emergencies always graduate to a surface (concept §5 cascade).** An emergency
   may use its reserve to reach the nearest surface, doesn't thrash re-declarations
   once it holds a route, and — being airborne — is never given a delayed departure.

## 3. Files changed

- `src/backend/agents/routing.py` — `RouteFollower` rewritten as a 4D
  time-interpolator (`interpolate_route_4d`); clean, non-compounding lateral offset.
- `src/backend/agents/agent.py` — landing coordination (`_coordinate_landing`,
  `_hold`, `_divert`); land-only-with-a-stand gate; V2V offset decay & altitude-aware
  yielding; gated emergency re-declaration.
- `src/backend/agents/config.py` — V2V radius 5000→600 m + vertical separation;
  holding constants; softer default fault MTBF (1200→3000).
- `src/backend/server/route_planner.py` — stand-before-take-off capacity gate;
  airborne reroutes skip the departure-delay ladder; earlier-departure scoring.
- `src/backend/server/slot_scheduler.py` — idempotent `assign_stand_locked`.
- `src/backend/server/state_service.py` + `config.py` — reduced emergency reserve.
- `src/backend/server/app.py` — prune inactive routes/slots; free stands & retire
  routes/slots for stale/disconnected agents.
- `src/backend/server/emergency.py` — emergency plans bypass the capacity gate.

## 4. Verification (30 taxis, default config)

- **No deadlock / no freeze**: continuous land → park → re-dispatch; ~15 landings
  per 100 s; stands cycle.
- **Altitude**: telemetry and the headless dashboard show **0** altitude teleports;
  smooth climb→cruise→descend→land.
- **Holding**: every landing is preceded by a brief stand-request hold; the pad is
  never over-committed.
- **Emergencies** (stress, `AIR2_FAULT_MTBF_S=150`): **11/11 resolved, 0 human
  takeovers**; min battery stays healthy.
- **State file** bounded (~165 KB vs ~500 KB before).

Reproduce:

```bash
rm -rf .air2
.venv/bin/python -m src.backend.demo 30           # tower + fleet + dashboard on :8000
.venv/bin/python scripts/telemetry_probe.py 100   # fleet dynamics
.venv/bin/python scripts/kinematics_probe.py      # single-taxi motion (all LAND)
.venv/bin/python scripts/verify_altitude_render.py # headless dashboard altitude check
```

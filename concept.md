# AIR2 Simulation Model

The project is a visual browser simulation, not a distributed control system.
All state lives in memory in `src/frontend/simulation.js`.

On every animation frame one update performs the complete model:

1. Move each airborne taxi along its locally planned route.
2. Drain airborne batteries and recharge parked taxis.
3. Detect nearby aircraft and make the less constrained aircraft yield.
4. Trigger and resolve low-energy emergency diversions.
5. Complete arrivals, short stand stays, and new departures.
6. Move the weather cell and emit dashboard events.

Routes are calculated on a small navigation grid. Hard no-fly areas block grid
cells, and a simplified A* path becomes the aircraft route. The dashboard uses
the same in-memory objects for metrics, event logs, route drawing, and aircraft
inspection, so there is no synchronization or transport layer.

This intentionally trades production coordination guarantees for a compact,
understandable hackathon simulation.

# AIR2 Tower — Autonomous eVTOL Coordination

A decentralized-first, tower-authoritative coordination system for a fleet of
autonomous electric air-taxis over a real map of Munich. It implements the
concept in [`concept.md`](concept.md) for the challenge in
[`challenge.md`](challenge.md): conflicts are designed out on the ground via 4D
route reservation, and the unavoidable rest is resolved locally and
autonomously between aircraft.

The project is one process for the tower, one process for the fleet, and a
single self-contained HTML dashboard the tower serves — no database, no build
step.

## What's inside

- **Tower server** (`src/backend/server`) — FastAPI + WebSocket. 4D corridor
  conflict checks, pad-slot leases with a serial-pad lifecycle, tower-authoritative
  emergency landing + preemption, noise spreading over residential zones,
  weather avoidance, agent heartbeats and reconnect sync. One async lock keeps
  route and pad changes consistent. State lives in memory and is persisted to
  `.air2/state.json`.
- **eVTOL agents** (`src/backend/agents`) — each taxi is a bundle of asyncio
  loops: a persistent tower link, V2V broadcast + handshake (with a tower-down
  fallback), a flight loop that follows the reserved 4D corridor, battery
  drain/recharge, ETA-driven slot lifecycle, and a dispatch loop that keeps the
  taxi flying new legs. The fleet discovers the real vertiport network from the
  tower's API at startup.
- **Live dashboard** (`src/frontend`) — a Munich map (real OpenStreetMap/CARTO
  basemap) that connects to the tower over `/ws/dashboard`, projects the real
  aircraft and vertiports onto the map, and animates each taxi along its actual
  reserved 4D corridor. Falls back to a self-contained demo simulation if the
  tower is unreachable (the badge in the top bar shows **LIVE** vs **DEMO**).

The tower and the dashboard share one geographic projection (a local
equirectangular frame around Munich, metres east/north of 48.137°N 11.576°E), so
everything lines up on the map.

## Run it

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run air2-demo
```

Then open **http://localhost:8000/** — the dashboard connects automatically and
shows the live fleet crossing Munich. `air2-demo` starts the tower and a
connected fleet (50 taxis) in one process; pass a different size with
`uv run air2-demo 18`.

Prefer two terminals (tower and fleet separately)?

```bash
uv run air2-server        # terminal 1 — tower + dashboard on :8000
uv run air2-fleet         # terminal 2 — demo fleet (optional: `uv run air2-fleet <ws-url> <count>`)
```

No `uv`? Use a plain virtualenv:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m src.backend.demo          # one-command demo
```

## Configuration

Settings use the `AIR2_` prefix:

```text
AIR2_STATE_FILE=.air2/state.json    # delete this file to reset to the default Munich network
AIR2_HOST=0.0.0.0
AIR2_PORT=8000
AIR2_LOG_LEVEL=info
```

## Interfaces

Dashboard: `http://localhost:8000/` · API docs: `http://localhost:8000/docs`

Transports:

```text
ws://localhost:8000/ws/agent/{agent_id}     # eVTOL agents
ws://localhost:8000/ws/dashboard            # dashboard (SNAPSHOT + STATE_CHANGED)
```

HTTP:

- `GET /api/snapshot` · `GET /api/agents` · `GET /api/vertiports` · `GET /api/routes`
- `GET /api/reservations` · `GET /api/events` · `GET /health`
- `POST /api/vertiports` · `POST /api/weather` · `POST /api/noise-zones`
- `PATCH /api/vertiports/{id}/pad`

## Verification

Self-checks (need the dev extras: `pip install playwright websockets && playwright install chromium`):

```bash
python -m src.backend.demo &                 # or air2-server + air2-fleet
python scripts/verify_dashboard.py           # browser: dashboard connects + tracks animate
python scripts/verify_emergency.py           # tower emergency cascade + preemption
```

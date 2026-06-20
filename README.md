# AIR2 Tower

The backend contains:

- A FastAPI Tower server with HTTP and WebSocket interfaces.
- A small JSON state file for aircraft, routes, slots, stands, weather, and events.
- One async lock to keep route and pad changes consistent.
- 4D corridor conflict checks.
- Pad leases, lifecycle protection, stand assignment, and emergency standby.
- Tower-authoritative emergency landing and preemption.
- Agent heartbeat and reconnect synchronization.

This is intentionally a one-process hackathon architecture. State is loaded into memory and written to `.air2/state.json` after changes. There is no SQL server, migration system, or Docker requirement.

## Start

Requirements: Python 3.11+ and `uv`.

```bash
uv sync
uv run air2-server
```

The service listens on `http://localhost:8000`. FastAPI documentation is at `/docs`.

To start the demo fleet in another terminal:

```bash
uv run air2-fleet
```

## Configuration

Settings use the `AIR2_` prefix. The state file can be changed with:

```text
AIR2_STATE_FILE=.air2/state.json
```

Delete the state file to reset the simulation to the included demo infrastructure.

## Interfaces

Agent transport:

```text
ws://localhost:8000/ws/agent/{agent_id}
```

Dashboard transport:

```text
ws://localhost:8000/ws/dashboard
```

HTTP:

- `GET /api/snapshot`
- `GET /api/agents`
- `GET /api/vertiports`
- `GET /api/routes`
- `GET /api/reservations`
- `GET /api/events`
- `POST /api/vertiports`
- `POST /api/weather`
- `POST /api/noise-zones`
- `PATCH /api/vertiports/{id}/pad`
- `GET /health`

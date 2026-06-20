# AIR2 Tower Simulation

A browser-only eVTOL traffic simulation over Munich. There is no backend,
database, API, WebSocket transport, agent process, package manager, or build
step.

## Run

Open [`src/frontend/Electrx Tower - Clean.dc.html`](src/frontend/Electrx%20Tower%20-%20Clean.dc.html)
directly in a browser.

If the browser restricts map assets for local files, serve the same directory
with any static file server:

```bash
cd src/frontend
python3 -m http.server 8000
```

Then open <http://localhost:8000/Electrx%20Tower%20-%20Clean.dc.html>.

## Structure

- `simulation.js` contains the complete simulation state and update logic.
- `Electrx Tower - Clean.dc.html` contains the dashboard markup, map projection,
  route finder, and canvas renderer.
- `support.js` is the small template runtime used by the dashboard.

The animation frame is the only runtime loop. Each frame advances aircraft,
battery levels, landing cycles, emergencies, weather, and local separation,
then draws the resulting state.

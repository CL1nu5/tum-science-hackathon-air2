"""One-command demo launcher: starts the Tower server and a connected eVTOL
fleet in a single process, then serves the live dashboard.

    uv run air2-demo            # 12 taxis (default)
    uv run air2-demo 18         # custom fleet size

Open the dashboard at http://localhost:8000/ — it connects to this tower
automatically and shows the live fleet. Press Ctrl-C to stop everything.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import urllib.request

import uvicorn

from .agents.fleet import make_fleet
from .server.app import app
from .server.config import get_settings

log = logging.getLogger("air2.demo")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # V2V peers open and drop short-lived handshake sockets constantly; that is
    # expected, so don't let the websockets library log every close as an error.
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    settings = get_settings()
    host, port = settings.host, settings.port
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    fleet_size = int(sys.argv[1]) if len(sys.argv) > 1 else 50

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=settings.log_level)
    )
    threading.Thread(target=server.run, name="tower", daemon=True).start()

    base = f"http://{connect_host}:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:  # noqa: BLE001 - server still booting
            time.sleep(0.5)
    else:
        raise SystemExit("Tower did not become healthy in time")

    log.info("Tower online — dashboard at %s/ (Ctrl-C to stop)", base)
    coordinator = make_fleet(
        tower_url=f"ws://{connect_host}:{port}/ws/agent/{{agent_id}}",
        fleet_size=fleet_size,
    )
    try:
        asyncio.run(coordinator.run())
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()

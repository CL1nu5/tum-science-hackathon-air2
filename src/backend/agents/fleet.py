from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .agent import EvtolAgent
from .config import TOWER_WS_URL
from .priority import VertiportInfo
from .state import AgentState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    agent_id: str
    initial_position: tuple[float, float]   # (x, y) metres from reference
    initial_altitude: float = 0.0
    initial_battery: float = 100.0
    initial_destination: str | None = None  # vertiport ID to fly to at startup
    tower_url: str | None = None
    destination_pool: list[str] | None = None


# ---------------------------------------------------------------------------
# FleetCoordinator
# ---------------------------------------------------------------------------

class FleetCoordinator:
    """
    Spawns and manages a fleet of EvtolAgent instances as asyncio tasks.

    Usage:
        coordinator = FleetCoordinator(configs, vertiport_candidates)
        asyncio.run(coordinator.run())
    """

    def __init__(
        self,
        fleet_config: list[AgentConfig],
        vertiport_candidates: list[VertiportInfo] | None = None,
        tower_url: str = TOWER_WS_URL,
    ) -> None:
        self.fleet_config = fleet_config
        self.vertiport_candidates = vertiport_candidates or []
        self.tower_url = tower_url
        self._agents: dict[str, EvtolAgent] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

    async def run(self) -> None:
        """Start all agents and run until cancelled."""
        log.info("FleetCoordinator starting %d agents", len(self.fleet_config))
        for cfg in self.fleet_config:
            self._spawn(cfg)

        try:
            await asyncio.gather(*self._tasks.values(), return_exceptions=False)
        except asyncio.CancelledError:
            log.info("Fleet coordinator shutting down")
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def spawn_agent(self, cfg: AgentConfig) -> EvtolAgent:
        """Dynamically add a new agent to the running fleet."""
        agent = self._spawn(cfg)
        return agent

    def get_fleet_state(self) -> list[AgentState]:
        """Return a snapshot of all agents' current state (for monitoring / API)."""
        return [a.state for a in self._agents.values()]

    def get_agent(self, agent_id: str) -> EvtolAgent | None:
        return self._agents.get(agent_id)

    # -- internals -----------------------------------------------------------

    def _spawn(self, cfg: AgentConfig) -> EvtolAgent:
        agent = EvtolAgent(
            agent_id=cfg.agent_id,
            initial_position=cfg.initial_position,
            initial_altitude=cfg.initial_altitude,
            initial_battery=cfg.initial_battery,
            tower_url=cfg.tower_url or self.tower_url,
            vertiport_candidates=self.vertiport_candidates,
            destination_pool=cfg.destination_pool,
        )
        if cfg.initial_destination:
            agent.set_destination(cfg.initial_destination)

        self._agents[cfg.agent_id] = agent
        task = asyncio.create_task(agent.run(), name=f"agent-{cfg.agent_id}")
        self._tasks[cfg.agent_id] = task
        log.info("Spawned agent %s → %s", cfg.agent_id, cfg.initial_destination)
        return agent


# ---------------------------------------------------------------------------
# Infrastructure discovery — learn the real vertiport network from the tower
# ---------------------------------------------------------------------------

def _http_base_from_ws(tower_url: str) -> str:
    """ws://host:port/ws/agent/{id} -> http://host:port"""
    base = tower_url.split("/ws/")[0]
    base = base.replace("wss://", "https://").replace("ws://", "http://")
    return base.rstrip("/")


def _fetch_vertiports(http_base: str, attempts: int = 20) -> list[dict]:
    """Poll the tower's HTTP API until the vertiport list is available."""
    url = f"{http_base}/api/vertiports"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - tower may still be starting
            log.info("Waiting for tower at %s (%s)", url, exc)
            time.sleep(1.0)
    raise RuntimeError(f"Could not reach the tower at {http_base} after {attempts} tries")


def _candidates_from_vertiports(vertiports: list[dict]) -> list[VertiportInfo]:
    candidates = []
    for port in vertiports:
        if not port.get("active", True):
            continue
        x, y = port["position"][0], port["position"][1]
        candidates.append(
            VertiportInfo(
                vertiport_id=port["vertiport_id"],
                distance_m=0.0,
                has_free_slot=True,
                is_own_operator=port.get("operator") == "AIR2",
                surface_type=port.get("surface_type", "vertiport"),
                x=x,
                y=y,
                name=port.get("name", port["vertiport_id"]),
            )
        )
    return candidates


def make_fleet(
    tower_url: str = TOWER_WS_URL,
    fleet_size: int = 16,
) -> FleetCoordinator:
    """Build a fleet from the tower's real vertiport network."""
    http_base = _http_base_from_ws(tower_url)
    vertiports = _fetch_vertiports(http_base)
    candidates = _candidates_from_vertiports(vertiports)

    home_pads = [c for c in candidates if c.is_own_operator and c.surface_type == "vertiport"]
    destination_pool = [c.vertiport_id for c in home_pads]
    if len(home_pads) < 2:
        raise RuntimeError("Need at least two AIR2 vertiports to run a fleet")

    log.info(
        "Discovered %d vertiports (%d AIR2 pads) from the tower",
        len(candidates), len(home_pads),
    )

    configs: list[AgentConfig] = []
    for i in range(fleet_size):
        home = random.choice(home_pads)
        destination = random.choice([p for p in home_pads if p.vertiport_id != home.vertiport_id])
        # Start parked on a real pad with a small jitter so markers don't overlap.
        start = (
            home.x + random.uniform(-150.0, 150.0),
            home.y + random.uniform(-150.0, 150.0),
        )
        configs.append(
            AgentConfig(
                agent_id=f"EVX-{101 + i}",
                initial_position=start,
                initial_battery=random.uniform(62.0, 100.0),
                initial_destination=destination.vertiport_id,
                destination_pool=destination_pool,
            )
        )
    return FleetCoordinator(configs, candidates, tower_url)


# Backwards-compatible alias.
def _make_demo_fleet(tower_url: str = TOWER_WS_URL) -> FleetCoordinator:
    return make_fleet(tower_url=tower_url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Expected V2V short-lived handshake closes shouldn't spam as errors.
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

    tower = sys.argv[1] if len(sys.argv) > 1 else TOWER_WS_URL
    fleet_size = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    coordinator = make_fleet(tower_url=tower, fleet_size=fleet_size)

    try:
        asyncio.run(coordinator.run())
    except KeyboardInterrupt:
        log.info("Fleet stopped by user")


if __name__ == "__main__":
    main()

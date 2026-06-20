from __future__ import annotations

import asyncio
import logging
import random
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
        )
        if cfg.initial_destination:
            agent.set_destination(cfg.initial_destination)

        self._agents[cfg.agent_id] = agent
        task = asyncio.create_task(agent.run(), name=f"agent-{cfg.agent_id}")
        self._tasks[cfg.agent_id] = task
        log.info("Spawned agent %s → %s", cfg.agent_id, cfg.initial_destination)
        return agent


# ---------------------------------------------------------------------------
# Minimal demo fleet for standalone testing
# ---------------------------------------------------------------------------

def _make_demo_fleet(tower_url: str = TOWER_WS_URL) -> FleetCoordinator:
    """
    16-agent demo fleet roughly matching the frontend simulation.
    Positions are in metres from a Munich city-centre reference point.
    """
    VERTIPORTS: list[VertiportInfo] = [
        VertiportInfo("VP-01", distance_m=3200, has_free_slot=True, is_own_operator=True, surface_type="vertiport"),
        VertiportInfo("VP-02", distance_m=4100, has_free_slot=True, is_own_operator=True, surface_type="vertiport"),
        VertiportInfo("VP-03", distance_m=2700, has_free_slot=False, is_own_operator=True, surface_type="vertiport"),
        VertiportInfo("VP-04", distance_m=5500, has_free_slot=True, is_own_operator=False, surface_type="vertiport"),
        VertiportInfo("EMER-01", distance_m=1800, has_free_slot=True, is_own_operator=False, surface_type="light_red"),
        VertiportInfo("FIELD-01", distance_m=6200, has_free_slot=True, is_own_operator=False, surface_type="dark_red"),
    ]

    configs = [
        AgentConfig(f"EVX-{100 + i}", (random.uniform(-5000, 5000), random.uniform(-5000, 5000)),
                    initial_battery=random.uniform(60, 100),
                    initial_destination=f"VP-0{(i % 4) + 1}")
        for i in range(1, 17)
    ]
    return FleetCoordinator(configs, VERTIPORTS, tower_url)


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

    tower = sys.argv[1] if len(sys.argv) > 1 else TOWER_WS_URL
    coordinator = _make_demo_fleet(tower_url=tower)

    try:
        asyncio.run(coordinator.run())
    except KeyboardInterrupt:
        log.info("Fleet stopped by user")


if __name__ == "__main__":
    main()

"""
agents — multi-agent eVTOL layer for the Electrx Tower system.

Public surface:
    EvtolAgent       — single autonomous aircraft agent
    FleetCoordinator — spawns and manages N agents as asyncio tasks
    AgentConfig      — configuration dataclass for one agent
    AgentState       — full runtime state of an agent
"""

from .agent import EvtolAgent
from .fleet import AgentConfig, FleetCoordinator
from .state import AgentState, AgentStatus, FlightStage, SlotStage

__all__ = [
    "EvtolAgent",
    "FleetCoordinator",
    "AgentConfig",
    "AgentState",
    "AgentStatus",
    "FlightStage",
    "SlotStage",
]

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass
class BaseMessage:
    type: str
    timestamp: str = field(default_factory=_now_iso)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "BaseMessage":
        data: dict[str, Any] = json.loads(raw)
        return _dispatch(data)


# ---------------------------------------------------------------------------
# Agent → All  (UDP broadcast)
# ---------------------------------------------------------------------------

@dataclass
class PositionBroadcast(BaseMessage):
    type: str = "POSITION_BROADCAST"
    agent_id: str = ""
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])  # [x, y] metres
    altitude: float = 0.0
    velocity: list[float] = field(default_factory=lambda: [0.0, 0.0])  # [vx, vy] m/s
    speed: float = 0.0
    battery_pct: float = 100.0
    status: str = "NORMAL"          # "NORMAL" | "EMERGENCY"
    flight_stage: str = "PARKED"


# ---------------------------------------------------------------------------
# Agent → Tower
# ---------------------------------------------------------------------------

@dataclass
class StateUpdate(BaseMessage):
    type: str = "STATE_UPDATE"
    agent_id: str = ""
    battery_pct: float = 100.0
    speed: float = 0.0
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    altitude: float = 0.0
    route: list[list[float]] = field(default_factory=list)   # [[x,y,z,t], ...]
    intent: str = "PARKED"                                   # FlightStage name
    slot_stage: str = "NONE"
    destination_vertiport: str | None = None
    priority_metric: float = 0.0
    reachable_options: list[str] = field(default_factory=list)


@dataclass
class RouteRequest(BaseMessage):
    type: str = "ROUTE_REQUEST"
    agent_id: str = ""
    origin: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # [x,y,z]
    destination_vertiport: str = ""
    departure_time: str = field(default_factory=_now_iso)
    battery_pct: float = 100.0
    speed_capability: float = 55.0


@dataclass
class LockRequest(BaseMessage):
    type: str = "LOCK_REQUEST"
    agent_id: str = ""
    vertiport_id: str = ""
    requested_time: str = field(default_factory=_now_iso)
    priority_metric: float = 0.0
    status: str = "NORMAL"


@dataclass
class LockRelease(BaseMessage):
    type: str = "LOCK_RELEASE"
    agent_id: str = ""
    vertiport_id: str = ""
    slot_time: str = ""


@dataclass
class EmergencyDeclaration(BaseMessage):
    type: str = "EMERGENCY_DECLARATION"
    agent_id: str = ""
    battery_pct: float = 0.0
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    altitude: float = 0.0
    reachable_options: list[str] = field(default_factory=list)
    priority_metric: float = 1.0
    preempt_target: str | None = None   # vertiport ID being preempted (if any)


# ---------------------------------------------------------------------------
# Tower → Agent
# ---------------------------------------------------------------------------

@dataclass
class RouteAssignment(BaseMessage):
    type: str = "ROUTE_ASSIGNMENT"
    agent_id: str = ""
    corridor_id: str = ""
    waypoints: list[list[float]] = field(default_factory=list)  # [[x,y,z,t], ...]
    departure_time: str = field(default_factory=_now_iso)
    eta: str = field(default_factory=_now_iso)


@dataclass
class LockGrant(BaseMessage):
    type: str = "LOCK_GRANT"
    agent_id: str = ""
    vertiport_id: str = ""
    slot_time: str = ""
    stand_id: str | None = None


@dataclass
class PreemptNotice(BaseMessage):
    """Tower informs victim it is being preempted and must replan."""
    type: str = "PREEMPT_NOTICE"
    agent_id: str = ""              # victim
    by_agent_id: str = ""           # attacker
    vertiport_id: str = ""
    slot_time: str = ""
    backup_options: list[str] = field(default_factory=list)  # suggested alternatives


# ---------------------------------------------------------------------------
# Agent ↔ Agent  (V2V handshake)
# ---------------------------------------------------------------------------

@dataclass
class HandshakeInit(BaseMessage):
    type: str = "HANDSHAKE_INIT"
    from_agent: str = ""
    to_agent: str = ""
    distance_m: float = 0.0
    battery_pct: float = 100.0
    speed: float = 0.0
    route: list[list[float]] = field(default_factory=list)
    intent: str = "EN_ROUTE"
    status: str = "NORMAL"
    priority_metric: float = 0.0
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    altitude: float = 0.0
    velocity: list[float] = field(default_factory=lambda: [0.0, 0.0])


@dataclass
class HandshakeAck(BaseMessage):
    type: str = "HANDSHAKE_ACK"
    from_agent: str = ""
    to_agent: str = ""
    accepted: bool = True
    battery_pct: float = 100.0
    speed: float = 0.0
    route: list[list[float]] = field(default_factory=list)
    intent: str = "EN_ROUTE"
    status: str = "NORMAL"
    priority_metric: float = 0.0
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    altitude: float = 0.0
    velocity: list[float] = field(default_factory=lambda: [0.0, 0.0])
    # if the ack-sender will yield, it tells the init-sender so both sides agree
    i_will_yield: bool = False


# ---------------------------------------------------------------------------
# Internal sentinel (never serialised over the wire)
# ---------------------------------------------------------------------------

@dataclass
class TowerDown(BaseMessage):
    type: str = "TOWER_DOWN"


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, type] = {
    "POSITION_BROADCAST":   PositionBroadcast,
    "STATE_UPDATE":         StateUpdate,
    "ROUTE_REQUEST":        RouteRequest,
    "ROUTE_ASSIGNMENT":     RouteAssignment,
    "LOCK_REQUEST":         LockRequest,
    "LOCK_GRANT":           LockGrant,
    "LOCK_RELEASE":         LockRelease,
    "EMERGENCY_DECLARATION": EmergencyDeclaration,
    "HANDSHAKE_INIT":       HandshakeInit,
    "HANDSHAKE_ACK":        HandshakeAck,
    "PREEMPT_NOTICE":       PreemptNotice,
    "TOWER_DOWN":           TowerDown,
}


def _dispatch(data: dict[str, Any]) -> BaseMessage:
    msg_type = data.get("type", "")
    cls = _TYPE_MAP.get(msg_type, BaseMessage)
    # Only pass fields that exist in the dataclass
    import dataclasses
    known = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in known}
    return cls(**filtered)


def parse_message(raw: str | bytes) -> BaseMessage:
    """Parse a raw JSON string/bytes into the appropriate message dataclass."""
    data: dict[str, Any] = json.loads(raw)
    return _dispatch(data)

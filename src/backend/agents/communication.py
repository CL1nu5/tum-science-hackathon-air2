from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .config import (
    BROADCAST_INTERVAL_S,
    CRITICAL_RADIUS_M,
    TOWER_RECONNECT_BACKOFF_S,
    TOWER_RECONNECT_MAX_S,
    TOWER_DEAD_TIMEOUT_S,
    V2V_PORT,
    V2V_WS_PORT,
)
from .messages import BaseMessage, HandshakeAck, HandshakeInit, PositionBroadcast, parse_message
from .routing import distance_2d
from .state import AgentState

log = logging.getLogger(__name__)

MessageHandler = Callable[[BaseMessage], Awaitable[None]]


# ---------------------------------------------------------------------------
# TowerClient — persistent WebSocket connection to the Tower
# ---------------------------------------------------------------------------

class TowerClient:
    """
    Manages the WebSocket connection to the tower server.

    Usage:
        client = TowerClient(url, agent_id)
        client.subscribe("ROUTE_ASSIGNMENT", my_handler)
        await client.run()          # starts loop; call as asyncio task
        await client.send(msg)      # send a message
    """

    def __init__(self, url: str, agent_id: str) -> None:
        self.url = url
        self.agent_id = agent_id
        self.tower_alive: bool = False
        self._ws: Any = None   # websockets.ClientConnection when connected
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._send_queue: asyncio.Queue[str] = asyncio.Queue()
        self._last_pong: float = 0.0
        self._reconnect_event = asyncio.Event()

    # -- public API ----------------------------------------------------------

    def subscribe(self, msg_type: str, handler: MessageHandler) -> None:
        self._handlers.setdefault(msg_type, []).append(handler)

    async def send(self, message: BaseMessage) -> None:
        await self._send_queue.put(message.to_json())

    async def run(self) -> None:
        """Main loop — runs forever, reconnecting on failure."""
        backoff = TOWER_RECONNECT_BACKOFF_S
        while True:
            try:
                await self._connect_and_pump(backoff)
                backoff = TOWER_RECONNECT_BACKOFF_S  # reset on clean disconnect
            except Exception as exc:
                log.warning("[%s] Tower disconnected: %s", self.agent_id, exc)
                self.tower_alive = False
                await self._fire("TOWER_DOWN", _make_tower_down())
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, TOWER_RECONNECT_MAX_S)

    # -- internal ------------------------------------------------------------

    async def _connect_and_pump(self, backoff: float) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            log.error("websockets package not installed; tower comms disabled")
            await asyncio.sleep(60)
            return

        log.info("[%s] Connecting to tower at %s", self.agent_id, self.url)
        async with websockets.connect(self.url) as ws:
            self._ws = ws
            self.tower_alive = True
            self._last_pong = time.monotonic()
            log.info("[%s] Connected to tower", self.agent_id)

            recv_task = asyncio.create_task(self._recv_loop(ws))
            send_task = asyncio.create_task(self._send_loop(ws))
            watch_task = asyncio.create_task(self._watchdog())

            done, pending = await asyncio.wait(
                {recv_task, send_task, watch_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            self._last_pong = time.monotonic()
            try:
                msg = parse_message(raw)
            except Exception as exc:
                log.debug("[%s] Unparseable tower message: %s", self.agent_id, exc)
                continue
            await self._fire(msg.type, msg)

    async def _send_loop(self, ws: Any) -> None:
        while True:
            payload = await self._send_queue.get()
            try:
                await ws.send(payload)
            except Exception as exc:
                log.warning("[%s] Send to tower failed: %s", self.agent_id, exc)
                raise

    async def _watchdog(self) -> None:
        """Declare tower dead if no message received for TOWER_DEAD_TIMEOUT_S."""
        while True:
            await asyncio.sleep(1.0)
            if time.monotonic() - self._last_pong > TOWER_DEAD_TIMEOUT_S:
                raise ConnectionError("Tower watchdog timeout")

    async def _fire(self, msg_type: str, msg: BaseMessage) -> None:
        for handler in self._handlers.get(msg_type, []):
            try:
                await handler(msg)
            except Exception as exc:
                log.exception("[%s] Handler for %s raised: %s", self.agent_id, msg_type, exc)


def _make_tower_down() -> BaseMessage:
    from .messages import TowerDown
    return TowerDown()


# ---------------------------------------------------------------------------
# V2VNetwork — UDP broadcast + per-peer WebSocket handshake
# ---------------------------------------------------------------------------

class V2VNetwork:
    """
    Two-layer V2V communication:
      - UDP broadcast: periodic PositionBroadcast to all agents on the LAN
      - WebSocket: per-peer channel opened when distance < CRITICAL_RADIUS_M

    When tower is down (tower_client.tower_alive == False), V2VNetwork also
    handles lock negotiation using the same priority rule (concept §6).
    """

    BROADCAST_ADDR = "255.255.255.255"

    def __init__(
        self,
        agent_id: str,
        tower_client: TowerClient,
    ) -> None:
        self.agent_id = agent_id
        self.tower_client = tower_client
        self.nearby_agents: dict[str, AgentState] = {}  # agent_id → last known state
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._my_state_ref: AgentState | None = None    # set by EvtolAgent each tick

    def subscribe(self, msg_type: str, handler: MessageHandler) -> None:
        self._handlers.setdefault(msg_type, []).append(handler)

    def update_own_state(self, state: AgentState) -> None:
        self._my_state_ref = state

    def find_nearby(
        self,
        position: tuple[float, float],
        radius_m: float = CRITICAL_RADIUS_M,
    ) -> list[AgentState]:
        return [
            s for s in self.nearby_agents.values()
            if distance_2d(position, s.position) < radius_m
        ]

    # -- tasks ---------------------------------------------------------------

    async def run_broadcast(self) -> None:
        """Send PositionBroadcast over UDP every BROADCAST_INTERVAL_S."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(BROADCAST_INTERVAL_S)
                if self._my_state_ref is None:
                    continue
                s = self._my_state_ref
                msg = PositionBroadcast(
                    agent_id=s.agent_id,
                    position=list(s.position),
                    altitude=s.altitude,
                    velocity=list(s.velocity),
                    speed=s.speed,
                    battery_pct=s.battery_pct,
                    status=s.status.value,
                    flight_stage=s.flight_stage.name,
                )
                payload = msg.to_json().encode()
                try:
                    await loop.run_in_executor(
                        None,
                        lambda: sock.sendto(payload, (self.BROADCAST_ADDR, V2V_PORT)),
                    )
                except Exception as exc:
                    log.debug("[%s] UDP broadcast failed: %s", self.agent_id, exc)
        finally:
            sock.close()

    async def run_listener(self) -> None:
        """Listen for incoming UDP broadcasts from other agents."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass  # Windows doesn't have SO_REUSEPORT
        sock.bind(("", V2V_PORT))
        sock.setblocking(False)

        while True:
            try:
                raw, _ = await loop.run_in_executor(None, lambda: sock.recvfrom(4096))
            except Exception:
                await asyncio.sleep(0.05)
                continue

            try:
                msg = parse_message(raw)
            except Exception:
                continue

            if isinstance(msg, PositionBroadcast) and msg.agent_id != self.agent_id:
                await self._handle_position_broadcast(msg)

    async def run_handshake_server(self) -> None:
        """Accept incoming V2V WebSocket handshakes from nearby peers."""
        try:
            import websockets  # type: ignore
        except ImportError:
            log.warning("websockets not installed; V2V WS handshake server disabled")
            return

        port = V2V_WS_PORT + _agent_port_offset(self.agent_id)
        log.info("[%s] V2V WS server listening on port %d", self.agent_id, port)

        async def _on_conn(ws: Any) -> None:
            async for raw in ws:
                try:
                    msg = parse_message(raw)
                    await self._fire(msg.type, msg)
                except Exception as exc:
                    log.debug("[%s] V2V WS parse error: %s", self.agent_id, exc)

        async with websockets.serve(_on_conn, "0.0.0.0", port):
            await asyncio.Future()  # serve forever

    async def send_handshake(
        self,
        peer_agent_id: str,
        peer_port: int,
        msg: HandshakeInit | HandshakeAck,
    ) -> None:
        """Open a short-lived WebSocket to a peer and send a handshake message."""
        try:
            import websockets  # type: ignore
        except ImportError:
            return

        url = f"ws://localhost:{peer_port}"
        try:
            async with websockets.connect(url, open_timeout=2) as ws:
                await ws.send(msg.to_json())
        except Exception as exc:
            log.debug(
                "[%s] Could not reach peer %s on %s: %s",
                self.agent_id, peer_agent_id, url, exc,
            )

    # -- internals -----------------------------------------------------------

    async def _handle_position_broadcast(self, msg: PositionBroadcast) -> None:
        from .state import AgentStatus, FlightStage

        # Update our picture of this agent
        peer_state = self.nearby_agents.get(msg.agent_id)
        if peer_state is None:
            # Create a minimal stub; priority_metric will be set when handshake happens
            from .state import AgentState, SlotStage
            peer_state = AgentState(agent_id=msg.agent_id)
            self.nearby_agents[msg.agent_id] = peer_state

        peer_state.position = tuple(msg.position[:2])  # type: ignore[assignment]
        peer_state.altitude = msg.altitude
        peer_state.velocity = tuple(msg.velocity[:2])  # type: ignore[assignment]
        peer_state.speed = msg.speed
        peer_state.battery_pct = msg.battery_pct
        peer_state.status = AgentStatus[msg.status]
        try:
            peer_state.flight_stage = FlightStage[msg.flight_stage]
        except KeyError:
            pass

        await self._fire("POSITION_BROADCAST", msg)

        # Check proximity and initiate handshake if needed
        if self._my_state_ref is not None:
            dist = distance_2d(self._my_state_ref.position, peer_state.position)
            if dist < CRITICAL_RADIUS_M:
                await self._initiate_handshake(peer_state, dist)

    async def _initiate_handshake(self, peer: AgentState, dist_m: float) -> None:
        if self._my_state_ref is None:
            return
        s = self._my_state_ref
        init = HandshakeInit(
            from_agent=self.agent_id,
            to_agent=peer.agent_id,
            distance_m=dist_m,
            battery_pct=s.battery_pct,
            speed=s.speed,
            route=[[w.x, w.y, w.z, w.t] for w in s.assigned_route],
            intent=s.flight_stage.name,
            status=s.status.value,
            priority_metric=s.priority_metric,
            position=list(s.position),
            altitude=s.altitude,
            velocity=list(s.velocity),
        )
        peer_port = V2V_WS_PORT + _agent_port_offset(peer.agent_id)
        await self.send_handshake(peer.agent_id, peer_port, init)

    async def _fire(self, msg_type: str, msg: BaseMessage) -> None:
        for handler in self._handlers.get(msg_type, []):
            try:
                await handler(msg)
            except Exception as exc:
                log.exception("[%s] V2V handler for %s raised: %s", self.agent_id, msg_type, exc)


def _agent_port_offset(agent_id: str) -> int:
    """Deterministic port offset from agent ID so each agent has a unique WS port."""
    return abs(hash(agent_id)) % 1000

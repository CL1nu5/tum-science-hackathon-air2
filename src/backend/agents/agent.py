from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .communication import TowerClient, V2VNetwork
from .config import (
    BATTERY_DRAIN_EMERGENCY,
    BATTERY_DRAIN_PER_S,
    BROADCAST_INTERVAL_S,
    CRUISE_SPEED_MS,
    EMERGENCY_THRESHOLD_PCT,
    HUMAN_TIMEOUT_S,
    MAX_REROUTE_PER_FLIGHT,
    STATE_UPDATE_INTERVAL_S,
    SIM_TICK_S,
    TOWER_WS_URL,
    V2V_PORT,
    V2V_WS_PORT,
)
from .messages import (
    EmergencyDeclaration,
    EmergencyResolution,
    HandshakeAck,
    HandshakeInit,
    Heartbeat,
    HeartbeatAck,
    LandingClearance,
    LockGrant,
    LockRelease,
    LockRequest,
    PositionBroadcast,
    PreemptNotice,
    RouteAssignment,
    RouteRequest,
    StateUpdate,
    SyncRequest,
    SyncState,
    TowerDown,
)
from .priority import (
    VertiportInfo,
    compute_priority_metric,
)
from .routing import RouteFollower, V2VDeconfliction, build_route_request_payload, waypoints_from_assignment
from .state import AgentState, AgentStatus, FlightStage, SlotStage, Waypoint

log = logging.getLogger(__name__)


class EvtolAgent:
    """
    Autonomous eVTOL agent.

    Runs as a collection of concurrent asyncio tasks:
      - Tower WebSocket connection (TowerClient)
      - V2V broadcast + listener + handshake server (V2VNetwork)
      - Flight loop (advance along route, lifecycle transitions)
      - Battery drain loop
      - Periodic state-update to tower
      - Slot lifecycle manager (FIRM_FAR → FIRM_NEAR → FINAL_APPROACH)
    """

    def __init__(
        self,
        agent_id: str,
        initial_position: tuple[float, float],
        initial_altitude: float = 0.0,
        initial_battery: float = 100.0,
        tower_url: str | None = None,
        vertiport_candidates: list[VertiportInfo] | None = None,
    ) -> None:
        self.state = AgentState(
            agent_id=agent_id,
            position=initial_position,
            altitude=initial_altitude,
            battery_pct=initial_battery,
            speed=0.0,
            flight_stage=FlightStage.PARKED,
        )
        url = (tower_url or TOWER_WS_URL).format(agent_id=agent_id)
        self.tower = TowerClient(url, agent_id)
        self.v2v = V2VNetwork(agent_id, self.tower)
        self._route_follower = RouteFollower()
        self._vertiport_candidates: list[VertiportInfo] = vertiport_candidates or []
        self._pending_destination: str | None = None
        self._emergency_handled: bool = False
        self._human_escalation_timer: float | None = None
        self._route_granted: bool = False
        self._slot_granted: bool = False

        self._register_tower_handlers()
        self._register_v2v_handlers()

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """Start all concurrent loops. This coroutine runs indefinitely."""
        log.info("[%s] Starting agent", self.state.agent_id)
        await asyncio.gather(
            self.tower.run(),
            self.v2v.run_broadcast(),
            self.v2v.run_listener(),
            self.v2v.run_handshake_server(),
            self._flight_loop(),
            self._battery_loop(),
            self._state_update_loop(),
            self._slot_lifecycle_loop(),
        )

    def set_destination(self, vertiport_id: str) -> None:
        """Set (or update) the destination. Will trigger a route request next tick."""
        self._pending_destination = vertiport_id

    # -----------------------------------------------------------------------
    # Tower message handlers
    # -----------------------------------------------------------------------

    def _register_tower_handlers(self) -> None:
        self.tower.subscribe("ROUTE_ASSIGNMENT", self._on_route_assignment)
        self.tower.subscribe("LOCK_GRANT", self._on_lock_grant)
        self.tower.subscribe("LANDING_CLEARANCE", self._on_landing_clearance)
        self.tower.subscribe("EMERGENCY_RESOLUTION", self._on_emergency_resolution)
        self.tower.subscribe("PREEMPT_NOTICE", self._on_preempt_notice)
        self.tower.subscribe("HEARTBEAT", self._on_heartbeat)
        self.tower.subscribe("SYNC_REQUEST", self._on_sync_request)
        self.tower.subscribe("TOWER_DOWN", self._on_tower_down)

    async def _on_route_assignment(self, msg: RouteAssignment) -> None:
        log.info("[%s] Route assigned via corridor %s", self.state.agent_id, msg.corridor_id)
        waypoints = waypoints_from_assignment(msg.waypoints)
        self.state.assigned_route = waypoints
        self.state.current_waypoint_idx = 0
        self.state.corridor_id = msg.corridor_id
        self.state.route_reservation_id = msg.reservation_id
        self.state.route_revision = msg.revision
        self.state.departure_time = msg.departure_time
        self.state.slot_time = msg.eta
        self.state.destination_vertiport = msg.destination_vertiport
        self._route_granted = True
        self._maybe_mark_ready_for_takeoff()

    async def _on_lock_grant(self, msg: LockGrant) -> None:
        log.info(
            "[%s] Lock granted: vertiport=%s slot=%s stand=%s",
            self.state.agent_id, msg.vertiport_id, msg.slot_time, msg.stand_id,
        )
        self.state.slot_reservation_id = msg.reservation_id
        self.state.slot_revision = msg.revision
        self.state.slot_vertiport = msg.vertiport_id
        self.state.slot_time = msg.slot_time
        self.state.stand_id = msg.stand_id

        if self.state.slot_stage == SlotStage.NONE:
            self.state.slot_stage = SlotStage.TENTATIVE
        self._slot_granted = True
        self._maybe_mark_ready_for_takeoff()

    async def _on_landing_clearance(self, msg: LandingClearance) -> None:
        self.state.stand_id = msg.stand_id
        self.state.slot_stage = SlotStage.FINAL_APPROACH
        log.info(
            "[%s] Landing clearance granted at %s (stand=%s standby=%s)",
            self.state.agent_id,
            msg.vertiport_id,
            msg.stand_id,
            msg.emergency_standby,
        )

    async def _on_emergency_resolution(self, msg: EmergencyResolution) -> None:
        if msg.outcome == "HUMAN_REQUIRED":
            self._human_escalation_timer = time.monotonic()
            log.critical(
                "[%s] HUMAN TAKEOVER REQUIRED: %s",
                self.state.agent_id,
                msg.reason,
            )
            return
        if msg.target_vertiport:
            self.state.destination_vertiport = msg.target_vertiport
        log.warning(
            "[%s] Emergency landing reserved at %s",
            self.state.agent_id,
            msg.target_vertiport,
        )

    async def _on_heartbeat(self, msg: Heartbeat) -> None:
        await self.tower.send(HeartbeatAck(agent_id=self.state.agent_id))

    async def _on_sync_request(self, msg: SyncRequest) -> None:
        s = self.state
        await self.tower.send(
            SyncState(
                agent_id=s.agent_id,
                route_reservation_id=s.route_reservation_id,
                route_revision=s.route_revision,
                slot_reservation_id=s.slot_reservation_id,
                slot_revision=s.slot_revision,
                route=[[w.x, w.y, w.z, w.t] for w in s.assigned_route],
                slot_stage=s.slot_stage.name,
            )
        )

    async def _on_preempt_notice(self, msg: PreemptNotice) -> None:
        log.warning(
            "[%s] PREEMPTED by %s at vertiport %s — replanning",
            self.state.agent_id, msg.by_agent_id, msg.vertiport_id,
        )
        # Release our slot and replan if under the reroute limit
        if self.state.reroute_count < MAX_REROUTE_PER_FLIGHT:
            self.state.slot_stage = SlotStage.NONE
            self.state.slot_reservation_id = None
            self.state.slot_revision = 0
            self.state.slot_vertiport = None
            self.state.slot_time = None
            self._slot_granted = False
            self._route_granted = False
            self.state.reroute_count += 1
            # Re-request a route to one of the backup options
            backup = msg.backup_options[0] if msg.backup_options else None
            if backup:
                await self._request_route(backup)
        else:
            # Immune: hold current plan, tower must resolve
            log.info("[%s] Reroute limit reached — immune to further preemption", self.state.agent_id)

    async def _on_tower_down(self, msg: TowerDown) -> None:
        log.warning("[%s] Tower connection lost — switching to V2V-only mode", self.state.agent_id)
        # In V2V-only mode, the agent continues on its current route and
        # uses V2V handshakes for local deconfliction with the same priority rule.

    # -----------------------------------------------------------------------
    # V2V message handlers
    # -----------------------------------------------------------------------

    def _register_v2v_handlers(self) -> None:
        self.v2v.subscribe("POSITION_BROADCAST", self._on_position_broadcast)
        self.v2v.subscribe("HANDSHAKE_INIT", self._on_handshake_init)
        self.v2v.subscribe("HANDSHAKE_ACK", self._on_handshake_ack)
        self.v2v.subscribe("EMERGENCY_DECLARATION", self._on_peer_emergency)

    async def _on_position_broadcast(self, msg: PositionBroadcast) -> None:
        # V2VNetwork already updates nearby_agents; here we just recompute
        # our reachable options list if needed (lightweight, no action needed every tick)
        pass

    async def _on_handshake_init(self, msg: HandshakeInit) -> None:
        s = self.state
        # Respond with our state so the initiator can run deconfliction
        ack = HandshakeAck(
            from_agent=s.agent_id,
            to_agent=msg.from_agent,
            accepted=True,
            battery_pct=s.battery_pct,
            speed=s.speed,
            route=[[w.x, w.y, w.z, w.t] for w in s.assigned_route],
            intent=s.flight_stage.name,
            status=s.status.value,
            priority_metric=s.priority_metric,
            position=list(s.position),
            altitude=s.altitude,
            velocity=list(s.velocity),
            i_will_yield=False,
        )

        # Run deconfliction from our side
        peer_metric = msg.priority_metric
        offset = V2VDeconfliction.negotiate(s, peer_metric)
        if offset != 0.0:
            self.state.lateral_offset = offset
            ack.i_will_yield = True
            log.info(
                "[%s] V2V: yielding to %s (their metric=%.3f > mine=%.3f)",
                s.agent_id, msg.from_agent, peer_metric, s.priority_metric,
            )
        else:
            # We hold; tell peer it should yield
            ack.i_will_yield = False

        from .communication import _agent_port_offset
        peer_port = V2V_WS_PORT + _agent_port_offset(msg.from_agent)
        await self.v2v.send_handshake(msg.from_agent, peer_port, ack)

    async def _on_handshake_ack(self, msg: HandshakeAck) -> None:
        # If the peer told us they will yield, we can hold course
        if msg.i_will_yield:
            self.state.lateral_offset = 0.0
            log.info(
                "[%s] V2V: peer %s will yield — holding course",
                self.state.agent_id, msg.from_agent,
            )
        else:
            # We need to yield
            offset = V2VDeconfliction.negotiate(self.state, msg.priority_metric)
            self.state.lateral_offset = offset

    async def _on_peer_emergency(self, msg: EmergencyDeclaration) -> None:
        log.info(
            "[%s] Peer %s declared EMERGENCY (battery=%.1f%%)",
            self.state.agent_id, msg.agent_id, msg.battery_pct,
        )
        # Update our picture of that peer
        peer = self.v2v.nearby_agents.get(msg.agent_id)
        if peer is not None:
            peer.status = AgentStatus.EMERGENCY
            from .priority import compute_priority_metric
            peer.priority_metric = compute_priority_metric(
                msg.battery_pct, len(msg.reachable_options)
            )

    # -----------------------------------------------------------------------
    # Flight loop
    # -----------------------------------------------------------------------

    async def _flight_loop(self) -> None:
        last_tick = time.monotonic()
        while True:
            await asyncio.sleep(SIM_TICK_S)
            now = time.monotonic()
            dt = now - last_tick
            last_tick = now

            s = self.state
            self.v2v.update_own_state(s)
            self._update_reachable_options()
            s.priority_metric = compute_priority_metric(
                s.battery_pct, len(s.reachable_options)
            )

            # --- Destination request ---
            if self._pending_destination and s.flight_stage == FlightStage.PARKED:
                dest = self._pending_destination
                self._pending_destination = None
                s.destination_vertiport = dest
                s.flight_stage = FlightStage.PRE_FLIGHT
                await self._request_route(dest)

            # --- Advance route ---
            if s.flight_stage not in (
                FlightStage.PARKED, FlightStage.PRE_FLIGHT,
                FlightStage.AWAITING_TAKEOFF,
            ):
                updates = self._route_follower.step(s, dt)
                for k, v in updates.items():
                    setattr(s, k, v)

            # --- AWAITING_TAKEOFF → CLIMBING ---
            departure_due = True
            if s.departure_time:
                try:
                    departure_due = (
                        datetime.now(timezone.utc)
                        >= datetime.fromisoformat(s.departure_time)
                    )
                except ValueError:
                    departure_due = True
            if (
                s.flight_stage == FlightStage.AWAITING_TAKEOFF
                and s.assigned_route
                and departure_due
            ):
                s.flight_stage = FlightStage.CLIMBING
                s.speed = CRUISE_SPEED_MS * 0.6
                log.info("[%s] Taking off", s.agent_id)

            # --- CLIMBING → EN_ROUTE once at cruise altitude ---
            if s.flight_stage == FlightStage.CLIMBING and s.altitude >= s.assigned_route[0].z if s.assigned_route else False:
                s.flight_stage = FlightStage.EN_ROUTE
                s.speed = CRUISE_SPEED_MS

            # --- ON_PAD → request stand assignment ---
            if s.flight_stage == FlightStage.ON_PAD and s.slot_stage != SlotStage.ON_PAD:
                s.slot_stage = SlotStage.ON_PAD
                log.info("[%s] On pad at %s", s.agent_id, s.slot_vertiport)
                # Release slot back to tower once stand assigned (tower will confirm)
                if s.slot_vertiport and s.slot_time:
                    await self.tower.send(LockRelease(
                        agent_id=s.agent_id,
                        vertiport_id=s.slot_vertiport,
                        slot_time=s.slot_time,
                    ))
                s.slot_stage = SlotStage.PARKED
                s.flight_stage = FlightStage.PARKED
                s.assigned_route = []
                s.speed = 0.0
                s.lateral_offset = 0.0
                s.reroute_count = 0
                self._emergency_handled = False
                log.info("[%s] Parked at %s", s.agent_id, s.slot_vertiport)

    # -----------------------------------------------------------------------
    # Battery loop
    # -----------------------------------------------------------------------

    async def _battery_loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(SIM_TICK_S)
            now = time.monotonic()
            dt = now - last
            last = now

            s = self.state
            if s.flight_stage in (FlightStage.PARKED, FlightStage.PRE_FLIGHT, FlightStage.AWAITING_TAKEOFF):
                continue

            drain = BATTERY_DRAIN_EMERGENCY if s.status == AgentStatus.EMERGENCY else BATTERY_DRAIN_PER_S
            s.battery_pct = max(0.0, s.battery_pct - drain * dt)

            if s.battery_pct < EMERGENCY_THRESHOLD_PCT and not self._emergency_handled:
                await self._declare_emergency("LOW_BATTERY")

    # -----------------------------------------------------------------------
    # Periodic state update to tower
    # -----------------------------------------------------------------------

    async def _state_update_loop(self) -> None:
        while True:
            await asyncio.sleep(STATE_UPDATE_INTERVAL_S)
            s = self.state
            msg = StateUpdate(
                agent_id=s.agent_id,
                battery_pct=s.battery_pct,
                speed=s.speed,
                position=list(s.position),
                altitude=s.altitude,
                route=[[w.x, w.y, w.z, w.t] for w in s.assigned_route],
                intent=s.flight_stage.name,
                slot_stage=s.slot_stage.name,
                destination_vertiport=s.destination_vertiport,
                priority_metric=s.priority_metric,
                reachable_options=s.reachable_options,
            )
            await self.tower.send(msg)

    # -----------------------------------------------------------------------
    # Slot lifecycle (concept §3 — ETA-driven stage advancement)
    # -----------------------------------------------------------------------

    async def _slot_lifecycle_loop(self) -> None:
        """Advance slot stage based on ETA proximity."""
        FIRM_FAR_MINUTES = 30
        FIRM_NEAR_MINUTES = 10

        while True:
            await asyncio.sleep(5.0)
            s = self.state
            if s.slot_stage == SlotStage.NONE or s.slot_time is None:
                continue
            try:
                eta_dt = datetime.fromisoformat(s.slot_time)
                minutes_left = (eta_dt - datetime.now(timezone.utc)).total_seconds() / 60
            except Exception:
                continue

            if s.slot_stage == SlotStage.TENTATIVE and minutes_left < FIRM_FAR_MINUTES:
                s.slot_stage = SlotStage.FIRM_FAR
                log.debug("[%s] Slot → FIRM_FAR", s.agent_id)
            elif s.slot_stage == SlotStage.FIRM_FAR and minutes_left < FIRM_NEAR_MINUTES:
                s.slot_stage = SlotStage.FIRM_NEAR
                log.debug("[%s] Slot → FIRM_NEAR", s.agent_id)
            elif s.slot_stage == SlotStage.FIRM_NEAR and s.flight_stage == FlightStage.FINAL_APPROACH:
                log.debug("[%s] Requesting final approach clearance", s.agent_id)
                if s.slot_vertiport:
                    await self.tower.send(LockRequest(
                        agent_id=s.agent_id,
                        vertiport_id=s.slot_vertiport,
                        requested_time=s.slot_time or "",
                        priority_metric=s.priority_metric,
                        status=s.status.value,
                    ))

    # -----------------------------------------------------------------------
    # Emergency handling
    # -----------------------------------------------------------------------

    async def _declare_emergency(self, reason: str) -> None:
        self._emergency_handled = True
        s = self.state
        s.status = AgentStatus.EMERGENCY
        log.warning("[%s] EMERGENCY declared: %s (battery=%.1f%%)", s.agent_id, reason, s.battery_pct)

        decl = EmergencyDeclaration(
            agent_id=s.agent_id,
            battery_pct=s.battery_pct,
            position=list(s.position),
            altitude=s.altitude,
            reachable_options=s.reachable_options,
            priority_metric=s.priority_metric,
        )
        # Broadcast to tower and peers simultaneously
        await asyncio.gather(
            self.tower.send(decl),
            self._v2v_broadcast_emergency(decl),
        )

    async def _v2v_broadcast_emergency(self, msg: EmergencyDeclaration) -> None:
        """Re-broadcast emergency declaration over UDP so nearby agents update their picture."""
        import socket
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = msg.to_json().encode()
        try:
            await loop.run_in_executor(
                None,
                lambda: sock.sendto(payload, ("255.255.255.255", V2V_PORT)),
            )
        except Exception as exc:
            log.debug("[%s] Emergency UDP broadcast failed: %s", self.state.agent_id, exc)
        finally:
            sock.close()

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _request_route(self, destination: str) -> None:
        s = self.state
        payload = build_route_request_payload(s, destination)
        msg = RouteRequest(**payload)
        await self.tower.send(msg)
        log.info("[%s] Route requested to %s", s.agent_id, destination)

    def _update_reachable_options(self) -> None:
        """Recompute which vertiports are within energy range."""
        from .priority import is_reachable
        s = self.state
        speed = s.speed if s.speed > 0 else CRUISE_SPEED_MS
        s.reachable_options = [
            v.vertiport_id
            for v in self._vertiport_candidates
            if is_reachable(s.battery_pct, v.distance_m, speed, BATTERY_DRAIN_PER_S)
        ]

    def _maybe_mark_ready_for_takeoff(self) -> None:
        if not (self._route_granted and self._slot_granted):
            return
        if self.state.flight_stage in (FlightStage.PARKED, FlightStage.PRE_FLIGHT):
            self.state.flight_stage = FlightStage.AWAITING_TAKEOFF
            self.state.slot_stage = SlotStage.TENTATIVE

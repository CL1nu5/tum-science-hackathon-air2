from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from .communication import TowerClient, V2VNetwork
from .config import (
    BATTERY_DRAIN_EMERGENCY,
    BATTERY_DRAIN_PER_S,
    BATTERY_RECHARGE_PER_S,
    BROADCAST_INTERVAL_S,
    CRITICAL_RADIUS_M,
    CRUISE_SPEED_MS,
    DISPATCH_RETRY_S,
    EMERGENCY_FAULT_MTBF_S,
    EMERGENCY_REDECLARE_S,
    EMERGENCY_THRESHOLD_PCT,
    HOLD_MAX_S,
    HOLD_RETRY_S,
    HUMAN_TIMEOUT_S,
    MAX_REROUTE_PER_FLIGHT,
    PARKED_DWELL_S,
    REDISPATCH_MIN_BATTERY_PCT,
    STATE_UPDATE_INTERVAL_S,
    SIM_TICK_S,
    TOWER_WS_URL,
    V2V_PORT,
    V2V_VERTICAL_SEP_M,
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
    ProtocolError,
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
from .routing import (
    RouteFollower,
    V2VDeconfliction,
    build_route_request_payload,
    interpolate_route_4d,
    waypoints_from_assignment,
)
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
        destination_pool: list[str] | None = None,
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
        # Pool of vertiport IDs this taxi may be dispatched to (own-operator pads).
        self._destination_pool: list[str] = destination_pool or [
            v.vertiport_id
            for v in self._vertiport_candidates
            if v.is_own_operator and v.surface_type == "vertiport"
        ]
        self._initial_destination: str | None = None
        # Stagger the first dispatch so a large fleet doesn't request every route
        # in the same instant (smooths take-off contention on shared pads).
        self._next_dispatch_at: float = time.monotonic() + random.uniform(0.0, 12.0)
        self._emergency_handled: bool = False
        self._emergency_declared_at: float = 0.0
        self._human_escalation_timer: float | None = None
        self._route_granted: bool = False
        self._slot_granted: bool = False
        # Landing coordination: a taxi only descends onto the pad once the tower
        # has granted it a free stand (LandingClearance). Until then it holds
        # (loiters on its corridor) instead of stacking onto an occupied pad.
        self._landing_cleared: bool = False
        self._hold_started_at: float | None = None
        self._last_landing_request_at: float = 0.0

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
            self._dispatch_loop(),
        )

    def set_destination(self, vertiport_id: str) -> None:
        """Set the first destination the taxi flies to once airborne capacity allows."""
        self._initial_destination = vertiport_id

    # -----------------------------------------------------------------------
    # Tower message handlers
    # -----------------------------------------------------------------------

    def _register_tower_handlers(self) -> None:
        self.tower.subscribe("ROUTE_ASSIGNMENT", self._on_route_assignment)
        self.tower.subscribe("LOCK_GRANT", self._on_lock_grant)
        self.tower.subscribe("LANDING_CLEARANCE", self._on_landing_clearance)
        self.tower.subscribe("EMERGENCY_RESOLUTION", self._on_emergency_resolution)
        self.tower.subscribe("PREEMPT_NOTICE", self._on_preempt_notice)
        self.tower.subscribe("PROTOCOL_ERROR", self._on_protocol_error)
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
        # Fresh corridor → fresh approach: clear any prior landing clearance/hold
        # and recentre on the new corridor (drop any stale V2V yield offset).
        self._landing_cleared = False
        self._hold_started_at = None
        self.state.lateral_offset = 0.0
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
        # A stand (or emergency standby) is secured — the taxi may now descend and
        # land. This releases any active hold so the route follower flies it in.
        self._landing_cleared = msg.stand_id is not None or msg.emergency_standby
        self._hold_started_at = None
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

    async def _on_protocol_error(self, msg: ProtocolError) -> None:
        s = self.state
        log.warning("[%s] Tower rejected %s: %s", s.agent_id, msg.code, msg.message)
        # A rejected take-off request (busy airspace / no conflict-free route)
        # drops us back to PARKED so the dispatch loop retries after a backoff.
        # NO_FREE_STAND during approach self-retries via the slot lifecycle loop.
        if s.flight_stage == FlightStage.PRE_FLIGHT and not s.assigned_route:
            s.flight_stage = FlightStage.PARKED
            s.slot_stage = SlotStage.NONE
            self._route_granted = False
            self._slot_granted = False
            self._next_dispatch_at = time.monotonic() + DISPATCH_RETRY_S

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

        # Run deconfliction from our side. Peers separated vertically (the tower
        # hands out conflict-free corridors at distinct altitudes) are not a
        # near-miss, so we hold course and don't thrash a lateral offset.
        peer_metric = msg.priority_metric
        vertically_clear = abs(s.altitude - msg.altitude) > V2V_VERTICAL_SEP_M
        offset = 0.0 if vertically_clear else V2VDeconfliction.negotiate(
            s, peer_metric, msg.from_agent
        )
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
        elif abs(self.state.altitude - msg.altitude) > V2V_VERTICAL_SEP_M:
            # Vertically separated — no near-miss; stay on the corridor centreline.
            self.state.lateral_offset = 0.0
        else:
            # We need to yield
            offset = V2VDeconfliction.negotiate(self.state, msg.priority_metric, msg.from_agent)
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
            cruise_alt = (
                s.assigned_route[1].z
                if len(s.assigned_route) > 1
                else None
            )
            if (
                s.flight_stage == FlightStage.CLIMBING
                and cruise_alt is not None
                and s.altitude >= cruise_alt - 1.0
            ):
                s.flight_stage = FlightStage.EN_ROUTE
                s.speed = CRUISE_SPEED_MS

            # --- Approach / landing coordination (hold until a stand is granted) ---
            if s.flight_stage in (FlightStage.DESCENDING, FlightStage.FINAL_APPROACH):
                await self._coordinate_landing(now)

            # --- V2V: return to corridor centre once no peer is close ---
            if s.lateral_offset != 0.0 and not self.v2v.find_nearby(
                s.position, CRITICAL_RADIUS_M
            ):
                s.lateral_offset = 0.0

            # --- ON_PAD → park (only with a granted stand) ---
            if s.flight_stage == FlightStage.ON_PAD and s.slot_stage != SlotStage.ON_PAD:
                if not self._landing_cleared:
                    # Reached the pad without a stand (race) — bounce back to a
                    # hold so the pad stays clear instead of an uncleared landing.
                    s.flight_stage = FlightStage.FINAL_APPROACH
                    await self._coordinate_landing(now)
                    continue
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
                s.status = AgentStatus.NORMAL
                s.parked_vertiport = s.slot_vertiport
                s.assigned_route = []
                s.current_waypoint_idx = 0
                s.destination_vertiport = None
                s.speed = 0.0
                s.lateral_offset = 0.0
                s.reroute_count = 0
                self._emergency_handled = False
                self._human_escalation_timer = None
                self._landing_cleared = False
                self._hold_started_at = None
                # Recharge, dwell briefly, then the dispatch loop sends it out again.
                self._next_dispatch_at = time.monotonic() + PARKED_DWELL_S
                log.info("[%s] Parked at %s", s.agent_id, s.slot_vertiport)

    # -----------------------------------------------------------------------
    # Approach / holding (concept §3 pad-standby + resolution order: adjust speed)
    # -----------------------------------------------------------------------

    async def _coordinate_landing(self, now: float) -> None:
        """Secure a stand before descending; loiter (hold) until one is granted.

        A taxi only commits to the final descent once the tower confirms a free
        stand. Until then it holds on its corridor — different speeds (cruise →
        hold) let an inbound taxi delay its approach instead of stacking onto an
        occupied pad. Held too long → divert to a backup vertiport.
        """
        s = self.state
        if self._landing_cleared:
            return

        # Rate-limited request for a stand at the destination.
        if (
            s.slot_vertiport
            and now - self._last_landing_request_at > HOLD_RETRY_S
        ):
            self._last_landing_request_at = now
            await self.tower.send(LockRequest(
                agent_id=s.agent_id,
                vertiport_id=s.slot_vertiport,
                requested_time=s.slot_time or "",
                priority_metric=s.priority_metric,
                status=s.status.value,
            ))

        # No clearance yet → hold: slide the unflown schedule forward so the taxi
        # loiters at altitude on its corridor (the pad stays clear).
        if self._hold_started_at is None:
            self._hold_started_at = now
            log.info("[%s] Holding for a stand at %s", s.agent_id, s.slot_vertiport)
        self._hold(SIM_TICK_S)
        s.speed = 0.0  # hover-hold

        if now - self._hold_started_at > HOLD_MAX_S:
            await self._divert()

    def _hold(self, dt: float) -> None:
        """Loiter by sliding the current and following corridor waypoints forward
        in time by ``dt``. The interpolated position is held exactly in place for
        both the simulation and the dashboard (which read the same 4D corridor)."""
        route = self.state.assigned_route
        if len(route) < 2:
            return
        _, seg = interpolate_route_4d(route, time.time())
        shifted = list(route)
        for i in range(seg, len(shifted)):
            w = shifted[i]
            shifted[i] = Waypoint(w.x, w.y, w.z, w.t + dt)
        self.state.assigned_route = shifted

    async def _divert(self) -> None:
        """Give up on the held destination and reroute to a backup vertiport."""
        s = self.state
        self._hold_started_at = None
        if s.reroute_count >= MAX_REROUTE_PER_FLIGHT:
            # Out of reroutes — keep holding; the tower/emergency path must resolve.
            self._hold_started_at = time.monotonic()
            return
        backup = self._pick_destination()
        if not backup or backup == s.slot_vertiport:
            self._hold_started_at = time.monotonic()
            return
        log.warning("[%s] Held too long — diverting to %s", s.agent_id, backup)
        if s.slot_vertiport and s.slot_time:
            await self.tower.send(LockRelease(
                agent_id=s.agent_id,
                vertiport_id=s.slot_vertiport,
                slot_time=s.slot_time,
            ))
        s.reroute_count += 1
        self._landing_cleared = False
        await self._request_route(backup)

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

            # Keep flagging an unresolved human takeover (concept §5 hard timeout).
            if (
                self._human_escalation_timer is not None
                and now - self._human_escalation_timer > HUMAN_TIMEOUT_S
            ):
                log.critical(
                    "[%s] Human takeover still pending after %.0fs",
                    s.agent_id,
                    HUMAN_TIMEOUT_S,
                )
                self._human_escalation_timer = now

            if s.flight_stage == FlightStage.PARKED:
                # Recharge on the stand so the taxi can fly another leg.
                s.battery_pct = min(100.0, s.battery_pct + BATTERY_RECHARGE_PER_S * dt)
                continue
            if s.flight_stage in (FlightStage.PRE_FLIGHT, FlightStage.AWAITING_TAKEOFF):
                continue

            drain = BATTERY_DRAIN_EMERGENCY if s.status == AgentStatus.EMERGENCY else BATTERY_DRAIN_PER_S
            s.battery_pct = max(0.0, s.battery_pct - drain * dt)

            # Rare simulated in-flight fault -> sudden critical energy state, so
            # the tower's emergency cascade is exercised even when batteries are
            # otherwise healthy (concept §4: "failure, low/empty battery").
            if (
                EMERGENCY_FAULT_MTBF_S > 0
                and not self._emergency_handled
                and s.flight_stage in (FlightStage.EN_ROUTE, FlightStage.CLIMBING)
                and random.random() < dt / EMERGENCY_FAULT_MTBF_S
            ):
                # Drop to a constrained-but-still-recoverable state so the tower
                # can usually reroute it (occasionally escalating to a human).
                s.battery_pct = min(s.battery_pct, 24.0 + random.random() * 6.0)
                await self._declare_emergency("SYSTEM_FAULT")
                continue

            if s.battery_pct < EMERGENCY_THRESHOLD_PCT:
                if not self._emergency_handled:
                    await self._declare_emergency("LOW_BATTERY")
                elif (
                    now - self._emergency_declared_at > EMERGENCY_REDECLARE_S
                    and not self._route_granted
                ):
                    # Only re-declare if the cascade never produced a landing route
                    # (e.g. it returned HUMAN_REQUIRED). If we already hold an
                    # emergency corridor we fly it — re-declaring would keep
                    # resetting the route and the taxi would never arrive.
                    await self._declare_emergency("LOW_BATTERY_RETRY")

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
            # The actual stand request + hold is driven by _coordinate_landing once
            # the taxi reaches its approach, so the pad is only committed near touchdown.

    # -----------------------------------------------------------------------
    # Dispatch (continuous operation)
    # -----------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """When parked and charged, fly the taxi to a fresh destination."""
        while True:
            await asyncio.sleep(1.0)
            s = self.state
            if s.flight_stage != FlightStage.PARKED:
                continue
            if not self.tower.tower_alive:
                continue
            if time.monotonic() < self._next_dispatch_at:
                continue
            if s.battery_pct < REDISPATCH_MIN_BATTERY_PCT:
                continue
            destination = self._pick_destination()
            if not destination:
                continue
            self._next_dispatch_at = time.monotonic() + DISPATCH_RETRY_S
            self._route_granted = False
            self._slot_granted = False
            s.destination_vertiport = destination
            s.flight_stage = FlightStage.PRE_FLIGHT
            await self._request_route(destination)

    def _pick_destination(self) -> str | None:
        if self._initial_destination:
            dest = self._initial_destination
            self._initial_destination = None
            return dest
        here = self.state.parked_vertiport or self.state.slot_vertiport
        pool = [vid for vid in self._destination_pool if vid != here]
        if not pool:
            pool = list(self._destination_pool)
        return random.choice(pool) if pool else None

    # -----------------------------------------------------------------------
    # Emergency handling
    # -----------------------------------------------------------------------

    async def _declare_emergency(self, reason: str) -> None:
        self._emergency_handled = True
        self._emergency_declared_at = time.monotonic()
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
        """Recompute which vertiports are within energy range of our position."""
        from math import hypot

        from .priority import is_reachable
        s = self.state
        speed = s.speed if s.speed > 0 else CRUISE_SPEED_MS
        options = []
        for v in self._vertiport_candidates:
            distance = hypot(v.x - s.position[0], v.y - s.position[1])
            if is_reachable(s.battery_pct, distance, speed, BATTERY_DRAIN_PER_S):
                options.append(v.vertiport_id)
        s.reachable_options = options

    def _maybe_mark_ready_for_takeoff(self) -> None:
        if not (self._route_granted and self._slot_granted):
            return
        if self.state.flight_stage in (FlightStage.PARKED, FlightStage.PRE_FLIGHT):
            self.state.flight_stage = FlightStage.AWAITING_TAKEOFF
            self.state.slot_stage = SlotStage.TENTATIVE

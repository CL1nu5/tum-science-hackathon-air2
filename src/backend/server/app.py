from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..agents.messages import (
    BaseMessage,
    EmergencyDeclaration,
    EmergencyResolution,
    Heartbeat,
    HeartbeatAck,
    LandingClearance,
    LockGrant,
    LockRelease,
    LockRequest,
    PreemptNotice,
    ProtocolError,
    RouteAssignment,
    RouteRequest,
    StateUpdate,
    SyncRequest,
    SyncState,
    WeatherAdvisory,
    parse_message,
)
from .config import get_settings
from .connections import ConnectionManager
from .emergency import EmergencyCoordinator
from .route_planner import FlightPlan, RoutePlanner, RoutePlanningError
from .schemas import (
    NoiseZoneCreate,
    PadAvailabilityUpdate,
    VertiportUpsert,
    WeatherCellCreate,
)
from .slot_scheduler import HARD_LOCK_STAGES, PadSlotScheduler
from .state_service import TowerStateService
from .store import JsonStore, new_id, now_iso


settings = get_settings()
store = JsonStore(settings.state_file)
connections = ConnectionManager()
slot_scheduler = PadSlotScheduler(settings, store)
route_planner = RoutePlanner(settings, store, slot_scheduler)
state_service = TowerStateService(settings, store)
emergency_coordinator = EmergencyCoordinator(
    settings,
    store,
    slot_scheduler,
    route_planner,
    state_service,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.load()
    tasks = [
        asyncio.create_task(_heartbeat_loop(), name="tower-heartbeats"),
        asyncio.create_task(_cleanup_loop(), name="tower-cleanup"),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="AIR2 Tower", version="0.2.0", lifespan=lifespan)


@app.get("/")
async def dashboard() -> FileResponse:
    path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "Electrx Tower - Clean.dc.html"
    )
    return FileResponse(path)


@app.get("/support.js")
async def dashboard_support() -> FileResponse:
    return FileResponse(
        Path(__file__).resolve().parents[2] / "frontend" / "support.js",
        media_type="application/javascript",
    )


@app.get("/landing-pads.csv")
async def dashboard_landing_pads() -> FileResponse:
    return FileResponse(
        Path(__file__).resolve().parents[2] / "frontend" / "landing-pads.csv",
        media_type="text/csv",
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "storage": "json",
        "state_file": str(settings.state_file),
        "connected_agents": len(connections.connected_agent_ids()),
        "timestamp": now_iso(),
    }


@app.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    return await store.snapshot()


@app.get("/api/agents")
async def agents() -> list[dict]:
    return (await store.snapshot())["aircraft"]


@app.get("/api/vertiports")
async def vertiports() -> list[dict]:
    return (await store.snapshot())["vertiports"]


@app.get("/api/routes")
async def routes() -> list[dict]:
    return (await store.snapshot())["routes"]


@app.get("/api/reservations")
async def reservations() -> list[dict]:
    return (await store.snapshot())["pad_reservations"]


@app.get("/api/events")
async def events(limit: int = 100) -> list[dict]:
    return (await store.snapshot())["events"][: max(1, min(limit, 500))]


@app.post("/api/vertiports")
async def upsert_vertiport(payload: VertiportUpsert) -> dict[str, str]:
    async with store.lock:
        port = {
            "vertiport_id": payload.vertiport_id,
            "name": payload.name,
            "position": [payload.position_x, payload.position_y],
            "elevation_m": payload.elevation_m,
            "operator": payload.operator,
            "surface_type": payload.surface_type,
            "suitability_score": payload.suitability_score,
            "pad_available": payload.pad_available,
            "active": payload.active,
            "metadata": payload.metadata,
        }
        store.state["vertiports"][payload.vertiport_id] = port
        existing = [
            stand
            for stand in store.state["stands"].values()
            if stand["vertiport_id"] == payload.vertiport_id
        ]
        for index in range(len(existing), payload.stand_count):
            stand_id = f"{payload.vertiport_id}-S{index + 1:02d}"
            store.state["stands"][stand_id] = {
                "stand_id": stand_id,
                "vertiport_id": payload.vertiport_id,
                "name": stand_id,
                "occupied_by": None,
                "active": True,
            }
        store.event_locked(
            "VERTIPORT_UPDATED",
            f"Infrastructure updated: {payload.vertiport_id}",
        )
        store.persist_locked()
    await _publish_change("VERTIPORT_UPDATED", payload.vertiport_id)
    return {"vertiport_id": payload.vertiport_id}


@app.post("/api/weather")
async def create_weather(payload: WeatherCellCreate) -> dict:
    weather_id = new_id()
    cell = {
        "weather_id": weather_id,
        "center": [payload.center_x, payload.center_y],
        "radius_m": payload.radius_m,
        "severity": payload.severity,
        "velocity": [payload.velocity_x, payload.velocity_y],
        "active_from": payload.active_from.isoformat(),
        "active_until": payload.active_until.isoformat(),
        "active": True,
    }
    async with store.lock:
        store.state["weather"][weather_id] = cell
        store.event_locked(
            "WEATHER_UPDATED",
            f"Weather cell {weather_id} created",
            payload=cell,
        )
        store.persist_locked()
    advisory = WeatherAdvisory(cells=[cell])
    for agent_id in connections.connected_agent_ids():
        await connections.send_agent(agent_id, advisory)
    await _publish_change("WEATHER_UPDATED", weather_id)
    return cell


@app.post("/api/noise-zones")
async def create_noise_zone(payload: NoiseZoneCreate) -> dict:
    zone_id = new_id()
    zone = {
        "zone_id": zone_id,
        "name": payload.name,
        "center": [payload.center_x, payload.center_y],
        "radius_m": payload.radius_m,
        "penalty_weight": payload.penalty_weight,
        "max_active_overflights": payload.max_active_overflights,
        "active": True,
    }
    async with store.lock:
        store.state["noise_zones"][zone_id] = zone
        store.event_locked("NOISE_ZONE_UPDATED", f"{payload.name} created")
        store.persist_locked()
    await _publish_change("NOISE_ZONE_UPDATED", zone_id)
    return zone


@app.patch("/api/vertiports/{vertiport_id}/pad")
async def set_pad_availability(
    vertiport_id: str, payload: PadAvailabilityUpdate
) -> dict:
    async with store.lock:
        port = store.state["vertiports"].get(vertiport_id)
        if not port:
            raise HTTPException(status_code=404, detail="Vertiport not found")
        port["pad_available"] = payload.available
        store.event_locked(
            "PAD_AVAILABILITY_CHANGED",
            f"{vertiport_id} pad available={payload.available}",
            severity="INFO" if payload.available else "CRITICAL",
        )
        store.persist_locked()
    await _publish_change("PAD_AVAILABILITY_CHANGED", vertiport_id)
    return {"vertiport_id": vertiport_id, "available": payload.available}


@app.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket) -> None:
    await connections.connect_dashboard(websocket)
    try:
        await websocket.send_json({"type": "SNAPSHOT", "data": await store.snapshot()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await connections.disconnect_dashboard(websocket)


@app.websocket("/ws/agent/{agent_id}")
async def agent_socket(websocket: WebSocket, agent_id: str) -> None:
    await connections.connect_agent(agent_id, websocket)
    await state_service.mark_connected(agent_id, True)
    async with store.lock:
        store.event_locked(
            "AGENT_CONNECTED",
            f"{agent_id} connected to Tower",
            agent_id=agent_id,
        )
        store.persist_locked()
    await connections.send_agent(agent_id, SyncRequest(agent_id=agent_id))
    await _publish_change("AGENT_CONNECTED", agent_id)
    try:
        while True:
            try:
                message = parse_message(await websocket.receive_text())
            except ValueError as exc:
                await connections.send_agent(
                    agent_id,
                    ProtocolError(
                        agent_id=agent_id,
                        code="INVALID_MESSAGE",
                        message=str(exc),
                    ),
                )
                continue
            if hasattr(message, "agent_id") and not getattr(message, "agent_id"):
                setattr(message, "agent_id", agent_id)
            if hasattr(message, "agent_id") and getattr(message, "agent_id") != agent_id:
                await connections.send_agent(
                    agent_id,
                    ProtocolError(
                        agent_id=agent_id,
                        code="AGENT_ID_MISMATCH",
                        message="Message agent_id does not match WebSocket path",
                    ),
                )
                continue
            await _handle_agent_message(agent_id, message)
    except WebSocketDisconnect:
        pass
    finally:
        await connections.disconnect_agent(agent_id, websocket)
        await state_service.mark_connected(agent_id, False)
        await _publish_change("AGENT_DISCONNECTED", agent_id)


async def _handle_agent_message(agent_id: str, message: BaseMessage) -> None:
    responses: list[BaseMessage] = []
    victim_notice: tuple[str, PreemptNotice] | None = None
    try:
        if isinstance(message, HeartbeatAck):
            await state_service.mark_connected(agent_id, True)
        elif isinstance(message, Heartbeat):
            await state_service.mark_connected(agent_id, True)
            responses.append(HeartbeatAck(agent_id=agent_id))
        elif isinstance(message, StateUpdate):
            await state_service.ingest(message)
        elif isinstance(message, RouteRequest):
            responses.extend(_plan_messages(await route_planner.reserve_flight_plan(message)))
        elif isinstance(message, LockRequest):
            async with store.lock:
                aircraft = store.state["aircraft"].get(agent_id)
                reservation = slot_scheduler.active_for_agent_locked(agent_id)
                if not aircraft or not reservation:
                    raise RoutePlanningError("No active landing reservation")
                stand = slot_scheduler.assign_stand_locked(
                    agent_id, reservation["vertiport_id"]
                )
                standby = stand is None and aircraft["status"] == "EMERGENCY"
                if not stand and not standby:
                    responses.append(
                        ProtocolError(
                            agent_id=agent_id,
                            code="NO_FREE_STAND",
                            message="Landing withheld until a stand is free",
                            retryable=True,
                        )
                    )
                else:
                    reservation["stage"] = "FINAL_APPROACH"
                    reservation["emergency_standby"] = standby
                    reservation["revision"] += 1
                    responses.append(
                        LandingClearance(
                            agent_id=agent_id,
                            reservation_id=reservation["reservation_id"],
                            vertiport_id=reservation["vertiport_id"],
                            stand_id=stand["stand_id"] if stand else None,
                            emergency_standby=standby,
                        )
                    )
                store.persist_locked()
        elif isinstance(message, LockRelease):
            async with store.lock:
                reservation = slot_scheduler.release_locked(agent_id)
                for route in store.state["routes"].values():
                    if route["agent_id"] == agent_id and route.get("active", True):
                        route["active"] = False
                        route["revision"] += 1
                aircraft = store.state["aircraft"].get(agent_id)
                if aircraft:
                    aircraft.update(
                        {
                            "flight_stage": "PARKED",
                            "slot_stage": "PARKED",
                            "route": [],
                            "reroute_count": 0,
                            "status": "NORMAL",
                            "revision": aircraft["revision"] + 1,
                        }
                    )
                store.event_locked(
                    "LANDING_COMPLETED",
                    f"{agent_id} cleared the pad",
                    agent_id=agent_id,
                    payload={
                        "reservation_id": reservation["reservation_id"]
                        if reservation
                        else None
                    },
                )
                store.persist_locked()
        elif isinstance(message, EmergencyDeclaration):
            decision = await emergency_coordinator.resolve(message)
            responses.append(
                EmergencyResolution(
                    agent_id=agent_id,
                    outcome=decision.outcome,
                    target_vertiport=(
                        decision.target["vertiport_id"] if decision.target else None
                    ),
                    surface_type=(
                        decision.target["surface_type"] if decision.target else None
                    ),
                    preempted_agent_id=decision.preempted_agent_id,
                    reason=decision.reason,
                )
            )
            if decision.plan:
                responses.extend(_plan_messages(decision.plan))
            if decision.preempted_agent_id and decision.target:
                victim_notice = (
                    decision.preempted_agent_id,
                    PreemptNotice(
                        agent_id=decision.preempted_agent_id,
                        by_agent_id=agent_id,
                        vertiport_id=decision.target["vertiport_id"],
                        slot_time=decision.plan.route["eta_at"] if decision.plan else "",
                        backup_options=decision.victim_backups or [],
                    ),
                )
        elif isinstance(message, SyncState):
            responses.extend(await _synchronize(message))
        else:
            responses.append(
                ProtocolError(
                    agent_id=agent_id,
                    code="UNSUPPORTED_MESSAGE",
                    message=f"Tower does not handle {message.type}",
                )
            )
    except (RoutePlanningError, ValueError) as exc:
        responses = [
            ProtocolError(
                agent_id=agent_id,
                code="OPERATION_FAILED",
                message=str(exc),
                retryable=True,
            )
        ]
    except Exception as exc:
        responses = [
            ProtocolError(
                agent_id=agent_id,
                code="SERVER_ERROR",
                message=str(exc),
            )
        ]
    for response in responses:
        await connections.send_agent(agent_id, response)
    if victim_notice:
        await connections.send_agent(*victim_notice)
    # Heartbeats are pure liveness chatter; don't make every dashboard refetch on them.
    if message.type not in ("HEARTBEAT", "HEARTBEAT_ACK"):
        await _publish_change(message.type, agent_id)


def _plan_messages(plan: FlightPlan) -> list[BaseMessage]:
    route, slot = plan.route, plan.slot
    return [
        RouteAssignment(
            agent_id=route["agent_id"],
            reservation_id=route["reservation_id"],
            corridor_id=route["corridor_id"],
            destination_vertiport=route["destination_vertiport"],
            waypoints=route["waypoints"],
            departure_time=route["departure_at"],
            eta=route["eta_at"],
            lease_expires_at=route["lease_expires_at"],
            revision=route["revision"],
        ),
        LockGrant(
            agent_id=slot["agent_id"],
            reservation_id=slot["reservation_id"],
            vertiport_id=slot["vertiport_id"],
            start_time=slot["start_at"],
            end_time=slot["end_at"],
            slot_time=route["eta_at"],
            stand_id=None,
            lease_expires_at=slot["lease_expires_at"],
            stage=slot["stage"],
            revision=slot["revision"],
        ),
    ]


async def _synchronize(message: SyncState) -> list[BaseMessage]:
    async with store.lock:
        route = state_service.active_route_locked(message.agent_id)
        slot = state_service.active_slot_locked(message.agent_id)
        responses = (
            _plan_messages(FlightPlan(route=route, slot=slot))
            if route and slot
            else []
        )
        weather = [
            cell
            for cell in store.state["weather"].values()
            if cell.get("active", True)
        ]
        if weather:
            responses.append(WeatherAdvisory(cells=weather))
        return responses


async def _publish_change(change_type: str, entity_id: str) -> None:
    await connections.broadcast_dashboards(
        {
            "type": "STATE_CHANGED",
            "change_type": change_type,
            "entity_id": entity_id,
            "timestamp": now_iso(),
        }
    )


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(settings.heartbeat_interval_s)
        for agent_id in connections.connected_agent_ids():
            await connections.send_agent(agent_id, Heartbeat(agent_id=agent_id))


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.cleanup_interval_s)
        now = datetime.now(timezone.utc)
        changed = False
        async with store.lock:
            expired_agents: set[str] = set()
            for slot in store.state["pad_reservations"].values():
                if (
                    slot.get("active", True)
                    and slot["stage"] not in HARD_LOCK_STAGES
                    and datetime.fromisoformat(slot["lease_expires_at"]) < now
                ):
                    slot["active"] = False
                    slot["revision"] += 1
                    changed = True
                    expired_agents.add(slot["agent_id"])
            for route in store.state["routes"].values():
                if (
                    route.get("active", True)
                    and datetime.fromisoformat(route["lease_expires_at"]) < now
                ):
                    route["active"] = False
                    route["revision"] += 1
                    changed = True
                    expired_agents.add(route["agent_id"])
            # A flight plan is a route+slot pair: if one half lapsed, retire the
            # other too so no orphaned corridor or held pad interval lingers
            # (hard-locked slots belong to a landing taxi and are left alone).
            for agent_id in expired_agents:
                for route in store.state["routes"].values():
                    if route["agent_id"] == agent_id and route.get("active", True):
                        route["active"] = False
                        route["revision"] += 1
                for slot in store.state["pad_reservations"].values():
                    if (
                        slot["agent_id"] == agent_id
                        and slot.get("active", True)
                        and slot["stage"] not in HARD_LOCK_STAGES
                    ):
                        slot["active"] = False
                        slot["revision"] += 1
            stale_before = now - timedelta(seconds=settings.agent_stale_after_s)
            for aircraft in store.state["aircraft"].values():
                if (
                    aircraft["connected"]
                    and datetime.fromisoformat(aircraft["last_seen_at"]) < stale_before
                ):
                    aircraft["connected"] = False
                    aircraft["revision"] += 1
                    changed = True
                    # A vanished taxi must not keep holding a stand or a corridor.
                    agent_id = aircraft["agent_id"]
                    slot_scheduler.release_departure_stand_locked(agent_id)
                    for route in store.state["routes"].values():
                        if route["agent_id"] == agent_id and route.get("active", True):
                            route["active"] = False
                            route["revision"] += 1
                    for slot in store.state["pad_reservations"].values():
                        if (
                            slot["agent_id"] == agent_id
                            and slot.get("active", True)
                            and slot["stage"] not in HARD_LOCK_STAGES
                        ):
                            slot["active"] = False
                            slot["revision"] += 1

            # Prune deactivated routes/slots so the persisted state file does not
            # grow without bound (it had ballooned to ~500 KB of dead reservations).
            for bucket in ("routes", "pad_reservations"):
                dead = [
                    key
                    for key, item in store.state[bucket].items()
                    if not item.get("active", True)
                ]
                if dead:
                    for key in dead:
                        del store.state[bucket][key]
                    changed = True

            if changed:
                store.persist_locked()
        if changed:
            await _publish_change("CLEANUP", "tower")


def run() -> None:
    uvicorn.run(
        "src.backend.server.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    run()

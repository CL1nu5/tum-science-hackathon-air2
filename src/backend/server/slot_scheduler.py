from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Settings
from .store import JsonStore, new_id, now_iso


HARD_LOCK_STAGES = {"FINAL_APPROACH", "ON_PAD", "PARKED"}


class SlotUnavailable(RuntimeError):
    pass


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PadSlotScheduler:
    def __init__(self, settings: Settings, store: JsonStore) -> None:
        self.settings = settings
        self.store = store

    def interval_for_eta(self, eta: datetime) -> tuple[datetime, datetime]:
        half = timedelta(seconds=self.settings.pad_occupancy_seconds / 2)
        return eta - half, eta + half

    def is_interval_free_locked(
        self,
        vertiport_id: str,
        start_at: datetime,
        end_at: datetime,
        *,
        exclude_agent_id: str | None = None,
    ) -> bool:
        buffer = timedelta(seconds=self.settings.pad_buffer_seconds)
        for slot in self.store.state["pad_reservations"].values():
            if not slot.get("active", True):
                continue
            if slot["vertiport_id"] != vertiport_id:
                continue
            if exclude_agent_id and slot["agent_id"] == exclude_agent_id:
                continue
            if parse_time(slot["start_at"]) < end_at + buffer and parse_time(
                slot["end_at"]
            ) > start_at - buffer:
                return False
        return True

    def reserve_exact_locked(
        self,
        *,
        agent_id: str,
        vertiport_id: str,
        eta: datetime,
        emergency_standby: bool = False,
        reroute_count: int = 0,
    ) -> dict:
        port = self.store.state["vertiports"].get(vertiport_id)
        if not port or not port["active"] or not port["pad_available"]:
            raise SlotUnavailable(f"Landing surface {vertiport_id} is unavailable")

        start_at, end_at = self.interval_for_eta(eta)
        if port["surface_type"] == "vertiport" and not self.is_interval_free_locked(
            vertiport_id,
            start_at,
            end_at,
            exclude_agent_id=agent_id,
        ):
            raise SlotUnavailable(f"No free pad interval at {vertiport_id}")

        future_slot_exists = any(
            slot.get("active", True)
            and slot["vertiport_id"] == vertiport_id
            and slot["agent_id"] != agent_id
            and parse_time(slot["start_at"]) >= end_at
            for slot in self.store.state["pad_reservations"].values()
        )
        for slot in self.store.state["pad_reservations"].values():
            if slot["agent_id"] == agent_id and slot.get("active", True):
                slot["active"] = False
                slot["revision"] += 1

        reservation_id = new_id()
        reservation = {
            "reservation_id": reservation_id,
            "vertiport_id": vertiport_id,
            "agent_id": agent_id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "stage": "TENTATIVE",
            "lease_expires_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.settings.slot_lease_seconds)
            ).isoformat(),
            "active": True,
            "slot_in": future_slot_exists,
            "emergency_standby": emergency_standby,
            "reroute_count": reroute_count,
            "preempted_by": None,
            "stand_id": None,
            "revision": 1,
            "created_at": now_iso(),
        }
        self.store.state["pad_reservations"][reservation_id] = reservation
        return reservation

    def active_for_agent_locked(self, agent_id: str) -> dict | None:
        return next(
            (
                slot
                for slot in self.store.state["pad_reservations"].values()
                if slot["agent_id"] == agent_id and slot.get("active", True)
            ),
            None,
        )

    def advance_stage_locked(self, agent_id: str, stage: str) -> dict | None:
        reservation = self.active_for_agent_locked(agent_id)
        if not reservation:
            return None
        allowed = {
            "TENTATIVE": {"TENTATIVE", "FIRM_FAR"},
            "FIRM_FAR": {"FIRM_FAR", "FIRM_NEAR"},
            "FIRM_NEAR": {"FIRM_NEAR", "FINAL_APPROACH"},
            "FINAL_APPROACH": {"FINAL_APPROACH", "ON_PAD"},
            "ON_PAD": {"ON_PAD", "PARKED"},
            "PARKED": {"PARKED"},
        }
        if stage not in allowed.get(reservation["stage"], {reservation["stage"]}):
            raise SlotUnavailable(
                f"Invalid slot transition {reservation['stage']} -> {stage}"
            )
        reservation["stage"] = stage
        reservation["revision"] += 1
        return reservation

    def assign_stand_locked(self, agent_id: str, vertiport_id: str) -> dict | None:
        # Idempotent: a retried LockRequest (the inbound taxi polls while holding)
        # must not hand the same taxi a *second* stand. Return the one it holds.
        for stand in self.store.state["stands"].values():
            if (
                stand["vertiport_id"] == vertiport_id
                and stand["occupied_by"] == agent_id
            ):
                return stand
        for stand in self.store.state["stands"].values():
            if (
                stand["vertiport_id"] == vertiport_id
                and stand["active"]
                and stand["occupied_by"] is None
            ):
                stand["occupied_by"] = agent_id
                reservation = self.active_for_agent_locked(agent_id)
                if reservation:
                    reservation["stand_id"] = stand["stand_id"]
                    reservation["revision"] += 1
                return stand
        return None

    def release_locked(self, agent_id: str) -> dict | None:
        reservation = self.active_for_agent_locked(agent_id)
        if reservation:
            reservation["active"] = False
            reservation["revision"] += 1
        return reservation

    def release_departure_stand_locked(self, agent_id: str) -> None:
        for stand in self.store.state["stands"].values():
            if stand["occupied_by"] == agent_id:
                stand["occupied_by"] = None

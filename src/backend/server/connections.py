from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from ..agents.messages import BaseMessage


class ConnectionManager:
    """Tracks live agent and dashboard WebSocket transports."""

    def __init__(self) -> None:
        self._agents: dict[str, WebSocket] = {}
        self._dashboards: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect_agent(self, agent_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            previous = self._agents.get(agent_id)
            self._agents[agent_id] = websocket
        if previous and previous is not websocket:
            await previous.close(code=1012, reason="Replaced by a newer connection")

    async def disconnect_agent(self, agent_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._agents.get(agent_id) is websocket:
                self._agents.pop(agent_id, None)

    async def connect_dashboard(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._dashboards.add(websocket)

    async def disconnect_dashboard(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._dashboards.discard(websocket)

    async def send_agent(
        self,
        agent_id: str,
        message: BaseMessage | dict[str, Any],
    ) -> bool:
        websocket = self._agents.get(agent_id)
        if websocket is None:
            return False
        payload = message.to_json() if isinstance(message, BaseMessage) else json.dumps(message)
        try:
            await websocket.send_text(payload)
            return True
        except Exception:
            await self.disconnect_agent(agent_id, websocket)
            return False

    async def broadcast_dashboards(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload)
        stale: list[WebSocket] = []
        for websocket in tuple(self._dashboards):
            try:
                await websocket.send_text(raw)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    self._dashboards.discard(websocket)

    def connected_agent_ids(self) -> list[str]:
        return list(self._agents)

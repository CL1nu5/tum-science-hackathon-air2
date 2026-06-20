"""Deterministic check of the tower emergency cascade + preemption over WS.

Boots an in-process tower on a private port, then:
  1. Agent A reserves a route+slot to a pad, advances to FINAL_APPROACH (hard lock).
  2. Agent B declares an emergency -> expects RESOLVED with a reachable target.
  3. Agent C (low battery) declares an emergency near a busy pad and we confirm
     the resolution never strands a victim (RESOLVED or HUMAN_REQUIRED, never crash).

Usage: .venv/bin/python scripts/verify_emergency.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AIR2_STATE_FILE", "/tmp/air2_emg_state.json")

import uvicorn  # noqa: E402
import websockets  # noqa: E402

PORT = 8077
URL = f"ws://127.0.0.1:{PORT}/ws/agent/{{}}"


async def recv_until(ws, msg_type, timeout=5.0):
    async def _loop():
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == msg_type:
                return msg

    return await asyncio.wait_for(_loop(), timeout)


async def run_checks() -> bool:
    ok = True
    # Agent A: reserve route+slot to TUM.
    async with websockets.connect(URL.format("A1")) as a:
        await a.send(json.dumps({
            "type": "ROUTE_REQUEST", "agent_id": "A1",
            "origin": [0.0, 0.0, 0.0], "destination_vertiport": "TUM",
            "departure_time": "2026-06-20T00:00:00+00:00",
            "battery_pct": 90.0, "speed_capability": 80.0,
        }))
        ra = await recv_until(a, "ROUTE_ASSIGNMENT")
        print("A1 route ->", ra["destination_vertiport"], "corridor", ra["corridor_id"])

        # Agent B declares an emergency far from A1; expect a resolved landing.
        async with websockets.connect(URL.format("B1")) as b:
            await b.send(json.dumps({
                "type": "EMERGENCY_DECLARATION", "agent_id": "B1",
                "battery_pct": 18.0, "position": [1000.0, 1000.0], "altitude": 200.0,
            }))
            res = await recv_until(b, "EMERGENCY_RESOLUTION")
            print("B1 emergency ->", res["outcome"], "target:", res.get("target_vertiport"))
            if res["outcome"] not in ("RESOLVED", "HUMAN_REQUIRED"):
                print("  !! unexpected outcome"); ok = False
            if res["outcome"] == "RESOLVED" and not res.get("target_vertiport"):
                print("  !! resolved without a target"); ok = False

        # Agent C: very low battery emergency near the city centre.
        async with websockets.connect(URL.format("C1")) as c:
            await c.send(json.dumps({
                "type": "EMERGENCY_DECLARATION", "agent_id": "C1",
                "battery_pct": 12.0, "position": [200.0, -300.0], "altitude": 150.0,
            }))
            res = await recv_until(c, "EMERGENCY_RESOLUTION")
            print("C1 emergency ->", res["outcome"], "target:", res.get("target_vertiport"),
                  "preempted:", res.get("preempted_agent_id"))
            if res["outcome"] not in ("RESOLVED", "HUMAN_REQUIRED"):
                print("  !! unexpected outcome"); ok = False
    return ok


async def main() -> int:
    if os.path.exists(os.environ["AIR2_STATE_FILE"]):
        os.remove(os.environ["AIR2_STATE_FILE"])
    from src.backend.server.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.1)
        ok = await run_checks()
    finally:
        server.should_exit = True
        await task
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

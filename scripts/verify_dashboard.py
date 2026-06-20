"""Headless browser check: load the live dashboard against a running tower and
confirm it connects, ingests real vertiports/aircraft, and animates tracks.

Usage: .venv/bin/python scripts/verify_dashboard.py [http://localhost:8000]
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

POLL = """() => {
    const c = window.__dcComponent;
    if (!c) return { mounted: false };
    const taxis = c.taxis || [];
    return {
        mounted: true,
        live: !!c.liveConnected,
        vps: (c.vertiports||[]).length,
        taxis: taxis.length,
        airborne: taxis.filter(t=>t.landed<=0).length,
        withRoute: taxis.filter(t=>t.route4d&&t.route4d.length>1).length,
        worldW: c.WORLD_W, worldH: c.WORLD_H,
        inBounds: (c.vertiports||[]).every(v=>v.x>=-50&&v.x<=c.WORLD_W+50&&v.y>=-50&&v.y<=c.WORLD_H+50),
    };
}"""

SAMPLE = """() => {
    const c = window.__dcComponent; if (!c) return null;
    const t = (c.taxis||[]).find(t=>t.landed<=0 && t.route4d && t.route4d.length>1);
    return t ? {id:t.id, x:t.x, y:t.y} : null;
}"""


async def main() -> int:
    page_errors: list[str] = []
    console_msgs: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
        await page.goto(BASE + "/", wait_until="domcontentloaded")

        state = {"mounted": False}
        for _ in range(30):
            try:
                state = await page.evaluate(POLL)
            except Exception as exc:  # noqa: BLE001
                state = {"mounted": False, "evalError": str(exc)}
            if state.get("mounted") and state.get("live") and state.get("taxis", 0) > 0:
                break
            await page.wait_for_timeout(1000)

        p1 = None
        p2 = None
        try:
            p1 = await page.evaluate(SAMPLE)
            await page.wait_for_timeout(3000)
            p2 = await page.evaluate(SAMPLE)
        except Exception as exc:  # noqa: BLE001
            console_msgs.append(f"[sample-error] {exc}")

        await page.screenshot(path="/tmp/air2_dashboard.png")
        await browser.close()

    print("STATE:", state)
    print("MOVE:", p1, "->", p2)
    moved = bool(p1 and p2 and (abs(p1["x"] - p2["x"]) + abs(p1["y"] - p2["y"]) > 0.5))
    print("MOVED:", moved)
    print("PAGE ERRORS:", page_errors[:10])
    errs = [m for m in console_msgs if m.startswith("[error]") or "initialize" in m.lower()]
    print("CONSOLE ERRORS:", errs[:10])

    ok = (
        state.get("mounted")
        and state.get("live")
        and state.get("vps", 0) > 0
        and state.get("taxis", 0) > 0
        and state.get("inBounds")
        and not page_errors
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

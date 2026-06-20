"""Load the LIVE dashboard in a headless browser and sample the rendered
per-taxi altitude over time, proving the 0<->150 m altitude hop is gone and that
tracks actually animate (move). Run against a running tower on :8000.
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
DURATION_S = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0

SAMPLE = """() => {
    const c = window.__dcComponent;
    if (!c || !c.taxis) return null;
    return {
        live: !!c.liveConnected,
        taxis: c.taxis.map(t => ({
            id: t.callsign || t.id,
            alt: Math.round(t.altitude_m || 0),
            x: Math.round(t.x || 0),
            y: Math.round(t.y || 0),
            landed: t.landed > 0,
        })),
    };
}"""


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.goto(BASE, wait_until="networkidle")
        await asyncio.sleep(3.0)

        history: dict[str, list] = {}
        live = False
        samples = 0
        for _ in range(int(DURATION_S * 2)):
            snap = await page.evaluate(SAMPLE)
            if snap:
                live = live or snap["live"]
                samples += 1
                for t in snap["taxis"]:
                    history.setdefault(t["id"], []).append((t["alt"], t["x"], t["y"], t["landed"]))
            await asyncio.sleep(0.5)
        await browser.close()

    # --- analysis ---
    print(f"live_connected={live}  samples={samples}  taxis_tracked={len(history)}")
    hops = 0
    moved_taxis = 0
    worst = []
    for tid, series in history.items():
        alts = [s[0] for s in series]
        # count >80 m jumps between consecutive 0.5s samples (a teleport)
        taxi_hops = sum(
            1 for i in range(1, len(alts))
            if abs(alts[i] - alts[i - 1]) > 80 and not (series[i][3] or series[i-1][3])
        )
        hops += taxi_hops
        # did it move (airborne)?
        xs = [s[1] for s in series]; ys = [s[2] for s in series]
        span = max((abs(xs[i]-xs[0]) + abs(ys[i]-ys[0])) for i in range(len(xs)))
        if span > 200:
            moved_taxis += 1
        if taxi_hops:
            worst.append((tid, taxi_hops, alts[:30]))

    print(f"rendered altitude >80 m/0.5s teleports (the hop bug): {hops}")
    print(f"taxis that visibly moved (>200 m): {moved_taxis}/{len(history)}")
    for tid, h, alts in worst[:5]:
        print(f"  HOP {tid}: {h} jumps; alt trace {alts}")
    ok = live and hops == 0 and moved_taxis > 0
    print("RESULT:", "PASS — smooth altitude, tracks animate" if ok else "CHECK — see above")


if __name__ == "__main__":
    asyncio.run(main())

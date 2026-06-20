"""Telemetry probe: poll the running tower and record per-agent time series so we
can detect altitude oscillation, deadlocks (stuck agents), and stand/slot
exhaustion. Run AFTER the demo is up on :8000.

    .venv/bin/python scripts/telemetry_probe.py [seconds] [interval]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import defaultdict

BASE = "http://127.0.0.1:8000"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read().decode())


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    hist: dict[str, list] = defaultdict(list)  # agent_id -> [(t, alt, stage, slot, pos, dest)]
    samples = []
    t0 = time.time()
    while time.time() - t0 < duration:
        ts = round(time.time() - t0, 1)
        try:
            agents = get("/api/agents")
            verts = get("/api/vertiports")
            ress = get("/api/reservations")
        except Exception as e:
            print(f"[{ts}] poll error: {e}")
            time.sleep(interval)
            continue

        # stand occupancy per vertiport
        stand_occ = {}
        stand_total = {}
        for v in verts:
            stands = v.get("stands", [])
            stand_total[v["vertiport_id"]] = len(stands)
            stand_occ[v["vertiport_id"]] = sum(1 for s in stands if s.get("occupied_by"))

        stage_count = defaultdict(int)
        for a in agents:
            stage_count[a.get("flight_stage")] += 1
            hist[a["agent_id"]].append((
                ts,
                round(a.get("altitude_m", 0), 1),
                a.get("flight_stage"),
                a.get("slot_stage"),
                tuple(round(p, 0) for p in a.get("position", [0, 0])),
                a.get("destination_vertiport"),
                round(a.get("battery_pct", 0), 1),
            ))
        active_res = [r for r in ress]
        samples.append((ts, dict(stage_count), sum(stand_occ.values()), sum(stand_total.values()), len(active_res)))
        time.sleep(interval)

    # ---- Analysis ----
    print("\n==================== STAGE TIMELINE ====================")
    for ts, sc, occ, tot, nres in samples[::max(1, len(samples)//30)]:
        line = " ".join(f"{k}={v}" for k, v in sorted(sc.items()))
        print(f"t={ts:6.1f} stands={occ}/{tot} pad_res={nres} | {line}")

    print("\n==================== ALTITUDE OSCILLATION ====================")
    # Detect agents whose altitude flips direction many times (hops)
    osc = []
    for aid, series in hist.items():
        alts = [s[1] for s in series]
        flips = 0
        big_jumps = 0
        for i in range(2, len(alts)):
            d1 = alts[i-1] - alts[i-2]
            d2 = alts[i] - alts[i-1]
            if d1 * d2 < 0 and abs(d1) > 5 and abs(d2) > 5:
                flips += 1
            if abs(alts[i] - alts[i-1]) > 80:  # >80m jump in one sample
                big_jumps += 1
        osc.append((aid, flips, big_jumps, alts))
    osc.sort(key=lambda x: (x[2], x[1]), reverse=True)
    for aid, flips, jumps, alts in osc[:8]:
        print(f"{aid}: dir-flips={flips} big-jumps(>80m/s)={jumps}")
        # print a compact altitude trace
        trace = "".join(
            "_" if a < 10 else ("^" if a > 140 else "-") for a in alts
        )
        print(f"   alt[{min(alts):.0f}..{max(alts):.0f}] {trace[:120]}")

    print("\n==================== DEADLOCK / STUCK DETECTION ====================")
    # An agent is "stuck" if its position barely changes while it is in an
    # airborne stage (should be moving), or it sits in PRE_FLIGHT/AWAITING forever.
    for aid, series in hist.items():
        if len(series) < 10:
            continue
        last = series[-20:]
        stages = [s[2] for s in last]
        positions = [s[4] for s in last]
        moved = max(
            ((positions[i][0]-positions[0][0])**2 + (positions[i][1]-positions[0][1])**2) ** 0.5
            for i in range(len(positions))
        )
        airborne = all(st in ("CLIMBING", "EN_ROUTE", "DESCENDING", "FINAL_APPROACH") for st in stages)
        pre = all(st in ("PRE_FLIGHT", "AWAITING_TAKEOFF") for st in stages)
        if airborne and moved < 30:
            print(f"STUCK-AIRBORNE {aid}: stage={stages[-1]} moved={moved:.0f}m batt={last[-1][6]}")
        if pre:
            print(f"STUCK-PREFLIGHT {aid}: stage={stages[-1]} for {len(last)} samples batt={last[-1][6]}")

    # Final distribution
    print("\n==================== FINAL STATE ====================")
    final_stage = defaultdict(int)
    for aid, series in hist.items():
        final_stage[series[-1][2]] += 1
    for k, v in sorted(final_stage.items()):
        print(f"  {k}: {v}")
    print(f"  total agents tracked: {len(hist)}")


if __name__ == "__main__":
    main()

# Electric Air-Taxi System - Consolidated Concept

**Goal:** Resolve conflicts before take-off through perfect tower planning, and resolve the unavoidable rest locally and autonomously between affected objects - keeping noise low, emergency landing sites in reach, and flight safe, stable, and predictable.

## 1. Map & Infrastructure
* **Vertiport:** Map point with one serial landing pad (one take-off/landing at a time) + several surrounding parking stands.
* Map also shows other operators' vertiports and emergency surfaces: light-red (prepared) and dark-red (open field).
* Distances drive ETA and remaining range; energy reachability underpins all routing and emergency logic.

## 2. 4D Route Reservation (pre-take-off)
* **Principle:** Airborne objects keep their route; new objects must secure a conflict-free route before take-off. Conflicts are designed out on the ground.
* Tower plans the initial route with full overview of all active reservations, it hands out an already conflict-free, optimal route.
* **4D:** x, y, altitude (z), time (t). Two objects can share a location at different times.
* **Route:** Reserved safety corridor, not a single line - horizontal + vertical margins + temporal buffer absorb small deviations.
* **Noise rule:** The tower spreads traffic over residential areas and limits how often any corridor is overflown; acoustic load is shared, not dumped on a few neighborhoods.
* **Emergency reachability built in:** No point on a route is more than ~30 min flight time from a suitable landing site.
* **Conflict check before launch:** A route is invalid if it intersects an existing corridor at/near the same time.
* **Resolution order:** delay take-off → change altitude → adjust speed → recompute alternative route.
* **Take-off flow:** request route → query reservations → adjust route/time if needed → reserve conflict-free route → launch only after confirmation.

## 3. Pad Slots & Stands
* Booking locks a landing-time slot on the pad, not a stand. The stand is assigned secondarily, just before landing.
* Every slot is leased and auto-frees if the taxi drops out.
* **Lifecycle:** tentative → firm far → firm near → final approach → on pad → parked. Closer ETA = more protected slot.
* Tentative can be rebooked to an equivalent free window for free (not a preemption).
* **Slot-in (non-destructive):** The pad is one timeline; if A's slot is far ahead, an earlier B may use the gap before it - provided B clears before A and a stand is free. A's slot is untouched; on conflict B yields and keeps its own slot as fallback.
* **Pad-standby (normal):** A taxi lands only if a stand is free to clear into; otherwise it holds/rebooks so the pad stays clear.

## 4. Priority Model
* **Guiding principle (makes the whole system consistent):** Every object must, at all times, have ≥ 1 reachable, free landing option within its energy budget.
* Reachable = energy-to-reach + safety reserve < remaining energy. All preemption and priority derive from this single rule.
* **Priority:** Who is more important = who is most constrained (fewest reachable options + least energy reserve). That object acts first. A measurable quantity replaces arbitrary ranking.
* **Status = two tiers only:**
    * **Normal:** flies destination/backup as planned; may not preempt.
    * **Emergency:** failure, low/empty battery, etc.; may preempt if the rules below hold.
* Status is just a gate (may preempt: yes/no), not a ranking. Fine-ordering among emergencies is done only by the constraint metric above.

## 5. Emergency Landing Cascade
* **Air-side escalation:** nearest reachable first: own slot/destination → slot-in → free slot → preempt a lower-priority slot → other operator's vertiport → light-red surface → dark-red field → trailing spot → human. (The "nearest reachable first" ordering produces graceful graduation by itself - the field is only reached when nothing else is.)
* **Ground-side:** free stand → (emergency only) pad-standby.
* **Preemption allowed when:** own options (destination + backup) are gone AND status = Emergency AND the victim keeps ≥ 1 reachable option afterward (guiding principle preserved for them).
* **Preemption forbidden (overrides everything) when:** the victim is in final approach / on pad / parked, or would be left with no option.
* Normal never preempts.
* **Chain reaction:** A preemption forces the victim to replan (drops to its backup) and may bump the next object (B→C→...). Fine as long as the guiding principle holds at every step. A bumped taxi shifts to a later slot or another vertiport and is then immune (max one reroute).
* **Tie-break (two emergencies, last free spot):** lower remaining energy wins; if still unresolved → human.
* **Pad-standby (emergency):** stand requirement waived - if no stand is free, the taxi may stay on the pad until one opens, blocking further landings (accepted cost of an emergency).
* **Trailing (simulated):** last automatic step before human. With no real object detection, the sim injects ad-hoc spots (field, intersection) with a suitability score; this feeds the cascade as lowest priority and may optionally be shared.
* **Human takes over when:** a pad fails unexpectedly (infrastructure, unpredictable for the system); an emergency has no stealable spot and no usable trailing spot; two equally critical cases want the same last spot; or the automation finds no solution within X seconds (hard timeout - required, or the system can hang in the worst case).

## 6. Communication
* **Role split (resolves "decentralized first" vs "tower controls locks"):**
    * **Normal operation:** the Tower is the sole lock authority, one source of truth, no race conditions.
    * **V2V layer:** redundancy + local coordination; takes over only if the tower drops out. That is what makes "decentralized first" honest - it kicks in exactly when it matters.
* **V2V (object-to-object):** each periodically broadcasts position (like ADS-B). When another comes within < 5 km → handshake → exchange battery, speed, route, intent. Purpose: collision avoidance, local coordination, lock negotiation as fallback.
* **Local rerouting:** Each object has a critical radius. Outside it, objects keep tower-assigned routes. The moment two enter each other's radius they reroute autonomously between themselves by the same priority rule - lower-priority yields, higher-priority holds - without waiting for the tower.
* **Tower (server):** permanent connection; aggregates everything for visualization/control, issues and manages pad locks, makes emergency decisions, supplies weather.
* **Tower failure (what makes it truly decentralized):** losing the tower, objects within radio range negotiate directly by the same fixed rule (guiding principle + Normal/Emergency status). On reconnect the tower re-syncs state. Without this defined fallback the system is effectively central despite V2V.
* **Shared store:** all reserved routes live centrally (ground station / reservation manager / UTM). Lets the tower plan perfect routes and every object see the live airspace. After any local rerouting, resolved routes are written back so the picture stays consistent.
* **Message types (keep small):** Position-Broadcast, State-Update (battery/speed/route/intent), Lock-Request/Grant/Release, Emergency-Declaration (preemption), Handshake init/ack.
* **Two parameters to fix in the sim:**
    * 5 km coupled to closing speed: two taxis flying toward each other have high closing speed, so 5 km can be tight; make it configurable.
    * Broadcast interval: defines how current the local picture is.

## 7. Invariants
* Hard-lock states (final approach / on pad/parked) are untouchable.
* Pad is serial; clear promptly (except emergency standby).
* Normal never preempts.
* Slot-in is always non-destructive.
* Stand is secondary, never fixed at booking; actual occupancy always overrides the plan.
* Every object always retains ≥ 1 reachable landing option (the guiding principle).
electric_air_taxi_system.md
electric_air_taxi_system.md wird angezeigt.
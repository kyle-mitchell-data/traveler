"""Residence and handoff.

Each stay is one Lambda invocation: arrive (if handed a suitcase), reside, then either
fly to the next region or rest. The code talks to the outside world only through
`World`, so the same state machine runs in AWS (traveler.world.AwsWorld) and in the
local simulation (tests/sim.py).

Flight phases (spec "Lifecycle and handoff", amended by D21):

    old  P1 DEPARTED         flight clock starts
    old  P2 FUNCTION_CREATED  ┐
    old  P3 FUNCTION_ACTIVE   ├ traveler.placement.create
    old  P4 URL_READY         ┘
    old  P5 HEALTH_OK         direct call answers with the right region and release
    old  P6 ROUTE_FLIPPED     routing keys point at the new copy
    old  P7 ROUTE_CONFIRMED   >= EDGE_PROPAGATION_S since P6 and CONFIRM_RUN probes in a row
    new  P8 ARRIVED           new copy wins the hop (conditional write), old records its exit
    new  P9 LANDED            new copy deletes the old one; flight clock stops
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from traveler import placement, regions
from traveler.facts import engine
from traveler.facts.model import Context
from traveler.routing import EDGE_PROPAGATION_S
from traveler.tower import Tower, now_iso

STAY_RANGE_S = (180, 300)
RETRY_STAY_S = (60, 120)
PACKING_S = 30
HEARTBEAT_S = 30
FLIGHT_CAP_S = 180  # D21
CONFIRM_RUN = 10
ARRIVAL_WAIT_S = 60
OLD_EXIT_WAIT_S = 60
RECENT_REGIONS = 5
# Time a stay must leave for a full flight that then rolls back, plus margin.
RESERVE_S = FLIGHT_CAP_S + EDGE_PROPAGATION_S + 30


class World(Protocol):
    region: str
    tower: Tower

    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def remaining_s(self) -> float: ...
    def lambda_client(self, region: str) -> Any: ...
    def set_route(self, **keys: str) -> None: ...
    def probe(self) -> str | None: ...
    def package(self, release: str) -> bytes: ...
    def invoke_async(self, region: str, payload: dict[str, Any]) -> None: ...
    def setting(self, name: str) -> str | None: ...
    def placement_args(self, *, release: str, hop_id: str) -> dict[str, Any]: ...
    def log(self, msg: str, **fields: Any) -> None: ...
    def metric(self, **values: float) -> None: ...
    def fetch_json(self, url: str) -> Any: ...


# --- entry -------------------------------------------------------------------


def run(world: World, suitcase: dict[str, Any]) -> dict[str, Any]:
    """One invocation = one stay."""
    s = dict(suitcase)
    if s.get("from_region") and not arrive(world, s):
        return {"outcome": "arrival_rejected", "hop_id": s["hop_id"]}
    outcome = reside(world, s)
    if outcome.startswith("fly:"):
        return fly(world, s, outcome.removeprefix("fly:"))
    return {"outcome": outcome, "hop_id": s.get("hop_id")}


# --- arrival (new copy, P8-P9) -------------------------------------------------


def arrive(world: World, s: dict[str, Any]) -> bool:
    t = world.tower
    hop_id = s["hop_id"]
    if world.region != s.get("to_region"):
        world.log("suitcase addressed elsewhere", to=s.get("to_region"))
        return False
    if not t.try_arrive(hop_id):
        # The old copy already gave up on this hop and is rolling back; it deletes us.
        world.log("hop already aborted; standing down", hop_id=hop_id)
        return False
    t.mark_phase(hop_id, "ARRIVED")
    _phase_event(world, s, "ARRIVED")
    t.event("ARRIVED", hop_id=hop_id, region=world.region)

    # Invariant: never delete the old copy before it has recorded its exit.
    deadline = world.now() + OLD_EXIT_WAIT_S
    while world.now() < deadline and not t.hop(hop_id).get("old_exited_at"):
        world.sleep(1)
    if not t.hop(hop_id).get("old_exited_at"):
        t.event("INVARIANT_VIOLATION", hop_id=hop_id, region=world.region, reason="old copy never recorded its exit")

    placement.remove(world.lambda_client(s["from_region"]))
    flight_ms = int((world.now() - s["flight_started_at"]) * 1000)
    t.land(hop_id, flight_ms=flight_ms)
    t.mark_phase(hop_id, "LANDED")
    _phase_event(world, s, "LANDED")
    s["stats"] = {**s.get("stats", {}), "hops_ok": s.get("stats", {}).get("hops_ok", 0) + 1}
    s["consecutive_aborts"] = 0
    t.event("LANDED", hop_id=hop_id, region=world.region, origin=s["from_region"], flight_ms=flight_ms)
    world.log("landed", hop_id=hop_id, flight_ms=flight_ms)
    return True


# --- residence ---------------------------------------------------------------


def reside(world: World, s: dict[str, Any]) -> str:
    """Stay, post, pack. Returns "fly:<region>", "rest:<reason>" or "stopped"."""
    t = world.tower
    rng = random.Random(f"{s['hop_id']}:stay")
    override = _int(world.setting("stay_seconds"))
    stay = s.get("stay_s") or override or rng.randint(*STAY_RANGE_S)
    stay = max(PACKING_S + 5, min(stay, world.remaining_s() - RESERVE_S))
    arrived = world.now()
    depart_at = arrived + stay
    t.set_state(
        status="live",
        region=world.region,
        hop_id=s["hop_id"],
        hop_count=s.get("hop_count", 0),
        release=s.get("release", ""),
        phase="RESIDENT",
        arrived_at=now_iso(),
        departure_at=_iso_in(world, stay),
        next_region="",
        heartbeat_at=now_iso(),
        hops_ok=s.get("stats", {}).get("hops_ok", 0),
        hops_aborted=s.get("stats", {}).get("hops_aborted", 0),
        consecutive_aborts=s.get("consecutive_aborts", 0),
    )
    t.event("ARRIVAL_STARTED", hop_id=s["hop_id"], region=world.region, stay_s=int(stay))
    statements = _plan_statements(world, s, stay)
    posted: list[str] = []
    try:
        return _stay(world, s, arrived, depart_at, statements, posted)
    finally:
        if posted:
            t.remember_visit(world.region, posted, visits=engine.MEMORY_VISITS, recent=engine.MEMORY_FACTS)


def _stay(
    world: World,
    s: dict[str, Any],
    arrived: float,
    depart_at: float,
    statements: list[dict[str, Any]],
    posted: list[str],
) -> str:
    t = world.tower
    dest: str | None = None
    next_beat = world.now() + HEARTBEAT_S
    while world.now() < depart_at:
        # Slot k (0-based) is said k * SLOT_S after arrival; slot 1 straight away.
        while len(posted) < len(statements) and world.now() >= arrived + engine.SLOT_S * len(posted):
            st = statements[len(posted)]
            t.event("STATEMENT", hop_id=s["hop_id"], region=world.region, **st)
            posted.append(st["fact_id"])
        if world.now() >= next_beat:
            next_beat += HEARTBEAT_S
            if t.state().get("status") != "live":
                world.log("put to sleep from outside; exiting")
                return "stopped"
            if not placement.exists(world.lambda_client(world.region)):
                # Deleted from outside. Stop heartbeating so the watchdog resurrects us.
                t.event(
                    "INVARIANT_VIOLATION", hop_id=s["hop_id"], region=world.region, reason="my function was deleted"
                )
                world.log("own function deleted; exiting")
                return "stopped"
            t.set_state(heartbeat_at=now_iso())
        if dest is None and world.now() >= depart_at - PACKING_S:
            dest = choose_destination(world, s)
            t.set_state(phase="PACKING", next_region=dest)
            t.event("PACKING_STARTED", hop_id=s["hop_id"], region=world.region, destination=dest)
        world.sleep(min(1.0, max(0.0, depart_at - world.now())))

    if t.state().get("status") != "live":
        return "stopped"
    if (world.setting("kill") or "").lower() == "true":
        return go_to_rest(world, s, "kill switch")
    limit_h = _int(world.setting("auto_sleep_hours"))
    awake_since = _epoch(str(t.state().get("awake_since", "")))
    pinned = (world.setting("pin_awake") or "").lower() == "true"
    if limit_h and awake_since and not pinned and _wall_now() - awake_since >= limit_h * 3600:
        return go_to_rest(world, s, f"auto-sleep after {limit_h} h awake")
    max_hops = _int(world.setting("max_hops"))
    if max_hops is not None and s.get("hop_count", 0) >= max_hops:
        return go_to_rest(world, s, f"reached max_hops={max_hops}")
    if s.get("consecutive_aborts", 0) >= 3:
        t.event("INVARIANT_VIOLATION", hop_id=s["hop_id"], region=world.region, reason="3 aborted hops in a row")
    dest = dest or choose_destination(world, s)
    if dest == world.region:
        return go_to_rest(world, s, "nowhere else to go")
    return "fly:" + dest


def choose_destination(world: World, s: dict[str, Any]) -> str:
    """Random from the pool, never here, not the last 5, not a region that just failed."""
    pool = _list(world.setting("hop_regions")) or _list(world.setting("allowlist")) or [world.region]
    recent = s.get("recent_regions", [])[:RECENT_REGIONS]
    avoid = set(s.get("avoid", []))
    rng = random.Random(f"{s['hop_id']}:dest")
    for candidates in (
        [r for r in pool if r != world.region and r not in recent and r not in avoid],
        [r for r in pool if r != world.region and r not in avoid],
        [r for r in pool if r != world.region],
    ):
        if candidates:
            return rng.choice(sorted(candidates))
    return world.region


def go_to_rest(world: World, s: dict[str, Any], reason: str) -> str:
    t = world.tower
    world.set_route(status="resting")
    t.set_state(status="resting", phase="SETTLING")
    world.sleep(EDGE_PROPAGATION_S)
    t.set_state(phase="RESTING")
    t.event("RESTING", hop_id=s["hop_id"], region=world.region, reason=reason)
    world.log("resting", reason=reason)
    placement.remove(world.lambda_client(world.region))
    return f"rest:{reason}"


# --- flight (old copy, P1-P8) ---------------------------------------------------


def fly(world: World, s: dict[str, Any], dest: str) -> dict[str, Any]:
    t = world.tower
    hop_id = t.next_hop_id()
    t0 = world.now()
    release = t.desired_release() or s.get("release", "")
    flight = {**s, "hop_id": hop_id, "from_region": world.region, "to_region": dest, "flight_started_at": t0}

    def phase(name: str) -> None:
        t.mark_phase(hop_id, name)
        _phase_event(world, flight, name)
        t.set_state(phase=name, next_region=dest)

    # Our own URL is the rollback target. If we have been deleted, do not build a copy.
    try:
        home_origin = placement.url_domain(world.lambda_client(world.region))
    except Exception:  # noqa: BLE001
        world.log("own function missing at departure; not flying")
        return {"outcome": "stopped", "hop_id": s.get("hop_id")}

    t.start_hop(hop_id, from_region=world.region, to_region=dest, release=release)
    phase("DEPARTED")
    new = world.lambda_client(dest)

    # P2-P5: build and check the new copy. Any failure: delete it, stay put.
    try:
        placed = placement.create(new, **world.placement_args(release=release, hop_id=hop_id), on_phase=phase)
        placement.health(new, expect_release=release)
        phase("HEALTH_OK")
    except placement.PlacementError as exc:
        placement.remove(new)
        return _abort(world, s, hop_id, dest, exc.phase, exc.reason, flipped=False)
    except Exception as exc:  # noqa: BLE001 - e.g. package fetch; still a P2 abort
        placement.remove(new)
        return _abort(world, s, hop_id, dest, "FUNCTION_CREATED", str(exc), flipped=False)

    # P6: point the edge at the new copy.
    try:
        world.set_route(current_origin=placed.url_domain, current_region=dest, hop_id=hop_id, status="live")
    except Exception as exc:  # noqa: BLE001
        placement.remove(new)
        return _abort(world, s, hop_id, dest, "ROUTE_FLIPPED", str(exc), flipped=False)
    phase("ROUTE_FLIPPED")
    flipped_at = world.now()

    # P7: the edge must settle on the new region (D21). We keep serving until then.
    run_ = 0
    while not (world.now() - flipped_at >= EDGE_PROPAGATION_S and run_ >= CONFIRM_RUN):
        if world.now() - t0 > FLIGHT_CAP_S:
            return _rollback(world, s, hop_id, dest, home_origin, "ROUTE_CONFIRMED", "edge did not settle in time")
        run_ = run_ + 1 if world.probe() == dest else 0
        world.sleep(1)
    phase("ROUTE_CONFIRMED")

    # P8: hand over the suitcase and wait for the new copy to claim the hop.
    recent = [dest, *s.get("recent_regions", [])][: RECENT_REGIONS + 1]
    suitcase = {
        "mode": "residence",
        "journey_id": s.get("journey_id", ""),
        "hop_id": hop_id,
        "hop_count": s.get("hop_count", 0) + 1,
        "from_region": world.region,
        "to_region": dest,
        "recent_regions": recent,
        "release": release,
        "flight_started_at": t0,
        "stats": s.get("stats", {}),
        "consecutive_aborts": s.get("consecutive_aborts", 0),
    }
    try:
        world.invoke_async(dest, suitcase)
    except Exception as exc:  # noqa: BLE001
        world.log("async invoke failed", error=str(exc))
    deadline = world.now() + ARRIVAL_WAIT_S
    while world.now() < deadline and t.hop(hop_id).get("status") != "arrived":
        world.sleep(1)
    # If try_abort loses the race, the new copy arrived just now: carry on.
    if t.hop(hop_id).get("status") != "arrived" and t.try_abort(
        hop_id, phase="ARRIVED", reason=f"no ARRIVED within {ARRIVAL_WAIT_S} s"
    ):
        return _rollback(world, s, hop_id, dest, home_origin, "ARRIVED", "", recorded=True)

    # P9 (old side): record the exit; the new copy deletes us once it sees this.
    t.mark_old_exited(hop_id)
    t.event("OLD_EXITED", hop_id=hop_id, region=world.region)
    world.log("handed over", hop_id=hop_id, to=dest)
    return {"outcome": "handed_over", "hop_id": hop_id, "to": dest}


def _rollback(
    world: World,
    s: dict[str, Any],
    hop_id: str,
    dest: str,
    home_origin: str,
    phase: str,
    reason: str,
    *,
    recorded: bool = False,
) -> dict[str, Any]:
    """After P6: route back here, let the edge settle, then delete the new copy."""
    world.set_route(current_origin=home_origin, current_region=world.region, hop_id=s["hop_id"], status="live")
    world.sleep(EDGE_PROPAGATION_S)
    placement.remove(world.lambda_client(dest))
    return _abort(world, s, hop_id, dest, phase, reason, flipped=True, recorded=recorded)


def _abort(
    world: World,
    s: dict[str, Any],
    hop_id: str,
    dest: str,
    phase: str,
    reason: str,
    *,
    flipped: bool,
    recorded: bool = False,
) -> dict[str, Any]:
    t = world.tower
    if not recorded:
        t.try_abort(hop_id, phase=phase, reason=reason)
    reason = reason or t.hop(hop_id).get("abort_reason", "")
    stats = s.get("stats", {})
    stats = {**stats, "hops_aborted": stats.get("hops_aborted", 0) + 1}
    t.event("HOP_ABORTED", hop_id=hop_id, region=world.region, destination=dest, phase=phase, reason=reason)
    t.set_state(
        phase="RESIDENT",
        next_region="",
        hops_aborted=stats["hops_aborted"],
        consecutive_aborts=s.get("consecutive_aborts", 0) + 1,
    )
    world.log("hop aborted", hop_id=hop_id, phase=phase, reason=reason, flipped=flipped)

    # Stay 60-120 s more in a fresh invocation, then try somewhere else.
    rng = random.Random(f"{hop_id}:retry")
    again = {
        **s,
        "mode": "residence",
        "from_region": None,
        "stay_s": rng.randint(*RETRY_STAY_S),
        "avoid": [dest],
        "stats": stats,
        "consecutive_aborts": s.get("consecutive_aborts", 0) + 1,
    }
    world.invoke_async(world.region, again)
    return {"outcome": "aborted", "hop_id": hop_id, "phase": phase, "reason": reason}


# --- helpers -----------------------------------------------------------------


def _phase_event(world: World, s: dict[str, Any], name: str) -> None:
    world.tower.event(
        "PHASE",
        hop_id=s["hop_id"],
        region=world.region,
        phase=name,
        origin=s.get("from_region", ""),
        destination=s.get("to_region", ""),
        elapsed_ms=int((world.now() - s.get("flight_started_at", world.now())) * 1000),
    )


def _plan_statements(world: World, s: dict[str, Any], stay: float) -> list[dict[str, Any]]:
    """Gather and pick this stay's statements. Never raises: a broken engine still says hello."""
    try:
        ctx = Context(
            region=world.region,
            profile=regions.profile(world.region),
            now=datetime.now(UTC),
            hop_id=s["hop_id"],
            hop_count=int(s.get("hop_count") or 0),
            from_region=s.get("from_region") or "",
            fetch=world.fetch_json,
        )
        memory = world.tower.region_memory(world.region)
        plan = engine.plan(
            ctx, slots=engine.slots_for(stay), memory=memory, disabled=set(_list(world.setting("adapters_off")))
        )
    except Exception as e:  # noqa: BLE001
        world.log("fact engine failed", error=repr(e)[:300])
        return [_hello(world)]
    failures = sum(1 for r in plan.adapters.values() if not r.get("ok") and not r.get("skipped"))
    world.metric(StatementsPlanned=len(plan.statements), AdapterFailures=failures)
    world.log("facts planned", statements=len(plan.statements), candidates=plan.candidates, adapters=plan.adapters)
    world.tower.record_facts(
        s["hop_id"], {"statements": len(plan.statements), "candidates": plan.candidates, "adapters": plan.adapters}
    )
    return plan.statements or [_hello(world)]


def _hello(world: World) -> dict[str, Any]:
    p = regions.profile(world.region)
    return {
        "slot": 1,
        "category": "time",
        "text": f"Landed {p['near']}.",
        "source_name": "Computed",
        "source_url": "",
        "fact_id": f"hello-{world.region}",
    }


def _iso_in(world: World, seconds: float) -> str:
    then = datetime.now(UTC) + timedelta(seconds=seconds)
    return then.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _wall_now() -> float:
    # Wall clock, not world.now(): awake_since is a real timestamp in the tower.
    return datetime.now(UTC).timestamp()


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]

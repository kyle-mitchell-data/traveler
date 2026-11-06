"""Watchdog decisions, run every 5 minutes from the home region.

Enforces the invariants the traveler cannot enforce on itself:

- Extra copies: a traveler outside the region the tower names, older than any
  flight could be, is deleted (and reported).
- Leftovers after rest: copies still present 10 minutes after the traveler rested.
- Death: status is live but nothing has updated the tower for 10 minutes, or the
  copy the tower names is gone. The traveler is resurrected elsewhere from the
  latest release. The kill switch prevents resurrection.

It reads the tower and Lambda only, never the KeyValueStore (D5), and reports a
handful of metrics that the alarms watch (D18).
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Protocol

from traveler.tower import Tower

STALE_S = 600  # spec: no heartbeat for 10 minutes means dead
MISSING_GRACE_S = 120  # the tower's region briefly lacks a copy during a landing
EXTRA_AGE_S = 600  # older than any flight (180 s cap) plus rollback (90 s)
REST_SETTLE_S = 600  # sleep keeps a copy for EDGE_PROPAGATION_S before deleting it
VIOLATION_WINDOW_S = 300


class Ops(Protocol):
    tower: Tower

    def now(self) -> float: ...
    def copies(self) -> dict[str, float]: ...  # region -> seconds since the copy was created
    def delete(self, region: str) -> None: ...
    def setting(self, name: str) -> str | None: ...
    def resurrect(self, region: str, awake_since: str | None) -> str: ...
    def wait_for_edge(self) -> None: ...
    def metric(self, **values: float) -> None: ...
    def log(self, msg: str, **fields: Any) -> None: ...


def tick(ops: Ops) -> list[str]:
    t = ops.tower
    now = ops.now()
    state = t.state()
    status = state.get("status", "unknown")
    home = str(state.get("region", ""))
    copies = ops.copies()
    actions: list[str] = []
    violations = 0
    kill = (ops.setting("kill") or "").lower() == "true"
    liveness = max(_age(now, state.get("heartbeat_at")), 0.0)
    liveness = min(liveness, _age(now, state.get("updated_at")))

    if len(copies) > 2:
        violations += 1
        t.event("INVARIANT_VIOLATION", region=home, reason=f"{len(copies)} copies: {sorted(copies)}")

    # 1. Copies that should not exist.
    if status == "live":
        extras = [r for r, age in copies.items() if r != home and age > EXTRA_AGE_S]
    elif status == "resting" and _age(now, state.get("updated_at")) > REST_SETTLE_S:
        extras = list(copies)
    else:
        extras = []
    for r in extras:
        ops.delete(r)
        copies.pop(r, None)
        violations += 1
        t.event("INVARIANT_VIOLATION", region=r, reason=f"watchdog deleted a stray copy ({status})")
        actions.append(f"deleted stray copy in {r}")

    # 2. A dead traveler.
    dead = status == "live" and (liveness > STALE_S or (home not in copies and liveness > MISSING_GRACE_S))
    if dead and kill:
        actions.append("traveler looks dead but the kill switch is on; not resurrecting")
    elif dead:
        pool = [r for r in _list(ops.setting("allowlist")) if r != home and r not in copies] or [home]
        target = random.Random(f"{state.get('hop_id')}:resurrect").choice(sorted(pool))  # noqa: S311
        old = sorted(copies)
        reason = "no heartbeat for 10 minutes" if liveness > STALE_S else f"no copy in {home}"
        ops.log("resurrecting", target=target, reason=reason, old=old)
        region = ops.resurrect(target, state.get("awake_since"))
        t.event("RESURRECTED", region=region, origin=home, reason=reason)
        actions.append(f"resurrected in {region} ({reason})")
        if old:
            # A copy whose loop died can still serve visitors; let the edge move first (D21).
            ops.wait_for_edge()
            for r in old:
                ops.delete(r)
            actions.append(f"retired dead copies in {', '.join(old)}")
        copies = ops.copies()

    recent = [
        e
        for e in t.recent_events(50)
        if e.get("type") == "INVARIANT_VIOLATION" and _age(now, e.get("ts")) <= VIOLATION_WINDOW_S
    ]
    ops.metric(
        HeartbeatAgeSeconds=liveness if status == "live" else 0.0,
        Copies=float(len(copies)),
        ConsecutiveAborts=float(state.get("consecutive_aborts", 0) or 0),
        InvariantViolations=float(max(violations, len(recent))),
    )
    return actions


def _age(now: float, iso: Any) -> float:
    try:
        return now - datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]

"""gather -> score -> select -> render (spec "Fact engine"; D29).

Everything random is seeded from the hop ID, so a hop's statements are reproducible
from its candidate pool and the region's memory.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from traveler.facts import adapters
from traveler.facts.model import Context, Fact
from traveler.facts.templates import TEMPLATES

ADAPTER_TIMEOUT_S = 3.0
SLOT_S = 30
PER_CATEGORY = 2
MEMORY_VISITS = 3  # no fact repeats within this many visits to a region
MEMORY_FACTS = 50

BASE_WEIGHT = {
    "time": 3.0,
    "weather": 3.0,
    "holiday": 2.0,
    "sun": 2.0,
    "journey": 1.8,
    "quake": 1.5,
    "onthisday": 1.5,
    "nearby": 1.3,
    "money": 1.2,
    "country": 1.0,
}
PACK_WEIGHT = 1.6  # curated evergreen facts, whatever their category
FRESH_BOOST = {"live": 1.0, "today": 0.5, "evergreen": 0.0}
RECENT_PENALTY = 1.0  # said here before, but longer ago than MEMORY_VISITS


@dataclass
class Plan:
    statements: list[dict[str, Any]]
    adapters: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates: int = 0


def slots_for(stay_s: float) -> int:
    return max(1, int(stay_s // SLOT_S))


# --- gather ----------------------------------------------------------------------


def gather(
    ctx: Context, disabled: set[str] | frozenset[str] = frozenset()
) -> tuple[list[Fact], dict[str, dict[str, Any]]]:
    """All local adapters, plus every enabled network adapter in parallel under one deadline."""
    facts: list[Fact] = []
    report: dict[str, dict[str, Any]] = {}
    for name, fn in adapters.LOCAL.items():
        found, report[name] = _run(fn, ctx)
        facts.extend(found)

    wanted = {} if "all" in disabled else {n: fn for n, fn in adapters.NETWORK.items() if n not in disabled}
    for name in adapters.NETWORK.keys() - wanted.keys():
        report[name] = {"ok": False, "skipped": True}
    if wanted:
        pool = ThreadPoolExecutor(max_workers=len(wanted), thread_name_prefix="adapter")
        futures = {pool.submit(_run, fn, ctx): n for n, fn in wanted.items()}
        done, late = wait(futures, timeout=ADAPTER_TIMEOUT_S + 0.5)
        for f in done:
            found, report[futures[f]] = f.result()
            facts.extend(found)
        for f in late:
            report[futures[f]] = {"ok": False, "error": "timeout"}
        pool.shutdown(wait=False, cancel_futures=True)
    return facts, report


def _run(fn: adapters.Adapter, ctx: Context) -> tuple[list[Fact], dict[str, Any]]:
    started = time.monotonic()
    try:
        out = fn(ctx)
        return out, {"ok": True, "ms": round((time.monotonic() - started) * 1000), "n": len(out)}
    except Exception as e:  # noqa: BLE001 - a failed source never stops a stay
        return [], {"ok": False, "ms": round((time.monotonic() - started) * 1000), "error": type(e).__name__}


# --- score -----------------------------------------------------------------------


def score(facts: list[Fact], memory: dict[str, Any], seed: str) -> list[tuple[float, Fact]]:
    """Scored candidates, best first. Facts said in the last MEMORY_VISITS visits are dropped."""
    blocked = {fid for visit in (memory.get("visits") or [])[-MEMORY_VISITS:] for fid in visit}
    recent = set(memory.get("recent") or [])
    seen: set[str] = set()
    out = []
    for f in facts:
        fid = f.fact_id
        if fid in blocked or fid in seen:
            continue
        seen.add(fid)
        s = (PACK_WEIGHT if f.pack else BASE_WEIGHT.get(f.category, 1.0)) + FRESH_BOOST.get(f.freshness, 0.0) + f.rarity
        if fid in recent:
            s -= RECENT_PENALTY
        s += random.Random(f"{seed}:{fid}").uniform(0, 0.5)  # seeded tie-breaking
        out.append((round(s, 6), f))
    out.sort(key=lambda sf: (-sf[0], sf[1].fact_id))
    return out


# --- select ----------------------------------------------------------------------


def select(scored: list[tuple[float, Fact]], slots: int, *, budget: int = 20_000) -> list[Fact]:
    """Pick `slots` facts in order.

    Slot 1 is a "right now" fact; no category twice in a row; at most PER_CATEGORY per
    category; at least one curated evergreen fact when any is available. Depth-first in
    score order, so the first complete sequence found is the greedy one whenever greedy
    works; the node budget bounds the rare dead end, and the longest partial is kept.
    """
    pool = [f for _, f in scored]
    has_pack = any(f.pack for f in pool)
    best: list[Fact] = []
    nodes = 0

    def ok(f: Fact, seq: list[Fact], counts: dict[str, int]) -> bool:
        if not seq:
            return f.now
        if f.category == seq[-1].category or counts.get(f.category, 0) >= PER_CATEGORY:
            return False
        last = len(seq) == slots - 1
        return not (last and has_pack and not any(x.pack for x in seq) and not f.pack)

    def dfs(seq: list[Fact], used: set[str], counts: dict[str, int]) -> bool:
        nonlocal best, nodes
        if len(seq) > len(best):
            best = list(seq)
        if len(seq) == slots:
            return True
        for f in pool:
            nodes += 1
            if nodes > budget:
                return False
            if f.fact_id in used or not ok(f, seq, counts):
                continue
            seq.append(f)
            used.add(f.fact_id)
            counts[f.category] = counts.get(f.category, 0) + 1
            if dfs(seq, used, counts):
                return True
            seq.pop()
            used.discard(f.fact_id)
            counts[f.category] -= 1
        return False

    dfs([], set(), {})
    return best


# --- render ----------------------------------------------------------------------


def render(f: Fact, seed: str) -> str:
    options = TEMPLATES[f.template_key]
    template = random.Random(f"{seed}:{f.fact_id}:tpl").choice(options)
    text = template.format(**f.values)
    return text[:1].upper() + text[1:]  # "the United Kingdom's capital..." opens a sentence


def plan(
    ctx: Context,
    *,
    slots: int,
    memory: dict[str, Any] | None = None,
    disabled: set[str] | frozenset[str] = frozenset(),
) -> Plan:
    facts, report = gather(ctx, disabled)
    return choose(facts, slots=slots, memory=memory or {}, seed=ctx.hop_id, report=report)


def choose(
    facts: list[Fact], *, slots: int, memory: dict[str, Any], seed: str, report: dict[str, dict[str, Any]] | None = None
) -> Plan:
    """The deterministic half of plan(): same facts, memory and seed give the same statements."""
    visits = list(memory.get("visits") or [])
    for keep in range(len(visits), -1, -1):
        # Never go quiet: if the pool can't fill the stay without repeats, forget the
        # oldest remembered visit and try again, so the most recent stay is protected longest.
        mem = {**memory, "visits": visits[len(visits) - keep :]}
        scored = score(facts, mem, seed)
        if not any(f.now for _, f in scored):
            # Every right-now fact was said on a recent visit (possible only within the
            # same minute or hour). Repeat the best one rather than open with nothing.
            scored = [*score([f for f in facts if f.now], {}, seed)[:1], *scored]
        picked = select(scored, slots)
        if len(picked) == slots:
            break
    statements = [
        {
            "slot": i + 1,
            "category": f.category,
            "text": render(f, seed),
            "source_name": f.source_name,
            "source_url": f.source_url,
            "fact_id": f.fact_id,
        }
        for i, f in enumerate(picked)
    ]
    return Plan(statements=statements, adapters=report or {}, candidates=len(scored))

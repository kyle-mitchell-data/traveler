"""Phase 5 gate, locally: the fact engine against recorded API responses and offline.

Fixtures in tests/fixtures/facts/ are real responses for Tokyo, trimmed, recorded
2026-11-06.
"""

import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws
from sim import FakeCloud

from traveler import regions
from traveler.facts import adapters, engine, sun
from traveler.facts.model import Context, Fact
from traveler.facts.templates import TEMPLATES
from traveler.tower import Tower

FIXTURES = Path(__file__).parent / "fixtures" / "facts"
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)  # noon on a Saturday in Tokyo
SOURCES = {
    "https://api.met.no/": "weather",
    "https://earthquake.usgs.gov/": "quakes",
    "https://en.wikipedia.org/api/rest_v1/feed/onthisday/": "onthisday",
    "https://en.wikipedia.org/w/api.php": "nearby",
    "https://date.nager.at/": "holidays",
    "https://api.frankfurter.dev/": "fx",
}


def recorded(url: str) -> Any:
    for prefix, name in SOURCES.items():
        if url.startswith(prefix):
            return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    raise ConnectionError(url)


def ctx(region: str = "ap-northeast-1", *, hop: str = "h-000500", online: bool = True, **kw: Any) -> Context:
    args: dict[str, Any] = {"hop_count": 500, "from_region": "eu-central-1", **kw}
    c = Context(region=region, profile=regions.profile(region), now=NOW, hop_id=hop, **args)
    if online:
        c.fetch = recorded  # offline, the default fetch raises like a dead network
    return c


def remember(memory: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    """The same bookkeeping as Tower.remember_visit, in memory."""
    return {
        "visits": [*memory.get("visits", []), ids][-engine.MEMORY_VISITS :],
        "recent": [*memory.get("recent", []), *ids][-engine.MEMORY_FACTS :],
    }


def assert_rules(statements: list[dict[str, Any]], slots: int, *, packs: set[str], city: str) -> None:
    assert len(statements) == slots
    assert city in statements[0]["text"], "the opening statement names the place"
    assert [s["slot"] for s in statements] == list(range(1, slots + 1))
    assert statements[0]["category"] in ("time", "weather"), "slot 1 must be a right-now fact"
    cats = [s["category"] for s in statements]
    assert all(a != b for a, b in zip(cats, cats[1:], strict=False)), cats
    assert max(Counter(cats).values()) <= engine.PER_CATEGORY, cats
    assert any(s["fact_id"] in packs for s in statements), "at least one curated evergreen fact"
    assert all(s["text"] and "{" not in s["text"] for s in statements)


# --- adapters against recorded responses --------------------------------------------


def test_adapters_parse_recorded_responses() -> None:
    c = ctx()
    wx = adapters.weather(c)
    assert wx[0].template_key == "weather.now" and wx[0].now
    assert wx[0].values["c"] == 13 and wx[0].values["cond"] == "mostly clear skies"

    q = adapters.quakes(c)[0]
    assert q.values["mag"] == "6.1" and q.rarity == 2.0 and "Hasaki" in q.values["place"]

    h = adapters.holidays(c)[0]
    assert h.template_key == "holiday.soon" and h.values == {
        "name": "Labour Thanksgiving Day",
        "country": "Japan",
        "days": 16,
    }

    assert adapters.fx(c)[0].values["rate"] == "158"
    assert len(adapters.nearby(c)) == 3
    # None of the recorded anniversaries mention Japan or Tokyo, so none are used.
    assert adapters.onthisday(c) == []


def test_only_local_anniversaries_are_used() -> None:
    c = ctx()
    feed = {
        "selected": [
            {"year": 1990, "text": "Somewhere else entirely.", "pages": [{"titles": {"normalized": "Elsewhere"}}]},
            {
                "year": 1964,
                "text": "The first Shinkansen line opens in Japan.",
                "pages": [
                    {
                        "titles": {"normalized": "Tokaido Shinkansen"},
                        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Tokaido_Shinkansen"}},
                    }
                ],
            },
        ]
    }
    c.fetch = lambda _url: feed
    [fact] = adapters.onthisday(c)
    assert engine.render(fact, "h-1") in {
        "On this date in 1964: a moment in Japan's history involving Tokaido Shinkansen.",
        "1964, on this date, Japan made Wikipedia's On This Day, in an entry on Tokaido Shinkansen.",
    }


def test_every_template_renders_with_its_adapter_values() -> None:
    c = ctx()
    facts, report = engine.gather(c)
    assert all(r["ok"] for r in report.values()), report
    keys = {f.template_key for f in facts}
    assert {"time.now", "weather.now", "quake.strongest", "holiday.soon", "money.fx", "journey.distance"} <= keys
    for f in facts:
        for template in TEMPLATES[f.template_key]:
            assert "{" not in template.format(**f.values)


def test_country_and_journey_facts() -> None:
    c = ctx()
    texts = {engine.render(f, "x") for f in adapters.computed(c) + adapters.country(c)}
    assert any("9,3" in t and "Frankfurt" in t for t in texts), texts  # Frankfurt to Tokyo is about 9,300 km
    assert any("8 hours" in t for t in texts)  # CET to JST in November
    assert any("yen" in t for t in texts)


# --- failure handling ---------------------------------------------------------------


def test_failed_and_slow_adapters_never_stop_the_stay(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(_: Context) -> list[Fact]:
        time.sleep(5)
        return []

    def broken(_: Context) -> list[Fact]:
        raise ValueError("bad payload")

    monkeypatch.setattr(engine, "ADAPTER_TIMEOUT_S", 0.3)
    monkeypatch.setattr(adapters, "NETWORK", {"slow": slow, "broken": broken, "weather": adapters.weather})
    started = time.monotonic()
    facts, report = engine.gather(ctx())
    assert time.monotonic() - started < 2
    assert report["slow"] == {"ok": False, "error": "timeout"}
    assert report["broken"]["error"] == "ValueError"
    assert report["weather"]["ok"]
    assert any(f.category == "weather" for f in facts)
    assert any(f.category == "time" for f in facts)


# --- gate: slots, rules, memory, no network, reproducibility ----------------------------


@pytest.mark.parametrize(("stay", "slots"), [(180, 6), (209, 6), (240, 8), (299, 9), (300, 10)])
def test_slots_are_stay_over_thirty(stay: int, slots: int) -> None:
    assert engine.slots_for(stay) == slots


@pytest.mark.parametrize("region", regions.known())
def test_offline_stays_post_a_full_set(region: str) -> None:
    """Every network adapter off, the longest stay, six visits in a row: always a full set.

    The first three offline visits repeat nothing. After that the computed and curated pool
    is spent, so the oldest remembered visit is released first (D29); the previous visit
    is never repeated.
    """
    packs = {str(e["id"]) for e in regions.profile(region).get("evergreen_facts", [])}
    memory: dict[str, Any] = {}
    for visit in range(6):
        c = ctx(region, hop=f"h-{700 + visit:06d}", online=False, hop_count=700 + visit)
        c.now = NOW + timedelta(hours=visit)
        plan = engine.plan(c, slots=10, memory=memory, disabled={"all"})
        assert_rules(plan.statements, 10, packs=packs, city=regions.profile(region)["city"])
        ids = set(s["fact_id"] for s in plan.statements)
        visits = memory.get("visits", [])
        if visit < engine.MEMORY_VISITS:
            assert not ids & {i for v in visits for i in v}, f"visit {visit} repeated a fact"
        if visits:
            assert not ids & set(visits[-1]), f"visit {visit} repeated the previous visit"
        memory = remember(memory, sorted(ids))


def test_online_stays_follow_the_rules_and_use_live_sources() -> None:
    packs = {str(e["id"]) for e in regions.profile("ap-northeast-1")["evergreen_facts"]}
    memory: dict[str, Any] = {}
    sources: set[str] = set()
    for visit in range(4):
        c = ctx(hop=f"h-{800 + visit:06d}", hop_count=800 + visit)
        c.now = NOW + timedelta(hours=visit)
        plan = engine.plan(c, slots=10, memory=memory)
        assert_rules(plan.statements, 10, packs=packs, city="Tokyo")
        ids = [s["fact_id"] for s in plan.statements]
        assert not {i for v in memory.get("visits", []) for i in v} & set(ids)
        memory = remember(memory, ids)
        sources |= {s["source_name"] for s in plan.statements}
    assert {"MET Norway", "USGS"} <= sources


def test_selection_is_reproducible_for_a_hop_id() -> None:
    a = engine.plan(ctx(hop="h-000123"), slots=10)
    b = engine.plan(ctx(hop="h-000123"), slots=10)
    assert a.statements == b.statements
    others = {tuple(s["fact_id"] for s in engine.plan(ctx(hop=f"h-{n:06d}"), slots=10).statements) for n in range(8)}
    assert len(others) > 1, "different hops should not all say the same thing"


def test_a_stay_still_opens_with_a_right_now_fact_when_all_were_said_recently() -> None:
    c = ctx(online=False)
    first = engine.plan(c, slots=10, disabled={"all"})
    again = engine.plan(c, slots=10, memory=remember({}, [s["fact_id"] for s in first.statements]), disabled={"all"})
    assert again.statements and again.statements[0]["category"] == "time"


def test_a_big_quake_or_a_holiday_today_outranks_routine_facts() -> None:
    routine = Fact("country", "evergreen", "country.capital", {"country": "Japan", "capital": "Tokyo"}, "W")
    quake = Fact("quake", "today", "quake.strongest", {"mag": "6.4"}, "USGS", rarity=2.0)
    scored = engine.score([routine, quake], {}, "h-1")
    assert scored[0][1] is quake


# --- sun -------------------------------------------------------------------------------


def _minutes(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 60


def test_sun_times_match_published_tables() -> None:
    # London, 2024 June solstice: 04:43 and 21:21 BST.
    rise, set_ = sun.sun_times(date(2024, 6, 21), 51.51, -0.13) or (NOW, NOW)
    assert _minutes(rise, datetime(2024, 6, 21, 3, 43, tzinfo=UTC)) < 3
    assert _minutes(set_, datetime(2024, 6, 21, 20, 21, tzinfo=UTC)) < 3
    # Sydney, 2024 December solstice: 05:41 and 20:05 AEDT.
    rise, set_ = sun.sun_times(date(2024, 12, 21), -33.87, 151.21) or (NOW, NOW)
    assert _minutes(rise, datetime(2024, 12, 20, 18, 41, tzinfo=UTC)) < 3
    assert _minutes(set_, datetime(2024, 12, 21, 9, 5, tzinfo=UTC)) < 3
    # Solar noon in London on the solstice (12:02 UTC): 90 - 51.5 + 23.4 = about 62 degrees.
    assert abs(sun.elevation(datetime(2024, 6, 21, 12, 2, tzinfo=UTC), 51.51, -0.13) - 62) < 1


# --- tower memory and the real stay loop -------------------------------------------------


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    with mock_aws():
        yield boto3.resource("dynamodb", region_name="us-east-2").create_table(
            TableName="traveler-tower",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


def test_region_memory_keeps_three_visits_and_fifty_ids(table: Any) -> None:
    t = Tower(table)
    for v in range(6):
        t.remember_visit("eu-west-1", [f"f{v}-{i}" for i in range(10)])
    mem = t.region_memory("eu-west-1")
    assert [v[0] for v in mem["visits"]] == ["f3-0", "f4-0", "f5-0"]
    assert len(mem["recent"]) == 50 and mem["recent"][-1] == "f5-9"
    assert mem["visit_count"] == 6


def test_each_stay_posts_stay_over_thirty_statements(table: Any) -> None:
    pair = "us-east-2,us-east-1"
    cloud = FakeCloud(table)
    cloud.settings.update({"allowlist": pair, "hop_regions": pair, "stay_seconds": "95", "max_hops": "3"})
    cloud.wake("us-east-2")
    cloud.wait_idle()
    assert not cloud.errors, cloud.errors

    events = table.query(KeyConditionExpression=Key("pk").eq("EVENT"))["Items"]
    by_hop: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e["type"] == "STATEMENT":
            by_hop[e["hop_id"]].append(e)
    assert len(by_hop) == 4  # the wake stay plus three hops
    for stmts in by_hop.values():
        stmts.sort(key=lambda s: int(s["seq"]))
        assert [int(s["slot"]) for s in stmts] == [1, 2, 3]  # 95 // 30
        assert stmts[0]["category"] == "time"
    for region in ("us-east-2", "us-east-1"):
        assert Tower(table).region_memory(region)["visit_count"] == 2
    assert all(m["StatementsPlanned"] == 3 for m in cloud.metrics)

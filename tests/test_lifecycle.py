"""Phase 3 gate, rehearsed locally: hops, every rollback path, and the invariants.

The simulation runs the real lifecycle on a sped-up clock (see tests/sim.py), with the
edge propagation delay measured in AWS (D21).
"""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws
from sim import FakeCloud, FakeWorld

from traveler import lifecycle

PAIR = "us-east-2,us-east-1"


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCloud]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-2").create_table(
            TableName="traveler-tower",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        c = FakeCloud(table)
        c.settings.update({"allowlist": PAIR, "hop_regions": PAIR, "stay_seconds": "40"})
        yield c


def _hops(cloud: FakeCloud) -> dict[str, dict[str, Any]]:
    items = cloud.table.query(KeyConditionExpression=Key("pk").eq("HOP"))["Items"]
    return {i["sk"]: i for i in items}


def _events(cloud: FakeCloud, type_: str) -> list[dict[str, Any]]:
    items = cloud.table.query(KeyConditionExpression=Key("pk").eq("EVENT"))["Items"]
    return [e for e in items if e["type"] == type_]


def _assert_clean_end(cloud: FakeCloud) -> None:
    assert not cloud.errors, cloud.errors
    assert not cloud.violations, cloud.violations
    assert cloud.max_copies <= 2
    assert cloud.functions == {}, "the traveler should have rested and removed itself"
    assert not _events(cloud, "INVARIANT_VIOLATION")


def test_twenty_hops_between_two_regions(cloud: FakeCloud) -> None:
    cloud.settings["max_hops"] = "20"
    cloud.wake("us-east-2")
    cloud.wait_idle()

    hops = _hops(cloud)
    landed = [h for h in hops.values() if h["status"] == "landed"]
    assert len(landed) == 20
    assert not [h for h in hops.values() if h["status"] != "landed"]
    for h in landed:
        assert h["from"] != h["to"]
        # D21: the edge needs ~60 s, the old copy waits >= 90 s; the flight cap is 180 s.
        assert 90_000 <= int(h["flight_ms"]) < 180_000
        assert list(h["phases"]) and "ROUTE_CONFIRMED" in h["phases"] and "LANDED" in h["phases"]
    assert len(_events(cloud, "LANDED")) == 20
    assert len(_events(cloud, "OLD_EXITED")) == 20
    assert _events(cloud, "RESTING")[0]["reason"] == "reached max_hops=20"
    _assert_clean_end(cloud)


@pytest.mark.parametrize(
    ("failure", "phase"),
    [
        ("create", "FUNCTION_CREATED"),
        ("active", "FUNCTION_ACTIVE"),
        ("health", "HEALTH_OK"),
        ("edge_stuck", "ROUTE_CONFIRMED"),
        ("drop_arrival", "ARRIVED"),
        ("late_arrival", "ARRIVED"),
    ],
)
def test_each_failure_rolls_back_and_travel_resumes(cloud: FakeCloud, failure: str, phase: str) -> None:
    cloud.settings["max_hops"] = "2"
    cloud.fail[failure] = "h-000002"  # the first flight
    cloud.wake("us-east-2")
    cloud.wait_idle()

    hops = _hops(cloud)
    first = hops["h-000002"]
    assert first["status"] == "aborted"
    assert first["abort_phase"] == phase
    aborted = _events(cloud, "HOP_ABORTED")
    assert len(aborted) == 1 and aborted[0]["phase"] == phase
    # The traveler recovered and completed its two hops afterwards.
    assert sorted(k for k, h in hops.items() if h["status"] == "landed") == ["h-000003", "h-000004"]
    _assert_clean_end(cloud)


def test_late_arrival_stands_down_without_touching_the_old_copy(cloud: FakeCloud) -> None:
    # The suitcase reaches the new copy 70 s late, after the old copy has given up (60 s)
    # but before rollback deletes it (90 s). The new copy must lose the race and do nothing.
    cloud.settings["max_hops"] = "1"
    cloud.fail["late_arrival"] = "h-000002"
    cloud.wake("us-east-2")
    cloud.wait_idle()
    assert {"region": "us-east-1", "msg": "hop already aborted; standing down", "hop_id": "h-000002"}.items() <= next(
        e for e in cloud.logs if e["msg"] == "hop already aborted; standing down"
    ).items()
    assert "h-000002" not in {e["hop_id"] for e in _events(cloud, "ARRIVED")}
    assert _hops(cloud)["h-000002"]["status"] == "aborted"
    _assert_clean_end(cloud)


def test_kill_switch_rests_at_the_end_of_the_stay(cloud: FakeCloud) -> None:
    cloud.settings["kill"] = "true"
    cloud.wake("us-east-2")
    cloud.wait_idle()
    assert _hops(cloud) == {}
    assert _events(cloud, "RESTING")[0]["reason"] == "kill switch"
    assert cloud.routes[-1][1]["status"] == "resting"
    _assert_clean_end(cloud)


def test_external_sleep_stops_the_loop(cloud: FakeCloud) -> None:
    cloud.settings["stay_seconds"] = "120"
    cloud.wake("us-east-2")
    cloud.sleep(10)
    FakeWorld(cloud, "us-east-2").tower.set_state(status="resting")
    cloud.wait_idle()
    assert _hops(cloud) == {}
    assert any(entry["msg"].startswith("put to sleep") for entry in cloud.logs)
    assert not cloud.errors


class _Stub:
    def __init__(self, region: str, settings: dict[str, str]) -> None:
        self.region = region
        self._settings = settings

    def setting(self, name: str) -> str | None:
        return self._settings.get(name)


ALL = "a-1,b-1,c-1,d-1,e-1,f-1,g-1,h-1"


def test_destination_rule() -> None:
    world: Any = _Stub("a-1", {"allowlist": ALL})
    recent = ["a-1", "b-1", "c-1", "d-1", "e-1"]
    for n in range(50):
        dest = lifecycle.choose_destination(world, {"hop_id": f"h-{n:06d}", "recent_regions": recent})
        assert dest in {"f-1", "g-1", "h-1"}
    s = {"hop_id": "h-000007", "recent_regions": recent}
    assert lifecycle.choose_destination(world, s) == lifecycle.choose_destination(world, s)  # seeded


def test_destination_rule_relaxes_for_small_pools() -> None:
    world: Any = _Stub("us-east-2", {"hop_regions": PAIR})
    s = {"hop_id": "h-000002", "recent_regions": ["us-east-1", "us-east-2"], "avoid": ["us-east-1"]}
    assert lifecycle.choose_destination(world, s) == "us-east-1"

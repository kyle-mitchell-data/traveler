"""Watchdog decisions against a fake environment with controlled timestamps."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from traveler import watchdog
from traveler.tower import Tower

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC).timestamp()
ALL = "us-east-2,us-east-1,eu-west-1,ap-southeast-2"


def iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeOps:
    def __init__(self, tower: Tower, copies: dict[str, float], **settings: str) -> None:
        self.tower = tower
        self._copies = dict(copies)
        self.settings = {"allowlist": ALL, **settings}
        self.deleted: list[str] = []
        self.resurrected: list[str] = []
        self.waited = 0
        self.metrics: dict[str, float] = {}
        self.order: list[str] = []

    def now(self) -> float:
        return NOW

    def copies(self) -> dict[str, float]:
        return dict(self._copies)

    def delete(self, region: str) -> None:
        self.deleted.append(region)
        self.order.append(f"delete {region}")
        self._copies.pop(region, None)

    def setting(self, name: str) -> str | None:
        return self.settings.get(name)

    def resurrect(self, region: str, awake_since: str | None) -> str:
        self.resurrected.append(region)
        self.order.append(f"resurrect {region}")
        self._copies[region] = 0.0
        return region

    def wait_for_edge(self) -> None:
        self.waited += 1
        self.order.append("wait")

    def metric(self, **values: float) -> None:
        self.metrics = values

    def log(self, msg: str, **fields: Any) -> None:
        pass


@pytest.fixture
def tower(monkeypatch: pytest.MonkeyPatch) -> Iterator[Tower]:
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
        yield Tower(table)


def state(
    tower: Tower, *, status: str = "live", region: str = "us-east-2", heartbeat: float = 10, **extra: Any
) -> None:
    tower.table.put_item(
        Item={
            "pk": "STATE",
            "sk": "CURRENT",
            "status": status,
            "region": region,
            "hop_id": "h-000100",
            "heartbeat_at": iso(heartbeat),
            "updated_at": iso(extra.pop("updated", heartbeat)),
            "awake_since": iso(3600),
            **extra,
        }
    )


def test_healthy_traveler_is_left_alone(tower: Tower) -> None:
    state(tower)
    ops = FakeOps(tower, {"us-east-2": 3000})
    assert watchdog.tick(ops) == []
    assert ops.metrics == {"HeartbeatAgeSeconds": 10, "Copies": 1, "ConsecutiveAborts": 0, "InvariantViolations": 0}


def test_old_stray_copy_is_deleted_and_reported(tower: Tower) -> None:
    state(tower)
    ops = FakeOps(tower, {"us-east-2": 3000, "eu-west-1": 1200})
    assert watchdog.tick(ops) == ["deleted stray copy in eu-west-1"]
    assert ops.deleted == ["eu-west-1"]
    assert ops.metrics["InvariantViolations"] >= 1


def test_young_copy_mid_flight_is_kept(tower: Tower) -> None:
    state(tower, phase="ROUTE_FLIPPED")
    ops = FakeOps(tower, {"us-east-2": 3000, "eu-west-1": 30})
    assert watchdog.tick(ops) == []
    assert ops.metrics["Copies"] == 2


def test_resting_leftovers_wait_for_sleep_to_finish(tower: Tower) -> None:
    state(tower, status="resting", updated=60)  # make sleep still waiting on the edge
    ops = FakeOps(tower, {"us-east-2": 3000})
    assert watchdog.tick(ops) == []
    state(tower, status="resting", updated=900)
    assert watchdog.tick(ops) == ["deleted stray copy in us-east-2"]


def test_stale_heartbeat_resurrects_elsewhere_then_retires_the_dead_copy(tower: Tower) -> None:
    state(tower, heartbeat=700)
    ops = FakeOps(tower, {"us-east-2": 5000})
    actions = watchdog.tick(ops)
    assert ops.resurrected and ops.resurrected[0] != "us-east-2"
    # The old copy may still serve visitors: route first, let the edge move, then delete.
    assert ops.order == [f"resurrect {ops.resurrected[0]}", "wait", "delete us-east-2"]
    assert any(a.startswith("resurrected") for a in actions)
    assert tower.recent_events(5)[0]["type"] == "RESURRECTED"


def test_hand_deleted_traveler_is_resurrected_after_a_short_grace(tower: Tower) -> None:
    state(tower, heartbeat=60)  # just deleted: still inside the landing grace window
    ops = FakeOps(tower, {})
    assert watchdog.tick(ops) == []
    state(tower, heartbeat=150)
    ops = FakeOps(tower, {})
    assert watchdog.tick(ops)[0].startswith("resurrected")
    assert ops.waited == 0  # nothing old to retire


def test_kill_switch_blocks_resurrection(tower: Tower) -> None:
    state(tower, heartbeat=900)
    ops = FakeOps(tower, {}, kill="true")
    assert watchdog.tick(ops) == ["traveler looks dead but the kill switch is on; not resurrecting"]
    assert ops.resurrected == []


def test_resting_traveler_is_never_resurrected(tower: Tower) -> None:
    state(tower, status="resting", heartbeat=9000, updated=9000)
    ops = FakeOps(tower, {})
    assert watchdog.tick(ops) == []
    assert ops.metrics["HeartbeatAgeSeconds"] == 0


def test_more_than_two_copies_is_a_violation(tower: Tower) -> None:
    state(tower)
    ops = FakeOps(tower, {"us-east-2": 3000, "eu-west-1": 30, "us-east-1": 30})
    watchdog.tick(ops)
    assert ops.metrics["InvariantViolations"] >= 1


def test_aborts_are_reported_for_the_alarm(tower: Tower) -> None:
    state(tower, consecutive_aborts=3)
    ops = FakeOps(tower, {"us-east-2": 3000})
    watchdog.tick(ops)
    assert ops.metrics["ConsecutiveAborts"] == 3


def test_lock_prevents_overlapping_runs(tower: Tower) -> None:
    assert tower.try_lock("watchdog", ttl_s=280)
    assert not tower.try_lock("watchdog", ttl_s=280)
    tower.unlock("watchdog")
    assert tower.try_lock("watchdog", ttl_s=280)

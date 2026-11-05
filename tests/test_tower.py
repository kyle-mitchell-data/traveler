"""Tower writer (traveler.tower) and tower read API (tower/handler.py) against a moto table."""

import json
import time
from collections.abc import Iterator
from typing import Any

import boto3
import handler as tower_api  # tower/handler.py, on pythonpath via pyproject
import pytest
from moto import mock_aws

from traveler.tower import Tower


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    with mock_aws():
        tbl = boto3.resource("dynamodb", region_name="us-east-2").create_table(
            TableName="traveler-tower",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(tower_api, "_table", tbl)
        yield tbl


def _get(path: str, **query: str) -> dict[str, Any]:
    event = {"rawPath": path, "requestContext": {"http": {"method": "GET"}}, "queryStringParameters": query}
    return tower_api.handler(event, None)


def test_sequences_are_monotonic(table: Any) -> None:
    t = Tower(table)
    assert [t.event("STATEMENT")["seq"] for _ in range(3)] == [1, 2, 3]
    assert t.next_hop_id() == "h-000001"
    assert t.next_hop_id() == "h-000002"


def test_events_expire_after_seven_days(table: Any) -> None:
    before = int(time.time())
    e = Tower(table).event("WOKE", hop_id="h-000001", region="eu-west-1")
    assert before + 7 * 24 * 3600 <= e["expires_at"] <= int(time.time()) + 7 * 24 * 3600
    item = table.get_item(Key={"pk": "EVENT", "sk": f"{e['seq']:012d}"})["Item"]
    assert item["type"] == "WOKE" and item["region"] == "eu-west-1"


def test_feed_returns_public_state_and_last_20_events_oldest_first(table: Any) -> None:
    t = Tower(table)
    t.set_state(status="live", region="eu-west-1", hop_id="h-000003", function_url="secret.lambda-url")
    for i in range(25):
        t.event("STATEMENT", slot=i)

    out = _get("/api/tower/feed")
    body = json.loads(out["body"])
    assert out["statusCode"] == 200
    assert out["headers"]["cache-control"] == "public, max-age=1"
    assert body["state"]["status"] == "live"
    assert "function_url" not in body["state"]  # the live origin is never published
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == list(range(6, 26))
    assert all("expires_at" not in e and "pk" not in e for e in body["events"])


def test_feed_before_anything_happened(table: Any) -> None:
    body = json.loads(_get("/api/tower/feed")["body"])
    assert body == {"state": {"status": "unknown"}, "events": []}


def test_hops_newest_first_with_clamped_limit(table: Any) -> None:
    for n in range(1, 6):
        table.put_item(Item={"pk": "HOP", "sk": f"h-{n:06d}", "outcome": "ok", "flight_ms": 20000 + n})
    body = json.loads(_get("/api/tower/hops", limit="3")["body"])
    assert [h["hop_id"] for h in body["hops"]] == ["h-000005", "h-000004", "h-000003"]
    assert json.loads(_get("/api/tower/hops", limit="abc")["body"])["hops"][0]["hop_id"] == "h-000005"


def test_desired_release_round_trip(table: Any) -> None:
    t = Tower(table)
    assert t.desired_release() is None
    t.set_desired_release("r202611051200-abc1234")
    assert t.desired_release() == "r202611051200-abc1234"


def test_tower_api_is_read_only(table: Any) -> None:
    event = {"rawPath": "/api/tower/feed", "requestContext": {"http": {"method": "POST"}}}
    assert tower_api.handler(event, None)["statusCode"] == 405

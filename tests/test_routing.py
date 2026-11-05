from typing import Any

from traveler.routing import Router


class Conflict(Exception):
    pass


class FakeKvs:
    """Mimics the KeyValueStore API's optimistic concurrency on ETag."""

    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        ConflictException = Conflict

    def __init__(self) -> None:
        self.etag = "e0"
        self.data: dict[str, str] = {}
        self.calls: list[str] = []

    def describe_key_value_store(self, KvsARN: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append("describe")
        return {"ETag": self.etag}

    def update_keys(self, KvsARN: str, IfMatch: str, Puts: list[dict[str, str]]) -> dict[str, Any]:  # noqa: N803
        self.calls.append("update")
        if IfMatch != self.etag:
            raise Conflict()
        self.data.update({p["Key"]: p["Value"] for p in Puts})
        self.etag = f"e{len(self.calls)}"
        return {"ETag": self.etag}


def test_go_live_writes_all_routing_keys_at_once() -> None:
    kvs = FakeKvs()
    Router(kvs, "arn:kvs").go_live(origin="abc.lambda-url.eu-west-1.on.aws", region="eu-west-1", hop_id="h-000001")
    assert kvs.data == {
        "current_origin": "abc.lambda-url.eu-west-1.on.aws",
        "current_region": "eu-west-1",
        "hop_id": "h-000001",
        "status": "live",
    }


def test_second_flip_is_one_api_call() -> None:
    kvs = FakeKvs()
    r = Router(kvs, "arn:kvs")
    r.go_live(origin="a", region="eu-west-1", hop_id="h-1")
    kvs.calls.clear()
    r.go_live(origin="b", region="eu-west-2", hop_id="h-2")
    assert kvs.calls == ["update"]  # D5: cached ETag, no describe


def test_stale_etag_refreshes_once_and_succeeds() -> None:
    kvs = FakeKvs()
    r = Router(kvs, "arn:kvs")
    r.rest()
    kvs.etag = "changed-elsewhere"
    kvs.calls.clear()
    r.go_live(origin="b", region="eu-west-2", hop_id="h-2")
    assert kvs.calls == ["update", "describe", "update"]
    assert kvs.data["status"] == "live"

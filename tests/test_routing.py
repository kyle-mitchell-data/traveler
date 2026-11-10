from typing import Any

import pytest

from traveler.routing import Router


class ClientError(Exception):
    """Shaped like botocore's ClientError."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeKvs:
    """Mimics the KeyValueStore API's optimistic concurrency on ETag."""

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
            # What the real API returned in production for a stale ETag.
            raise ClientError("ValidationException", "Pre-Condition failed during update of Key-Value-Store")
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


def test_other_errors_are_not_retried() -> None:
    class Broken(FakeKvs):
        def update_keys(self, KvsARN: str, IfMatch: str, Puts: list[dict[str, str]]) -> dict[str, Any]:  # noqa: N803
            self.calls.append("update")
            raise ClientError("AccessDeniedException", "nope")

    kvs = Broken()
    with pytest.raises(ClientError) as err:
        Router(kvs, "arn:kvs").rest()
    assert err.value.response["Error"]["Code"] == "AccessDeniedException"
    assert kvs.calls == ["describe", "update"]


class MemoryEtag:
    def __init__(self, etag: str | None = None) -> None:
        self.etag = etag

    def get(self) -> str | None:
        return self.etag

    def put(self, etag: str) -> None:
        self.etag = etag


def test_cold_router_uses_the_stored_etag() -> None:
    kvs = FakeKvs()
    store = MemoryEtag()
    Router(kvs, "arn:kvs", etag_store=store).go_live(origin="a", region="eu-west-1", hop_id="h-1")
    assert store.etag == kvs.etag
    kvs.calls.clear()
    # A new container: nothing cached in memory, one API call per flip.
    Router(kvs, "arn:kvs", etag_store=store).go_live(origin="b", region="eu-west-2", hop_id="h-2")
    assert kvs.calls == ["update"]
    assert store.etag == kvs.etag


def test_stale_stored_etag_refreshes_and_is_replaced() -> None:
    kvs = FakeKvs()
    store = MemoryEtag("written-before-make-wake")
    Router(kvs, "arn:kvs", etag_store=store).rest()
    assert kvs.calls == ["update", "describe", "update"]
    assert store.etag == kvs.etag


def test_a_broken_store_never_blocks_a_flip() -> None:
    class Broken:
        def get(self) -> str | None:
            raise RuntimeError("tower down")

        def put(self, etag: str) -> None:
            raise RuntimeError("tower down")

    kvs = FakeKvs()
    Router(kvs, "arn:kvs", etag_store=Broken()).go_live(origin="a", region="eu-west-1", hop_id="h-1")
    assert kvs.calls == ["describe", "update"]
    assert kvs.data["status"] == "live"


def test_route_writer_keeps_the_etag_on_its_own_tower_item() -> None:
    from route_writer.handler import TowerEtag

    class Table:
        def __init__(self) -> None:
            self.items: dict[tuple[str, str], dict[str, str]] = {}

        def get_item(self, Key: dict[str, str]) -> dict[str, Any]:  # noqa: N803
            item = self.items.get((Key["pk"], Key["sk"]))
            return {"Item": item} if item else {}

        def put_item(self, Item: dict[str, str]) -> None:  # noqa: N803
            self.items[(Item["pk"], Item["sk"])] = Item

    table = Table()
    store = TowerEtag(table)
    assert store.get() is None
    store.put("e7")
    assert store.get() == "e7"
    # Only the ROUTE partition, which is all the writer's IAM policy allows.
    assert list(table.items) == [("ROUTE", "ETAG")]

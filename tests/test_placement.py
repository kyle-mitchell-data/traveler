import io
import json
from typing import Any

import pytest

from traveler import placement


class NotFound(Exception):
    pass


class FakeLambda:
    """Records the calls placement makes; can be told to fail at one of them."""

    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        ResourceNotFoundException = NotFound

    class meta:  # noqa: N801
        region_name = "eu-west-1"

    def __init__(self, fail_on: str | None = None, answer: dict[str, Any] | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer = answer or {"region": "eu-west-1", "release": "r1"}
        self.exists = False

    def _call(self, name: str, kw: dict[str, Any]) -> None:
        self.calls.append((name, kw))
        if name == self.fail_on:
            raise RuntimeError(f"{name} exploded")

    def create_function(self, **kw: Any) -> dict[str, Any]:
        self._call("create_function", kw)
        self.exists = True
        return {"FunctionArn": "arn:aws:lambda:eu-west-1:1:function:traveler"}

    def get_waiter(self, name: str) -> Any:
        outer = self

        class W:
            def wait(self, **kw: Any) -> None:
                outer._call("wait_active", kw)

        return W()

    def put_function_event_invoke_config(self, **kw: Any) -> None:
        self._call("put_function_event_invoke_config", kw)

    def put_function_recursion_config(self, **kw: Any) -> None:
        self._call("put_function_recursion_config", kw)

    def create_function_url_config(self, **kw: Any) -> dict[str, Any]:
        self._call("create_function_url_config", kw)
        return {"FunctionUrl": "https://abc123.lambda-url.eu-west-1.on.aws/"}

    def add_permission(self, **kw: Any) -> None:
        self._call("add_permission", kw)

    def invoke(self, **kw: Any) -> dict[str, Any]:
        self._call("invoke", kw)
        resp = {"statusCode": 200, "body": json.dumps(self.answer)}
        return {"Payload": io.BytesIO(json.dumps(resp).encode())}

    def delete_function(self, **kw: Any) -> None:
        self._call("delete_function", kw)
        if not self.exists:
            raise NotFound()
        self.exists = False

    def get_function_configuration(self, **kw: Any) -> dict[str, Any]:
        if not self.exists:
            raise NotFound()
        return {}


def _create(lam: FakeLambda) -> placement.Placed:
    return placement.create(
        lam, zip_bytes=b"PK", role_arn="arn:role/traveler", distribution_arn="arn:dist/X", env={"A": "1"}
    )


def test_create_configures_everything_the_design_needs() -> None:
    lam = FakeLambda()
    placed = _create(lam)
    assert placed.url_domain == "abc123.lambda-url.eu-west-1.on.aws"
    calls = dict((n, kw) for n, kw in lam.calls if n != "add_permission")

    fn = calls["create_function"]
    assert fn["FunctionName"] == "traveler"
    assert fn["Architectures"] == ["arm64"] and fn["MemorySize"] == 128 and fn["Timeout"] == 600
    # D4: no ghosts
    assert calls["put_function_event_invoke_config"] == {
        "FunctionName": "traveler",
        "MaximumRetryAttempts": 0,
        "MaximumEventAgeInSeconds": 60,
    }
    # D14: chain is intentionally recursive
    assert calls["put_function_recursion_config"]["RecursiveLoop"] == "Allow"
    # D17: IAM-auth URL, callable only by the distribution
    assert calls["create_function_url_config"]["AuthType"] == "AWS_IAM"
    perms = [kw for n, kw in lam.calls if n == "add_permission"]
    assert {p["Action"] for p in perms} == {"lambda:InvokeFunctionUrl", "lambda:InvokeFunction"}
    assert all(p["Principal"] == "cloudfront.amazonaws.com" and p["SourceArn"] == "arn:dist/X" for p in perms)


@pytest.mark.parametrize(
    ("fail_on", "phase"),
    [
        ("create_function", "FUNCTION_CREATED"),
        ("wait_active", "FUNCTION_ACTIVE"),
        ("create_function_url_config", "URL_READY"),
        ("add_permission", "URL_READY"),
    ],
)
def test_failures_name_the_flight_phase(fail_on: str, phase: str) -> None:
    with pytest.raises(placement.PlacementError) as err:
        _create(FakeLambda(fail_on=fail_on))
    assert err.value.phase == phase


def test_health_accepts_matching_region_and_release() -> None:
    assert placement.health(FakeLambda(), expect_release="r1")["region"] == "eu-west-1"


@pytest.mark.parametrize("answer", [{"region": "us-east-1", "release": "r1"}, {"region": "eu-west-1", "release": "r0"}])
def test_health_rejects_wrong_region_or_release(answer: dict[str, Any]) -> None:
    with pytest.raises(placement.PlacementError) as err:
        placement.health(FakeLambda(answer=answer), expect_release="r1")
    assert err.value.phase == "HEALTH_OK"


def test_remove_is_idempotent() -> None:
    lam = FakeLambda()
    _create(lam)
    assert placement.exists(lam)
    assert placement.remove(lam) is True
    assert placement.remove(lam) is False
    assert not placement.exists(lam)

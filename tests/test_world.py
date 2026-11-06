"""AwsWorld's failure-injection hook: real failures, exactly once."""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from traveler.world import AwsWorld


class Ctx:
    def get_remaining_time_in_millis(self) -> int:
        return 900_000


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[AwsWorld]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("TOWER_REGION", "us-east-2")
    monkeypatch.setenv("TOWER_TABLE", "traveler-tower")
    monkeypatch.setenv("TRAVELER_RELEASE", "r1")
    with mock_aws():
        ssm = boto3.client("ssm", region_name="us-east-2")
        for name, value in {
            "role_arn": "arn:aws:iam::1:role/traveler",
            "distribution_arn": "arn:aws:cloudfront::1:distribution/X",
            "tower_table": "traveler-tower",
            "site_url": "https://example.invalid",
        }.items():
            ssm.put_parameter(Name=f"/traveler/{name}", Value=value, Type="String")
        w = AwsWorld(Ctx())
        monkeypatch.setattr(w, "package", lambda release: b"PK")
        yield w


def _inject(phase: str) -> None:
    boto3.client("ssm", region_name="us-east-2").put_parameter(
        Name="/traveler/inject", Value=phase, Type="String", Overwrite=True
    )


def _pending() -> bool:
    names = [p["Name"] for p in boto3.client("ssm", region_name="us-east-2").describe_parameters()["Parameters"]]
    return "/traveler/inject" in names


def test_no_injection_by_default(world: AwsWorld) -> None:
    args: dict[str, Any] = world.placement_args(release="r1", hop_id="h-000002")
    assert args["role_arn"] == "arn:aws:iam::1:role/traveler"
    assert args["env"]["TRAVELER_RELEASE"] == "r1"


def test_bad_role_once_for_p2(world: AwsWorld) -> None:
    _inject("FUNCTION_CREATED")
    assert world.placement_args(release="r1", hop_id="h-000002")["role_arn"].endswith("/traveler-does-not-exist")
    assert not _pending()
    assert world.placement_args(release="r1", hop_id="h-000003")["role_arn"].endswith("/traveler")


def test_wrong_release_once_for_p5(world: AwsWorld) -> None:
    _inject("HEALTH_OK")
    assert world.placement_args(release="r1", hop_id="h-000002")["env"]["TRAVELER_RELEASE"] == "r1-wrong"
    assert world.placement_args(release="r1", hop_id="h-000003")["env"]["TRAVELER_RELEASE"] == "r1"


def test_stuck_edge_lasts_the_whole_flight(world: AwsWorld) -> None:
    _inject("ROUTE_CONFIRMED")
    assert [world.probe() for _ in range(3)] == [None, None, None]
    assert not _pending()


def test_lost_suitcase_only_affects_handoffs(world: AwsWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    class FakeLambda:
        def invoke(self, **kw: Any) -> None:
            sent.append(kw)

    monkeypatch.setattr("traveler.world._lambda", lambda region: FakeLambda())
    _inject("ARRIVED")
    world.invoke_async("us-east-1", {"mode": "residence", "from_region": None})  # a retry, not a handoff
    assert len(sent) == 1 and _pending()
    world.invoke_async("us-east-1", {"mode": "residence", "from_region": "us-east-2"})
    assert len(sent) == 1 and not _pending()
    world.invoke_async("us-east-1", {"mode": "residence", "from_region": "us-east-2"})
    assert len(sent) == 2

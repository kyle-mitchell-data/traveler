import json

import pytest

from traveler import handler, regions


def _req(path: str, method: str = "GET") -> dict:
    return {"rawPath": path, "requestContext": {"http": {"method": method}}}


@pytest.fixture(autouse=True)
def frankfurt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "REGION", "eu-central-1")
    monkeypatch.setenv("TRAVELER_HOP_ID", "h-000412")
    monkeypatch.setenv("TRAVELER_RELEASE", "r1-abc")
    monkeypatch.setenv("TRAVELER_ARRIVED_AT", "2026-11-02T14:03:00.000Z")


def test_whereami_reports_region_and_identity() -> None:
    out = handler.handler(_req("/api/traveler/whereami"), None)
    body = json.loads(out["body"])
    assert out["statusCode"] == 200
    assert body == {
        "region": "eu-central-1",
        "city": "Frankfurt",
        "near": "near Frankfurt, Germany",
        "hop_id": "h-000412",
        "release": "r1-abc",
        "arrived_at": "2026-11-02T14:03:00.000Z",
    }


def test_every_response_carries_region_header_and_no_store() -> None:
    for path in ("/api/traveler/whereami", "/api/traveler/ping", "/nope"):
        headers = handler.handler(_req(path), None)["headers"]
        assert headers["x-traveler-region"] == "eu-central-1"
        assert headers["cache-control"] == "no-store"


def test_ping_is_minimal() -> None:
    out = handler.handler(_req("/api/traveler/ping"), None)
    assert json.loads(out["body"]) == {"region": "eu-central-1"}


def test_unknown_path_and_method() -> None:
    assert handler.handler(_req("/api/traveler/nope"), None)["statusCode"] == 404
    assert handler.handler(_req("/api/traveler/ping", "POST"), None)["statusCode"] == 405


def test_unknown_event_is_rejected() -> None:
    assert "error" in handler.handler({"mode": "teleport"}, None)


def test_profiles_cover_the_allowlist() -> None:
    allowlist = [
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ca-central-1",
        "eu-central-1",
        "eu-north-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    ]
    assert regions.known() == allowlist
    for code in allowlist:
        p = regions.profile(code)
        assert p["code"] == code
        assert p["near"].startswith(("near ", "in "))
        assert -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180


def test_unknown_region_falls_back() -> None:
    assert regions.profile("xx-nowhere-1")["near"] == "in xx-nowhere-1"

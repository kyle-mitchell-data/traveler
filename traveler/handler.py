"""Lambda entry point.

HTTP requests arrive through the Function URL (payload format 2.0) and must stay
trivial: everything they return is already in memory, so a request never makes a
network call and finishes in a few milliseconds (decision D3).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from traveler import regions

REGION = os.environ.get("AWS_REGION", "local")


def _identity() -> dict[str, Any]:
    p = regions.profile(REGION)
    return {
        "region": REGION,
        "city": p["city"],
        "near": p["near"],
        "hop_id": os.environ.get("TRAVELER_HOP_ID", ""),
        "release": os.environ.get("TRAVELER_RELEASE", "dev"),
        "arrived_at": os.environ.get("TRAVELER_ARRIVED_AT", ""),
    }


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-traveler-region": REGION,
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def http(event: dict[str, Any]) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method not in ("GET", "HEAD"):
        return _response(405, {"error": "method not allowed"})
    if path.endswith("/ping"):
        # The server's clock, so the page's countdown is right even when the visitor's clock is not.
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return _response(200, {"region": REGION, "now": now})
    if path.endswith("/whereami"):
        return _response(200, _identity())
    return _response(404, {"error": "not found"})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if "rawPath" in event:
        return http(event)
    if event.get("mode") == "residence":
        # Imported here so HTTP requests never pay for boto3 setup.
        from traveler import lifecycle
        from traveler.world import AwsWorld

        return lifecycle.run(AwsWorld(context), event)
    return {"error": f"unknown event; mode={event.get('mode')!r}"}

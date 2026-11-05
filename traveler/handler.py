"""Lambda entry point.

HTTP requests arrive through the Function URL (payload format 2.0) and must stay
trivial: everything they return is already in memory, so a request never makes a
network call and finishes in a few milliseconds (decision D3).
"""

from __future__ import annotations

import json
import os
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
        return _response(200, {"region": REGION})
    if path.endswith("/whereami"):
        return _response(200, _identity())
    return _response(404, {"error": "not found"})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if "rawPath" in event:
        return http(event)
    # Residence mode (the suitcase from the previous copy) arrives in phase 3.
    return {"error": f"unknown event; mode={event.get('mode')!r}"}

"""Tower read API, behind CloudFront with a 1-second cache.

GET /api/tower/feed        state snapshot plus the last 20 events (decision D6)
GET /api/tower/hops?limit= recent hop summaries, newest first

Every viewer requests the same URLs, so one origin call per second per edge cache
serves everyone. This function only reads.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

FEED_EVENTS = 20
MAX_HOPS = 100

# Fields of the STATE item that are safe to publish. The live Function URL is not.
PUBLIC_STATE = (
    "status",
    "region",
    "hop_id",
    "arrived_at",
    "departure_at",
    "next_region",
    "phase",
    "release",
    "heartbeat_at",
    "hops_ok",
    "hops_aborted",
    "updated_at",
)

# Failure reasons are written for the operator (exception text, request IDs). The page
# explains failures from the phase alone, so only resting reasons, which are fixed
# phrases, are published.
PUBLIC_REASON_EVENTS = frozenset({"RESTING"})

_table: Any = None


def table() -> Any:
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TOWER_TABLE"])
    return _table


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(type(obj).__name__)


def _response(status: int, body: Any, max_age: int = 1) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": f"public, max-age={max_age}" if status == 200 else "no-store",
        },
        "body": json.dumps(body, default=_jsonable, separators=(",", ":")),
    }


def feed(tbl: Any) -> dict[str, Any]:
    state = tbl.get_item(Key={"pk": "STATE", "sk": "CURRENT"}).get("Item", {})
    events = tbl.query(KeyConditionExpression=Key("pk").eq("EVENT"), ScanIndexForward=False, Limit=FEED_EVENTS)["Items"]
    for e in events:
        e.pop("pk", None)
        e.pop("sk", None)
        e.pop("expires_at", None)
        if e.get("type") not in PUBLIC_REASON_EVENTS:
            e.pop("reason", None)
    return {
        "state": {k: state[k] for k in PUBLIC_STATE if k in state} or {"status": "unknown"},
        "events": list(reversed(events)),
    }


def hops(tbl: Any, limit: int) -> list[dict[str, Any]]:
    items = tbl.query(KeyConditionExpression=Key("pk").eq("HOP"), ScanIndexForward=False, Limit=limit)["Items"]
    for h in items:
        h.pop("pk", None)
        h.pop("abort_reason", None)
        h["hop_id"] = h.pop("sk")
    return list(items)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method not in ("GET", "HEAD"):
        return _response(405, {"error": "method not allowed"})
    if path.endswith("/feed"):
        return _response(200, feed(table()))
    if path.endswith("/hops"):
        raw = (event.get("queryStringParameters") or {}).get("limit", "50")
        limit = max(1, min(MAX_HOPS, int(raw))) if raw.isdigit() else 50
        return _response(200, {"hops": hops(table(), limit)}, max_age=5)
    return _response(404, {"error": "not found"})

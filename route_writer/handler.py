"""Route writer: the only place outside the operator CLI that writes the KeyValueStore.

The KeyValueStore API needs SigV4A signing, which needs awscrt, which the Lambda
Python runtime does not include (verified 2026-11-05). This function lives in the
home region with awscrt bundled, so the traveler package stays small (decision D16).

It accepts only the four routing keys, with sane values, so a confused or
compromised traveler cannot write anything else to the edge.
"""

from __future__ import annotations

import os
import re
from typing import Any

import boto3

from traveler.routing import Router

ALLOWED = {
    "current_origin": re.compile(r"^[a-z0-9]+\.lambda-url\.[a-z0-9-]+\.on\.aws$"),
    "current_region": re.compile(r"^[a-z]{2}-[a-z]+-\d$"),
    "hop_id": re.compile(r"^h-\d{6,}$"),
    "status": re.compile(r"^(live|resting)$"),
}

_router: Router | None = None


class TowerEtag:
    """The last KeyValueStore ETag, on its own tower item (pk ROUTE)."""

    KEY = {"pk": "ROUTE", "sk": "ETAG"}

    def __init__(self, table: Any) -> None:
        self.table = table

    def get(self) -> str | None:
        item = self.table.get_item(Key=self.KEY).get("Item")
        return str(item["etag"]) if item else None

    def put(self, etag: str) -> None:
        self.table.put_item(Item={**self.KEY, "etag": etag})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _router
    keys = event.get("keys")
    if not isinstance(keys, dict) or not keys:
        return {"ok": False, "error": "keys required"}
    for k, v in keys.items():
        pattern = ALLOWED.get(k)
        if pattern is None or not isinstance(v, str) or not pattern.match(v):
            return {"ok": False, "error": f"rejected {k!r}"}
    if _router is None:
        table = boto3.resource("dynamodb").Table(os.environ["TOWER_TABLE"])
        _router = Router(
            boto3.client("cloudfront-keyvaluestore", region_name="us-east-1"),
            os.environ["KVS_ARN"],
            etag_store=TowerEtag(table),
        )
    _router.set(**keys)
    return {"ok": True}

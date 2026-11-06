"""Writes to the control tower table in the home region.

Item layout (single-table, decision D19 for the HOP partition):
    STATE  / CURRENT      current state snapshot
    SEQ    / EVENT        atomic event sequence counter
    SEQ    / HOP          atomic hop counter
    EVENT  / 000000018233 one event, expires after 7 days
    HOP    / h-000412     one hop summary
    CONFIG / RELEASE      desired_release, set by CI or `make release`
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

EVENT_TTL_SECONDS = 7 * 24 * 3600


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Tower:
    def __init__(self, table: Any) -> None:
        self.table = table

    def _next(self, counter: str) -> int:
        out = self.table.update_item(
            Key={"pk": "SEQ", "sk": counter},
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(out["Attributes"]["n"])

    def next_hop_id(self) -> str:
        return f"h-{self._next('HOP'):06d}"

    def event(self, type_: str, *, hop_id: str = "", region: str = "", **payload: Any) -> dict[str, Any]:
        seq = self._next("EVENT")
        item = {
            "pk": "EVENT",
            "sk": f"{seq:012d}",
            "type": type_,
            "seq": seq,
            "hop_id": hop_id,
            "region": region,
            "ts": now_iso(),
            "expires_at": int(time.time()) + EVENT_TTL_SECONDS,
            **payload,
        }
        self.table.put_item(Item=item)
        return item

    def set_state(self, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        self.table.update_item(
            Key={"pk": "STATE", "sk": "CURRENT"},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def state(self) -> dict[str, Any]:
        item: dict[str, Any] = self.table.get_item(Key={"pk": "STATE", "sk": "CURRENT"}).get("Item", {})
        return item

    def desired_release(self) -> str | None:
        item = self.table.get_item(Key={"pk": "CONFIG", "sk": "RELEASE"}).get("Item")
        return str(item["desired_release"]) if item else None

    def set_desired_release(self, version: str) -> None:
        self.table.put_item(Item={"pk": "CONFIG", "sk": "RELEASE", "desired_release": version, "set_at": now_iso()})

    # --- hops -------------------------------------------------------------------
    # A hop is in_flight until exactly one of try_arrive (new copy) or try_abort
    # (old copy) wins a conditional write. That single write is what stops both
    # sides from acting on the same hop.

    def start_hop(self, hop_id: str, *, from_region: str, to_region: str, release: str) -> None:
        self.table.put_item(
            Item={
                "pk": "HOP",
                "sk": hop_id,
                "status": "in_flight",
                "from": from_region,
                "to": to_region,
                "release": release,
                "started_at": now_iso(),
                "phases": {},
            }
        )

    def mark_phase(self, hop_id: str, phase: str) -> None:
        self.table.update_item(
            Key={"pk": "HOP", "sk": hop_id},
            UpdateExpression="SET phases.#p = :ts",
            ExpressionAttributeNames={"#p": phase},
            ExpressionAttributeValues={":ts": now_iso()},
        )

    def _transition(self, hop_id: str, to_status: str, **fields: Any) -> bool:
        fields["status"] = to_status
        names = {f"#{k}": k for k in fields}
        values: dict[str, Any] = {f":{k}": v for k, v in fields.items()}
        values[":in_flight"] = "in_flight"
        try:
            self.table.update_item(
                Key={"pk": "HOP", "sk": hop_id},
                UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                ConditionExpression="#status = :in_flight",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def try_arrive(self, hop_id: str) -> bool:
        return self._transition(hop_id, "arrived", arrived_at=now_iso())

    def try_abort(self, hop_id: str, *, phase: str, reason: str) -> bool:
        return self._transition(hop_id, "aborted", aborted_at=now_iso(), abort_phase=phase, abort_reason=reason)

    def mark_old_exited(self, hop_id: str) -> None:
        self.table.update_item(
            Key={"pk": "HOP", "sk": hop_id},
            UpdateExpression="SET old_exited_at = :ts",
            ExpressionAttributeValues={":ts": now_iso()},
        )

    def land(self, hop_id: str, *, flight_ms: int) -> None:
        self.table.update_item(
            Key={"pk": "HOP", "sk": hop_id},
            UpdateExpression="SET #s = :landed, flight_ms = :ms, landed_at = :ts",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":landed": "landed", ":ms": flight_ms, ":ts": now_iso()},
        )

    def hop(self, hop_id: str) -> dict[str, Any]:
        item: dict[str, Any] = self.table.get_item(Key={"pk": "HOP", "sk": hop_id}).get("Item", {})
        return item

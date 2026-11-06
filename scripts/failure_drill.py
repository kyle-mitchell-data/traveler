"""Phase 3 gate drill: force a failure at each phase and check the rollback.

    python -m scripts.failure_drill [PHASE ...]

Assumes the traveler is awake with hop_regions set. For each phase it sets the
one-shot /traveler/inject switch (D26), waits for the HOP_ABORTED event, and then
checks that exactly one traveler copy exists and that the switch was consumed.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

HOME = "us-east-2"
PHASES = ("FUNCTION_CREATED", "HEALTH_OK", "ROUTE_CONFIRMED", "ARRIVED")
TIMEOUT_S = 15 * 60


def main() -> None:
    phases = tuple(sys.argv[1:]) or PHASES
    unknown = set(phases) - set(PHASES)
    if unknown:
        sys.exit(f"unknown phases {sorted(unknown)}; choose from {PHASES}")
    ssm = boto3.client("ssm", region_name=HOME)
    table = boto3.resource("dynamodb", region_name=HOME).Table(
        ssm.get_parameter(Name="/traveler/tower_table")["Parameter"]["Value"]
    )
    regions = ssm.get_parameter(Name="/traveler/hop_regions")["Parameter"]["Value"].split(",")
    lambdas = {r: boto3.client("lambda", region_name=r) for r in regions}

    def latest_seq() -> int:
        items = table.query(KeyConditionExpression=Key("pk").eq("EVENT"), ScanIndexForward=False, Limit=1)["Items"]
        return int(str(items[0]["seq"])) if items else 0

    def events_after(seq: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = table.query(
            KeyConditionExpression=Key("pk").eq("EVENT") & Key("sk").gt(f"{seq:012d}")
        )["Items"]
        return items

    def copies() -> list[str]:
        here = []
        for r, lam in lambdas.items():
            try:
                lam.get_function_configuration(FunctionName="traveler")
                here.append(r)
            except lam.exceptions.ResourceNotFoundException:
                pass
        return here

    results: list[tuple[str, bool, str]] = []
    for phase in phases:
        start = latest_seq()
        ssm.put_parameter(Name="/traveler/inject", Value=phase, Type="String", Overwrite=True)
        print(f"injected {phase}; waiting for the next hop to fail…", flush=True)
        deadline = time.time() + TIMEOUT_S
        aborted: dict[str, Any] | None = None
        max_seen = 0
        while time.time() < deadline and aborted is None:
            max_seen = max(max_seen, len(copies()))
            for e in events_after(start):
                if e["type"] == "HOP_ABORTED":
                    aborted = e
            time.sleep(5)
        if aborted is None:
            results.append((phase, False, "no HOP_ABORTED within 15 minutes"))
            break
        here = copies()
        consumed = "/traveler/inject" not in [p["Name"] for p in ssm.describe_parameters()["Parameters"]]
        ok = aborted["phase"] == phase and len(here) == 1 and consumed and max_seen <= 2
        detail = (
            f"hop {aborted['hop_id']} aborted at {aborted['phase']} ({str(aborted.get('reason', ''))[:70]}); "
            f"copies after={here}, max during={max_seen}, switch consumed={consumed}"
        )
        results.append((phase, ok, detail))
        print(("PASS " if ok else "FAIL ") + f"{phase}: {detail}", flush=True)

    passed = all(ok for _, ok, _ in results) and len(results) == len(phases)
    print("\nforced-failure drill:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

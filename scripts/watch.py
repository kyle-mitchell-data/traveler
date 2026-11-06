"""Watch the traveler like a visitor would, and narrate the tower's events.

    python -m scripts.watch [--minutes 10] [--until-rest]

Probes the public whereami once a second and prints every new tower event. At the
end, prints how many probes returned each status and region. This is the synthetic
prober for goal G3 (zero failed visitor requests across hops).
"""

from __future__ import annotations

import argparse
import collections
import json
import time
import urllib.error
import urllib.request
from typing import Any

import boto3

HOME = "us-east-2"


def probe(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url + "/api/traveler/whereami", headers={"cache-control": "no-cache"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, resp.headers.get("x-traveler-region", "?")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:  # noqa: BLE001
        return 0, "timeout"


def describe(e: dict[str, Any]) -> str:
    bits = [str(e.get("phase", "")), str(e.get("region", ""))]
    if e.get("destination"):
        bits.append(f"-> {e['destination']}")
    for k in ("flight_ms", "elapsed_ms", "stay_s", "reason", "text", "release"):
        if e.get(k) not in (None, ""):
            bits.append(f"{k}={e[k]}")
    return " ".join(b for b in bits if b)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--until-rest", action="store_true", help="stop once a RESTING event appears")
    parser.add_argument("--copies", default="", help="comma-separated regions to count traveler copies in")
    args = parser.parse_args()

    ssm = boto3.client("ssm", region_name=HOME)
    site = ssm.get_parameter(Name="/traveler/site_url")["Parameter"]["Value"]
    table = boto3.resource("dynamodb", region_name=HOME).Table(
        ssm.get_parameter(Name="/traveler/tower_table")["Parameter"]["Value"]
    )
    from boto3.dynamodb.conditions import Key

    def events_after(seq: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = table.query(
            KeyConditionExpression=Key("pk").eq("EVENT") & Key("sk").gt(f"{seq:012d}"), ScanIndexForward=True
        )["Items"]
        return items

    latest = table.query(KeyConditionExpression=Key("pk").eq("EVENT"), ScanIndexForward=False, Limit=1)["Items"]
    seq = int(str(latest[0]["seq"])) if latest else 0
    counts: collections.Counter[str] = collections.Counter()
    end = time.time() + args.minutes * 60
    next_events = 0.0
    next_copies = 0.0
    regions = [r for r in args.copies.split(",") if r]
    lambdas = {r: boto3.client("lambda", region_name=r) for r in regions}
    max_copies = 0

    def count_copies() -> list[str]:
        here = []
        for r, lam in lambdas.items():
            try:
                lam.get_function_configuration(FunctionName="traveler")
                here.append(r)
            except lam.exceptions.ResourceNotFoundException:
                pass
        return here

    while time.time() < end:
        if regions and time.time() >= next_copies:
            next_copies = time.time() + 5
            here = count_copies()
            max_copies = max(max_copies, len(here))
            if len(here) > 2:
                print(f"  INVARIANT: {len(here)} copies {here}", flush=True)
        code, region = probe(site)
        counts[f"{code} {region}".strip()] += 1
        if code != 200:
            print(f"  probe {time.strftime('%H:%M:%S')} -> {code} {region}", flush=True)
        if time.time() >= next_events:
            next_events = time.time() + 3
            for e in events_after(seq):
                seq = int(e["seq"])
                print(f"{str(e['ts'])[11:19]} {str(e['type']):<19} {describe(e)}", flush=True)
                if args.until_rest and e["type"] == "RESTING":
                    end = 0
        time.sleep(1)
    print("probe results:", json.dumps(dict(counts)))
    if regions:
        print("max copies seen:", max_copies)


if __name__ == "__main__":
    main()

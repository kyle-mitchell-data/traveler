"""Watchdog Lambda: AWS adapter for traveler.watchdog.tick, run every 5 minutes."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import cache
from typing import Any

import boto3

from traveler import placement, wake, watchdog
from traveler.routing import EDGE_PROPAGATION_S
from traveler.tower import Tower

HOME = os.environ.get("TOWER_REGION", "us-east-2")


@cache
def _lambda(region: str) -> Any:
    return boto3.client("lambda", region_name=region)


class AwsOps:
    def __init__(self) -> None:
        self._ssm = boto3.client("ssm", region_name=HOME)
        pages = self._ssm.get_paginator("get_parameters_by_path").paginate(Path="/traveler/")
        self.cfg = {p["Name"].removeprefix("/traveler/"): p["Value"] for page in pages for p in page["Parameters"]}
        self.tower = Tower(boto3.resource("dynamodb", region_name=HOME).Table(self.cfg["tower_table"]))

    def now(self) -> float:
        return time.time()

    def setting(self, name: str) -> str | None:
        return self.cfg.get(name)

    def copies(self) -> dict[str, float]:
        def check(region: str) -> tuple[str, float] | None:
            try:
                fn = _lambda(region).get_function_configuration(FunctionName=placement.FUNCTION_NAME)
            except _lambda(region).exceptions.ResourceNotFoundException:
                return None
            created = datetime.fromisoformat(fn["LastModified"].replace("+0000", "+00:00")).timestamp()
            return region, time.time() - created

        regions = self.cfg["allowlist"].split(",")
        with ThreadPoolExecutor(max_workers=len(regions)) as pool:
            return dict(r for r in pool.map(check, regions) if r)

    def delete(self, region: str) -> None:
        placement.remove(_lambda(region))

    def resurrect(self, region: str, awake_since: str | None) -> str:
        release = self.tower.desired_release() or ""
        zip_bytes = (
            boto3.client("s3", region_name=HOME)
            .get_object(Bucket=self.cfg["release_bucket"], Key=f"releases/{release}.zip")["Body"]
            .read()
        )
        wake.place_and_start(
            tower=self.tower,
            lambda_for=_lambda,
            set_route=self._set_route,
            zip_bytes=zip_bytes,
            release=release,
            region=region,
            role_arn=self.cfg["role_arn"],
            distribution_arn=self.cfg["distribution_arn"],
            tower_table=self.cfg["tower_table"],
            home=HOME,
            event_type="WOKE",
            awake_since=awake_since,
        )
        return region

    def _set_route(self, **keys: str) -> None:
        out = _lambda(HOME).invoke(FunctionName=self.cfg["route_writer"], Payload=json.dumps({"keys": keys}).encode())
        resp = json.loads(out["Payload"].read())
        if out.get("FunctionError") or not resp.get("ok"):
            raise RuntimeError(f"route writer refused: {resp.get('errorMessage') or resp}"[:300])

    def wait_for_edge(self) -> None:
        time.sleep(EDGE_PROPAGATION_S)

    def metric(self, **values: float) -> None:
        # CloudWatch embedded metric format: one log line, no dimensions (D18).
        print(
            json.dumps(
                {
                    "_aws": {
                        "Timestamp": int(time.time() * 1000),
                        "CloudWatchMetrics": [
                            {"Namespace": "Traveler", "Dimensions": [[]], "Metrics": [{"Name": k} for k in values]}
                        ],
                    },
                    **values,
                }
            )
        )

    def log(self, msg: str, **fields: Any) -> None:
        print(json.dumps({"msg": msg, **fields}, default=str))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ops = AwsOps()
    if not ops.tower.try_lock("watchdog", ttl_s=280):
        return {"skipped": "another watchdog run holds the lock"}
    try:
        actions = watchdog.tick(ops)
    finally:
        ops.tower.unlock("watchdog")
    ops.log("watchdog tick", actions=actions)
    return {"actions": actions}

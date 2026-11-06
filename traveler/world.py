"""The real world: AWS clients behind the lifecycle's World interface."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from functools import cache
from typing import Any

import boto3

from traveler import placement
from traveler.tower import Tower, now_iso

HTTP_TIMEOUT_S = 3


class AwsWorld:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.region = os.environ["AWS_REGION"]
        self.home = os.environ["TOWER_REGION"]
        self.release = os.environ.get("TRAVELER_RELEASE", "")
        self.hop_id = os.environ.get("TRAVELER_HOP_ID", "")
        self.tower = Tower(boto3.resource("dynamodb", region_name=self.home).Table(os.environ["TOWER_TABLE"]))
        self._ssm = boto3.client("ssm", region_name=self.home)
        self._cfg = self._load()

    # --- config -------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        pages = self._ssm.get_paginator("get_parameters_by_path").paginate(Path="/traveler/")
        return {p["Name"].removeprefix("/traveler/"): p["Value"] for page in pages for p in page["Parameters"]}

    def setting(self, name: str) -> str | None:
        # Switches can change mid-stay; read them fresh (SSM standard reads are free).
        if name in ("kill", "max_hops", "stay_seconds", "hop_regions", "pin_awake"):
            try:
                value: str = self._ssm.get_parameter(Name=f"/traveler/{name}")["Parameter"]["Value"]
                return value
            except self._ssm.exceptions.ParameterNotFound:
                return None
        return self._cfg.get(name)

    # --- time ---------------------------------------------------------------

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def remaining_s(self) -> float:
        return float(self.context.get_remaining_time_in_millis()) / 1000

    # --- aws ----------------------------------------------------------------

    def lambda_client(self, region: str) -> Any:
        return _lambda(region)

    def set_route(self, **keys: str) -> None:
        out = _lambda(self.home).invoke(
            FunctionName=self._cfg["route_writer"], Payload=json.dumps({"keys": keys}).encode()
        )
        resp = json.loads(out["Payload"].read())
        if out.get("FunctionError") or not resp.get("ok"):
            raise RuntimeError(f"route writer refused: {resp}")

    def probe(self) -> str | None:
        """Ask the public URL where the traveler is, as a visitor would."""
        req = urllib.request.Request(  # noqa: S310 - our own https URL from config
            self._cfg["site_url"] + "/api/traveler/whereami", headers={"cache-control": "no-cache"}
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
                region: str | None = resp.headers.get("x-traveler-region")
                return region
        except Exception:  # noqa: BLE001 - a failed probe is just "not yet"
            return None

    def package(self, release: str) -> bytes:
        if release == self.release:
            return placement.own_package(_lambda(self.region))
        obj = boto3.client("s3", region_name=self.home).get_object(
            Bucket=self._cfg["release_bucket"], Key=f"releases/{release}.zip"
        )
        data: bytes = obj["Body"].read()
        return data

    def invoke_async(self, region: str, payload: dict[str, Any]) -> None:
        _lambda(region).invoke(
            FunctionName=placement.FUNCTION_NAME, InvocationType="Event", Payload=json.dumps(payload).encode()
        )

    def placement_args(self, *, release: str, hop_id: str) -> dict[str, Any]:
        return {
            "zip_bytes": self.package(release),
            "role_arn": self._cfg["role_arn"],
            "distribution_arn": self._cfg["distribution_arn"],
            "env": {
                "TOWER_TABLE": self._cfg["tower_table"],
                "TOWER_REGION": self.home,
                "TRAVELER_RELEASE": release,
                "TRAVELER_HOP_ID": hop_id,
                "TRAVELER_ARRIVED_AT": now_iso(),
            },
        }

    def log(self, msg: str, **fields: Any) -> None:
        # JSON log format is on for the function, so one JSON object per line.
        print(json.dumps({"msg": msg, "region": self.region, "release": self.release, **fields}, default=str))


@cache
def _lambda(region: str) -> Any:
    return boto3.client("lambda", region_name=region)

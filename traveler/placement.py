"""Create, verify and retire the traveler function in a region.

Used by `make wake` today and by the traveler itself for handoff (P2-P5) in phase 3.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

FUNCTION_NAME = "traveler"
RUNTIME = "python3.13"
HANDLER = "traveler.handler.handler"
MEMORY_MB = 128
TIMEOUT_S = 600  # 300 s stay + packing + 120 s flight cap


class PlacementError(RuntimeError):
    def __init__(self, phase: str, reason: str) -> None:
        super().__init__(f"{phase}: {reason}")
        self.phase = phase
        self.reason = reason


@dataclass(frozen=True)
class Placed:
    region: str
    arn: str
    url_domain: str


def own_package(lambda_client: Any, function_name: str = FUNCTION_NAME) -> bytes:
    """Download a function's deployment package (the presigned link lasts 10 minutes)."""
    location = lambda_client.get_function(FunctionName=function_name)["Code"]["Location"]
    with urllib.request.urlopen(location, timeout=20) as resp:  # noqa: S310 - AWS presigned URL
        data: bytes = resp.read()
    return data


def exists(lambda_client: Any) -> bool:
    try:
        lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)
        return True
    except lambda_client.exceptions.ResourceNotFoundException:
        return False


def create(
    lambda_client: Any,
    *,
    zip_bytes: bytes,
    role_arn: str,
    distribution_arn: str,
    env: dict[str, str],
) -> Placed:
    """P2-P4: create the function, wait for Active, configure it and open its URL."""
    region = lambda_client.meta.region_name
    try:
        fn = lambda_client.create_function(
            FunctionName=FUNCTION_NAME,
            Runtime=RUNTIME,
            Architectures=["arm64"],
            Role=role_arn,
            Handler=HANDLER,
            Code={"ZipFile": zip_bytes},
            MemorySize=MEMORY_MB,
            Timeout=TIMEOUT_S,
            Environment={"Variables": env},
            LoggingConfig={"LogFormat": "JSON"},
            Tags={"project": "traveler"},
        )
    except Exception as exc:  # noqa: BLE001 - every failure here is a P2 abort
        raise PlacementError("FUNCTION_CREATED", str(exc)) from exc

    try:
        lambda_client.get_waiter("function_active_v2").wait(
            FunctionName=FUNCTION_NAME, WaiterConfig={"Delay": 1, "MaxAttempts": 60}
        )
    except Exception as exc:  # noqa: BLE001
        raise PlacementError("FUNCTION_ACTIVE", str(exc)) from exc

    try:
        # D4: a late or retried arrival must never wake a ghost.
        lambda_client.put_function_event_invoke_config(
            FunctionName=FUNCTION_NAME, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=60
        )
        # D14: the hop chain is intentionally recursive.
        lambda_client.put_function_recursion_config(FunctionName=FUNCTION_NAME, RecursiveLoop="Allow")
        url = lambda_client.create_function_url_config(FunctionName=FUNCTION_NAME, AuthType="AWS_IAM")
        # D17: only this distribution may call the copy, via Origin Access Control.
        lambda_client.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId="cloudfront-url",
            Action="lambda:InvokeFunctionUrl",
            Principal="cloudfront.amazonaws.com",
            SourceArn=distribution_arn,
            FunctionUrlAuthType="AWS_IAM",
        )
        lambda_client.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId="cloudfront-invoke",
            Action="lambda:InvokeFunction",
            Principal="cloudfront.amazonaws.com",
            SourceArn=distribution_arn,
        )
    except Exception as exc:  # noqa: BLE001
        raise PlacementError("URL_READY", str(exc)) from exc

    domain = url["FunctionUrl"].removeprefix("https://").rstrip("/")
    return Placed(region=region, arn=fn["FunctionArn"], url_domain=domain)


def health(lambda_client: Any, *, expect_release: str) -> dict[str, Any]:
    """P5: call the new copy directly and check it knows where and what it is."""
    event = {"rawPath": "/api/traveler/whereami", "requestContext": {"http": {"method": "GET"}}}
    region = lambda_client.meta.region_name
    try:
        out = lambda_client.invoke(FunctionName=FUNCTION_NAME, Payload=json.dumps(event).encode())
        resp = json.loads(out["Payload"].read())
        body: dict[str, Any] = json.loads(resp["body"])
    except Exception as exc:  # noqa: BLE001
        raise PlacementError("HEALTH_OK", f"no valid answer: {exc}") from exc
    if out.get("FunctionError") or resp.get("statusCode") != 200:
        raise PlacementError("HEALTH_OK", f"bad answer: {resp}")
    if body.get("region") != region or body.get("release") != expect_release:
        raise PlacementError("HEALTH_OK", f"expected {region}/{expect_release}, got {body}")
    return body


def remove(lambda_client: Any) -> bool:
    """Best-effort delete. Returns True if something was deleted."""
    try:
        lambda_client.delete_function(FunctionName=FUNCTION_NAME)
        return True
    except lambda_client.exceptions.ResourceNotFoundException:
        return False

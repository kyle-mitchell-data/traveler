"""Operator commands: release, wake, sleep, status.

    python -m scripts.ops release          build the traveler zip, upload, set desired_release
    python -m scripts.ops wake [--region]  place the traveler from the latest release and route to it
    python -m scripts.ops sleep            route to "resting" and remove every traveler copy
    python -m scripts.ops status           tower state plus a live count of copies

Settings come from SSM under /traveler/ (written by Terraform). Status reads the tower,
never the key-value store (decision D5).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from traveler import placement
from traveler.routing import EDGE_PROPAGATION_S, Router
from traveler.tower import Tower, now_iso

ROOT = Path(__file__).resolve().parent.parent
HOME = "us-east-2"


# --- settings ---------------------------------------------------------------


def settings(session: boto3.Session) -> dict[str, Any]:
    ssm = session.client("ssm", region_name=HOME)
    pages = ssm.get_paginator("get_parameters_by_path").paginate(Path="/traveler/")
    params = [p for page in pages for p in page["Parameters"]]
    out: dict[str, Any] = {p["Name"].removeprefix("/traveler/"): p["Value"] for p in params}
    out["allowlist"] = out["allowlist"].split(",")
    missing = {"kvs_arn", "distribution_arn", "role_arn", "release_bucket", "tower_table"} - out.keys()
    if missing:
        sys.exit(f"missing SSM settings {sorted(missing)}; run `make apply` first")
    return out


def tower(session: boto3.Session, cfg: dict[str, Any]) -> Tower:
    return Tower(session.resource("dynamodb", region_name=HOME).Table(cfg["tower_table"]))


def copies(session: boto3.Session, regions: list[str]) -> list[str]:
    """Regions that currently hold a traveler function, checked in parallel."""

    def check(r: str) -> str | None:
        return r if placement.exists(session.client("lambda", region_name=r)) else None

    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        return [r for r in pool.map(check, regions) if r]


# --- release ----------------------------------------------------------------


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted((ROOT / "traveler").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py", ".json"):
                info = zipfile.ZipInfo(str(path.relative_to(ROOT)).replace("\\", "/"), (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)  # noqa: S603, S607 - fixed git args
    return out.stdout.strip()


def version() -> str:
    sha = _git("rev-parse", "--short", "HEAD")
    dirty = _git("status", "--porcelain", "traveler")
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    return f"r{stamp}-{sha or 'nogit'}{'-dirty' if dirty else ''}"


def cmd_release(session: boto3.Session, cfg: dict[str, Any], _: argparse.Namespace) -> None:
    data = build_zip()
    ver = version()
    session.client("s3", region_name=HOME).put_object(
        Bucket=cfg["release_bucket"], Key=f"releases/{ver}.zip", Body=data
    )
    tower(session, cfg).set_desired_release(ver)
    print(f"released {ver} ({len(data) / 1024:.0f} KB); the traveler adopts it at its next hop")


# --- wake / sleep -----------------------------------------------------------


def cmd_wake(session: boto3.Session, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    allow: list[str] = cfg["allowlist"]
    present = copies(session, allow)
    if present:
        sys.exit(f"already awake in {', '.join(present)}; run `make sleep` first")

    t = tower(session, cfg)
    release = t.desired_release()
    if not release:
        sys.exit("no release published; run `make release` first")
    region = args.region or random.choice(allow)  # noqa: S311 - not security sensitive
    if region not in allow:
        sys.exit(f"{region} is not in the allowlist")

    zip_bytes = (
        session.client("s3", region_name=HOME)
        .get_object(Bucket=cfg["release_bucket"], Key=f"releases/{release}.zip")["Body"]
        .read()
    )
    hop_id = t.next_hop_id()
    arrived = now_iso()
    lam = session.client("lambda", region_name=region)
    print(f"waking {release} in {region} as {hop_id}…")
    try:
        placed = placement.create(
            lam,
            zip_bytes=zip_bytes,
            role_arn=cfg["role_arn"],
            distribution_arn=cfg["distribution_arn"],
            env={
                "TOWER_TABLE": cfg["tower_table"],
                "TOWER_REGION": HOME,
                "TRAVELER_RELEASE": release,
                "TRAVELER_HOP_ID": hop_id,
                "TRAVELER_ARRIVED_AT": arrived,
            },
        )
        placement.health(lam, expect_release=release)
    except placement.PlacementError as exc:
        placement.remove(lam)
        t.event("HOP_ABORTED", hop_id=hop_id, region=region, phase=exc.phase, reason=exc.reason)
        sys.exit(f"wake failed at {exc.phase}: {exc.reason}")

    Router(session.client("cloudfront-keyvaluestore", region_name="us-east-1"), cfg["kvs_arn"]).go_live(
        origin=placed.url_domain, region=region, hop_id=hop_id
    )
    t.set_state(
        status="live",
        region=region,
        hop_id=hop_id,
        arrived_at=arrived,
        release=release,
        function_url=placed.url_domain,
        phase="RESIDENT",
        heartbeat_at=arrived,
    )
    t.event("WOKE", hop_id=hop_id, region=region, release=release)
    prior = t.state()
    # Start the residence loop; from here the traveler moves on its own.
    lam.invoke(
        FunctionName=placement.FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "mode": "residence",
                "journey_id": f"j-{arrived[:10]}",
                "hop_id": hop_id,
                "hop_count": int(prior.get("hop_count", 0)),
                "recent_regions": [region],
                "release": release,
                "stats": {
                    "hops_ok": int(prior.get("hops_ok", 0)),
                    "hops_aborted": int(prior.get("hops_aborted", 0)),
                },
            }
        ).encode(),
    )
    print(f"live in {region} ({placed.url_domain}); residence started")


def cmd_sleep(session: boto3.Session, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    # Route away first, then keep the copy alive until every edge has stopped sending
    # it traffic (D21). Deleting sooner gives stale edges about a minute of 403s.
    Router(session.client("cloudfront-keyvaluestore", region_name="us-east-1"), cfg["kvs_arn"]).rest()
    t = tower(session, cfg)
    t.set_state(status="resting", phase="SETTLING")
    if not args.now:
        print(f"routed to resting; waiting {EDGE_PROPAGATION_S} s for the edge before removing copies", flush=True)
        time.sleep(EDGE_PROPAGATION_S)
    removed = [r for r in cfg["allowlist"] if placement.remove(session.client("lambda", region_name=r))]
    t.set_state(status="resting", phase="RESTING")
    t.event("RESTING", removed=removed)
    print(f"resting; removed copies in: {', '.join(removed) or 'none'}")


SWITCHES = {"stay_seconds", "hop_regions", "max_hops", "kill", "pin_awake", "inject"}


def cmd_set(session: boto3.Session, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    if args.name not in SWITCHES:
        sys.exit(f"{args.name} is not a switch; choose from {sorted(SWITCHES)}")
    session.client("ssm", region_name=HOME).put_parameter(
        Name=f"/traveler/{args.name}", Value=args.value, Type="String", Overwrite=True
    )
    print(f"{args.name} = {args.value}")


def cmd_unset(session: boto3.Session, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    if args.name not in SWITCHES - {"kill", "pin_awake"}:
        sys.exit(f"{args.name} cannot be unset; use `set {args.name} false`")
    ssm = session.client("ssm", region_name=HOME)
    with contextlib.suppress(ssm.exceptions.ParameterNotFound):
        ssm.delete_parameter(Name=f"/traveler/{args.name}")
    print(f"{args.name} unset")


def cmd_status(session: boto3.Session, cfg: dict[str, Any], _: argparse.Namespace) -> None:
    s = tower(session, cfg).state()
    present = copies(session, cfg["allowlist"])
    print(f"status    {s.get('status', 'unknown')}")
    for k in (
        "region",
        "hop_id",
        "release",
        "phase",
        "next_region",
        "departure_at",
        "heartbeat_at",
        "hops_ok",
        "hops_aborted",
    ):
        if k in s:
            print(f"{k:<9} {s[k]}")
    print(f"copies    {len(present)} {present if present else ''}")
    if s.get("status") == "live" and present != [s.get("region")]:
        print("WARNING   copies do not match the tower state")


# --- main -------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="ops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("release")
    wake = sub.add_parser("wake")
    wake.add_argument("--region")
    sleep = sub.add_parser("sleep")
    sleep.add_argument("--now", action="store_true", help="skip the edge wait (emergency stop)")
    sub.add_parser("status")
    set_ = sub.add_parser("set")
    set_.add_argument("name")
    set_.add_argument("value")
    unset = sub.add_parser("unset")
    unset.add_argument("name")
    args = parser.parse_args()

    session = boto3.Session()
    cfg = settings(session)
    commands = {
        "release": cmd_release,
        "wake": cmd_wake,
        "sleep": cmd_sleep,
        "status": cmd_status,
        "set": cmd_set,
        "unset": cmd_unset,
    }
    commands[args.cmd](session, cfg, args)


if __name__ == "__main__":
    main()

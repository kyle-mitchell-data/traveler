"""Operator commands: release, wake, sleep, status.

    python -m scripts.ops release          build the traveler zip, upload, set desired_release
    python -m scripts.ops wake [--region]  place the traveler from the latest release and route to it
    python -m scripts.ops sleep            route to "resting" and remove every traveler copy
    python -m scripts.ops status           tower state plus a live count of copies
    python -m scripts.ops facts [--region] preview a stay's statements from live sources (writes nothing)

Settings come from SSM under /traveler/ (written by Terraform). Status reads the tower,
never the key-value store (decision D5).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from traveler import placement, regions, wake
from traveler.facts import engine
from traveler.facts.model import Context
from traveler.routing import EDGE_PROPAGATION_S, Router
from traveler.tower import Tower

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
    router = Router(session.client("cloudfront-keyvaluestore", region_name="us-east-1"), cfg["kvs_arn"])
    print(f"waking {release} in {region}...")
    try:
        hop_id = wake.place_and_start(
            tower=t,
            lambda_for=lambda r: session.client("lambda", region_name=r),
            set_route=router.set,
            zip_bytes=zip_bytes,
            release=release,
            region=region,
            role_arn=cfg["role_arn"],
            distribution_arn=cfg["distribution_arn"],
            tower_table=cfg["tower_table"],
            home=HOME,
        )
    except placement.PlacementError as exc:
        sys.exit(f"wake failed at {exc.phase}: {exc.reason}")
    print(f"live in {region} as {hop_id}; residence started")


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


SWITCHES = {"stay_seconds", "hop_regions", "max_hops", "kill", "pin_awake", "inject", "adapters_off"}


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


def cmd_facts(session: boto3.Session, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    """What a 5-minute stay would say right now, against the real sources and memory."""
    t = tower(session, cfg)
    region = args.region or str(t.state().get("region") or HOME)
    ctx = Context(
        region=region,
        profile=regions.profile(region),
        now=datetime.now(UTC),
        hop_id=f"preview-{int(time.time())}",
        hop_count=int(t.state().get("hop_count") or 0),
        from_region=args.origin or "",
        fetch=_fetcher(cfg),
    )
    plan = engine.plan(ctx, slots=engine.slots_for(300), memory=t.region_memory(region))
    for st in plan.statements:
        print(f"{st['slot']:>2}. [{st['category']}] {st['text']}  ({st['source_name']})")
    print(
        "adapters:",
        ", ".join(f"{k} {'ok' if r.get('ok') else r.get('error', 'off')}" for k, r in plan.adapters.items()),
    )


def _fetcher(cfg: dict[str, Any]) -> Any:
    try:  # Windows Python has no CA bundle of its own
        import certifi

        tls = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        tls = ssl.create_default_context()
    agent = f"traveler-preview (+{cfg.get('site_url', '')})"

    def fetch(url: str) -> Any:
        req = urllib.request.Request(url, headers={"user-agent": agent, "accept": "application/json"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=engine.ADAPTER_TIMEOUT_S, context=tls) as resp:  # noqa: S310
            return json.loads(resp.read())

    return fetch


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
    facts = sub.add_parser("facts")
    facts.add_argument("--region")
    facts.add_argument("--origin", help="pretend we just flew in from this region")
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
        "facts": cmd_facts,
        "set": cmd_set,
        "unset": cmd_unset,
    }
    commands[args.cmd](session, cfg, args)


if __name__ == "__main__":
    main()

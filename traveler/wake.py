"""Place the traveler from a release and start its residence loop.

Shared by `make wake` (scripts/ops.py) and the watchdog's resurrection, so both
bring the traveler up the same way: create, health check, route, record, start.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from traveler import placement
from traveler.tower import Tower, now_iso


def place_and_start(
    *,
    tower: Tower,
    lambda_for: Callable[[str], Any],
    set_route: Callable[..., None],
    zip_bytes: bytes,
    release: str,
    region: str,
    role_arn: str,
    distribution_arn: str,
    tower_table: str,
    home: str,
    event_type: str = "WOKE",
    awake_since: str | None = None,
) -> str:
    """Returns the new hop ID. Raises PlacementError (after cleaning up) if the copy is unhealthy."""
    hop_id = tower.next_hop_id()
    arrived = now_iso()
    lam = lambda_for(region)
    try:
        placed = placement.create(
            lam,
            zip_bytes=zip_bytes,
            role_arn=role_arn,
            distribution_arn=distribution_arn,
            env={
                "TOWER_TABLE": tower_table,
                "TOWER_REGION": home,
                "TRAVELER_RELEASE": release,
                "TRAVELER_HOP_ID": hop_id,
                "TRAVELER_ARRIVED_AT": arrived,
            },
        )
        placement.health(lam, expect_release=release)
    except placement.PlacementError as exc:
        placement.remove(lam)
        tower.event("HOP_ABORTED", hop_id=hop_id, region=region, phase=exc.phase, reason=exc.reason)
        raise

    set_route(current_origin=placed.url_domain, current_region=region, hop_id=hop_id, status="live")
    prior = tower.state()
    tower.set_state(
        status="live",
        region=region,
        hop_id=hop_id,
        arrived_at=arrived,
        release=release,
        function_url=placed.url_domain,
        phase="RESIDENT",
        heartbeat_at=arrived,
        awake_since=awake_since or arrived,
        consecutive_aborts=0,
    )
    tower.event(event_type, hop_id=hop_id, region=region, release=release)
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
    return hop_id

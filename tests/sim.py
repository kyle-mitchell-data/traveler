"""Local simulation of the traveler's world.

Runs the real traveler.lifecycle against fake regions, a fake edge with propagation
delay, and a moto DynamoDB tower, on a sped-up clock. Each async invoke starts a real
thread, so the old and new copies genuinely overlap during a handoff, as in AWS.

Failures are injected per hop: cloud.fail["create"] = "h-000002" makes P2 fail for that
hop only. Supported: create, active, health, edge_stuck, drop_arrival, late_arrival.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

from traveler import lifecycle, placement
from traveler.tower import Tower

FAILURES = ("create", "active", "health", "edge_stuck", "drop_arrival", "late_arrival")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class LockedTable:
    """Serializes moto calls; the tower is shared by several threads."""

    def __init__(self, table: Any, lock: threading.RLock) -> None:
        self._table = table
        self._lock = lock
        self.meta = table.meta

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._table, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return fn(*args, **kwargs)

        return call


class FakeCloud:
    def __init__(self, table: Any, *, scale: float = 250, propagation_s: float = 60) -> None:
        self.lock = threading.RLock()
        self.table = LockedTable(table, threading.RLock())
        self.scale = scale
        self.propagation_s = propagation_s
        self._t0 = time.monotonic()
        self.functions: dict[str, dict[str, str]] = {}
        self.routes: list[tuple[float, dict[str, str]]] = [(-1e9, {"status": "resting"})]
        self.settings: dict[str, str] = {}
        self.packages: dict[str, bytes] = {"r1": b"PK-r1"}
        self.fail: dict[str, str] = {}
        self.max_copies = 0
        self.violations: list[str] = []
        self.errors: list[BaseException] = []
        self.logs: list[dict[str, Any]] = []
        self._threads: list[threading.Thread] = []
        self._urls = itertools.count(1)

    # --- clock ----------------------------------------------------------------

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.scale

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds) / self.scale)

    # --- functions ------------------------------------------------------------

    def _count(self) -> None:
        n = len(self.functions)
        self.max_copies = max(self.max_copies, n)
        if n > 2:
            self.violations.append(f"{n} copies at t={self.now():.0f}s: {sorted(self.functions)}")

    def client(self, region: str) -> FakeLambda:
        return FakeLambda(self, region)

    # --- edge -----------------------------------------------------------------

    def set_route(self, **keys: str) -> None:
        with self.lock:
            current = dict(self.routes[-1][1])
            current.update(keys)
            self.routes.append((self.now(), current))

    def probe(self) -> str | None:
        """What a visitor sees: the route as it was propagation_s ago, if that copy still exists."""
        with self.lock:
            horizon = self.now() - self.propagation_s
            view = next(r for t, r in reversed(self.routes) if t <= horizon)
            if view.get("status") != "live":
                return None
            if self.fail.get("edge_stuck") == view.get("hop_id"):
                return None
            fn = self.functions.get(view.get("current_region", ""))
            if not fn or fn["url"] != view.get("current_origin"):
                return None  # stale edge pointing at a deleted copy: a visitor would get 403
            return view["current_region"]

    # --- invocations ----------------------------------------------------------

    def invoke_async(self, region: str, payload: dict[str, Any]) -> None:
        hop = payload.get("hop_id")
        if payload.get("from_region") and self.fail.get("drop_arrival") == hop:
            return
        delay = 70.0 if payload.get("from_region") and self.fail.get("late_arrival") == hop else 0.0
        with self.lock:
            if region not in self.functions:
                return
        thread = threading.Thread(target=self._run, args=(region, payload, delay), daemon=True)
        with self.lock:
            self._threads.append(thread)
        thread.start()

    def _run(self, region: str, payload: dict[str, Any], delay: float) -> None:
        if delay:
            self.sleep(delay)
            with self.lock:
                if region not in self.functions:
                    return  # deleted while queued: the event would be dropped
        try:
            lifecycle.run(FakeWorld(self, region), payload)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            self.errors.append(exc)

    def wait_idle(self, timeout_s: float = 120) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self.lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive:
                return
            alive[0].join(timeout=0.5)
        raise TimeoutError("simulation still running")

    # --- operator -------------------------------------------------------------

    def wake(self, region: str, release: str = "r1") -> str:
        """What `make wake` does: place, route, record, start the residence loop."""
        tower = Tower(self.table)
        tower.set_desired_release(release)
        hop_id = tower.next_hop_id()
        lam = self.client(region)
        placed = placement.create(
            lam,
            zip_bytes=self.packages[release],
            role_arn="arn:role/traveler",
            distribution_arn="arn:dist/X",
            env={"TRAVELER_RELEASE": release, "TRAVELER_HOP_ID": hop_id},
        )
        self.set_route(current_origin=placed.url_domain, current_region=region, hop_id=hop_id, status="live")
        tower.set_state(status="live", region=region, hop_id=hop_id)
        self.invoke_async(
            region,
            {"mode": "residence", "hop_id": hop_id, "hop_count": 0, "recent_regions": [region], "release": release},
        )
        return hop_id


class FakeLambda:
    """The slice of the Lambda client that placement and lifecycle use."""

    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        ResourceNotFoundException = NotFound

    def __init__(self, cloud: FakeCloud, region: str) -> None:
        self.cloud = cloud
        self.region = region

        class Meta:
            region_name = region

        self.meta = Meta()

    def _fn(self) -> dict[str, str]:
        fn = self.cloud.functions.get(self.region)
        if fn is None:
            raise NotFound(self.region)
        return fn

    def create_function(self, **kw: Any) -> dict[str, Any]:
        env = kw["Environment"]["Variables"]
        if self.cloud.fail.get("create") == env["TRAVELER_HOP_ID"]:
            raise RuntimeError("injected: CreateFunction failed")
        with self.cloud.lock:
            if self.region in self.cloud.functions:
                raise Conflict(f"traveler already exists in {self.region}")
            self.cloud.functions[self.region] = {
                "release": env["TRAVELER_RELEASE"],
                "hop_id": env["TRAVELER_HOP_ID"],
                "url": "",
            }
            self.cloud._count()
        return {"FunctionArn": f"arn:aws:lambda:{self.region}:1:function:traveler"}

    def get_waiter(self, name: str) -> Any:
        lam = self

        class Waiter:
            def wait(self, **kw: Any) -> None:
                lam.cloud.sleep(3)
                if lam.cloud.fail.get("active") == lam._fn()["hop_id"]:
                    raise RuntimeError("injected: function never became Active")

        return Waiter()

    def put_function_event_invoke_config(self, **kw: Any) -> None:
        self._fn()

    def put_function_recursion_config(self, **kw: Any) -> None:
        self._fn()

    def create_function_url_config(self, **kw: Any) -> dict[str, str]:
        fn = self._fn()
        fn["url"] = f"u{next(self.cloud._urls)}.lambda-url.{self.region}.on.aws"
        return {"FunctionUrl": f"https://{fn['url']}/"}

    def get_function_url_config(self, **kw: Any) -> dict[str, str]:
        return {"FunctionUrl": f"https://{self._fn()['url']}/"}

    def add_permission(self, **kw: Any) -> None:
        self._fn()

    def get_function_configuration(self, **kw: Any) -> dict[str, Any]:
        return dict(self._fn())

    def invoke(self, **kw: Any) -> dict[str, Any]:
        import io
        import json

        fn = self._fn()
        release = fn["release"]
        if self.cloud.fail.get("health") == fn["hop_id"]:
            release = "wrong-release"
        body = {"region": self.region, "release": release}
        return {"Payload": io.BytesIO(json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode())}

    def delete_function(self, **kw: Any) -> None:
        with self.cloud.lock:
            if self.region not in self.cloud.functions:
                raise NotFound(self.region)
            del self.cloud.functions[self.region]


class FakeWorld:
    def __init__(self, cloud: FakeCloud, region: str) -> None:
        self.cloud = cloud
        self.region = region
        self.tower = Tower(cloud.table)
        self._started = cloud.now()

    def now(self) -> float:
        return self.cloud.now()

    def sleep(self, seconds: float) -> None:
        self.cloud.sleep(seconds)

    def remaining_s(self) -> float:
        return placement.TIMEOUT_S - (self.cloud.now() - self._started)

    def lambda_client(self, region: str) -> Any:
        return self.cloud.client(region)

    def set_route(self, **keys: str) -> None:
        self.cloud.set_route(**keys)

    def probe(self) -> str | None:
        return self.cloud.probe()

    def package(self, release: str) -> bytes:
        return self.cloud.packages[release]

    def invoke_async(self, region: str, payload: dict[str, Any]) -> None:
        self.cloud.invoke_async(region, payload)

    def setting(self, name: str) -> str | None:
        return self.cloud.settings.get(name)

    def placement_args(self, *, release: str, hop_id: str) -> dict[str, Any]:
        return {
            "zip_bytes": self.package(release),
            "role_arn": "arn:role/traveler",
            "distribution_arn": "arn:dist/X",
            "env": {"TRAVELER_RELEASE": release, "TRAVELER_HOP_ID": hop_id},
        }

    def log(self, msg: str, **fields: Any) -> None:
        self.cloud.logs.append({"t": round(self.now()), "region": self.region, "msg": msg, **fields})

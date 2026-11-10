"""Edge routing keys in the CloudFront KeyValueStore.

Every API call here costs $0.001 (decision D5), so this module only writes. Callers
that need to know the current route read the tower instead.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Measured 2026-11-06 (decision D21): edge servers converge on a key change in about
# 60 s. Anything that retires the old origin waits at least this long after a flip.
EDGE_PROPAGATION_S = 90


class EtagStore(Protocol):
    """Keeps the last ETag somewhere that outlives one Lambda container."""

    def get(self) -> str | None: ...
    def put(self, etag: str) -> None: ...


class Router:
    def __init__(self, kvs_client: Any, kvs_arn: str, etag_store: EtagStore | None = None) -> None:
        self.kvs = kvs_client
        self.arn = kvs_arn
        self.store = etag_store
        self._etag: str | None = None

    def set(self, **keys: str) -> None:
        """Write all keys in one all-or-nothing UpdateKeys call."""
        if self._etag is None and self.store is not None:
            # Flips are minutes apart, so the writer is nearly always a cold container
            # (seen 2026-11-10); the stored ETag saves a DescribeKeyValueStore call.
            try:
                self._etag = self.store.get()
            except Exception:
                log.warning("could not read the stored ETag", exc_info=True)
        if self._etag is None:
            self._etag = self.kvs.describe_key_value_store(KvsARN=self.arn)["ETag"]
        puts = [{"Key": k, "Value": v} for k, v in keys.items()]
        try:
            out = self.kvs.update_keys(KvsARN=self.arn, IfMatch=self._etag, Puts=puts)
        except Exception as exc:
            if not _stale_etag(exc):
                raise
            # Someone else wrote since our cached ETag; refresh once and retry.
            self._etag = self.kvs.describe_key_value_store(KvsARN=self.arn)["ETag"]
            out = self.kvs.update_keys(KvsARN=self.arn, IfMatch=self._etag, Puts=puts)
        # Cache the new ETag so the next flip is one API call, not two.
        self._etag = out["ETag"]
        if self.store is not None:
            try:
                self.store.put(out["ETag"])
            except Exception:
                # The flip already happened; the next one just pays for a describe.
                log.warning("could not store the ETag", exc_info=True)

    def go_live(self, *, origin: str, region: str, hop_id: str) -> None:
        self.set(current_origin=origin, current_region=region, hop_id=hop_id, status="live")

    def rest(self) -> None:
        self.set(status="resting")


def _stale_etag(exc: Exception) -> bool:
    """True if UpdateKeys failed because our cached ETag is out of date.

    The API reports this as ValidationException "Pre-Condition failed" (seen in
    production 2026-11-06), though ConflictException is also documented.
    """
    error = getattr(exc, "response", {}).get("Error", {})
    code, message = error.get("Code", ""), error.get("Message", "")
    return code == "ConflictException" or (code == "ValidationException" and "Pre-Condition" in message)

"""Edge routing keys in the CloudFront KeyValueStore.

Every API call here costs $0.001 (decision D5), so this module only writes. Callers
that need to know the current route read the tower instead.
"""

from __future__ import annotations

from typing import Any


class Router:
    def __init__(self, kvs_client: Any, kvs_arn: str) -> None:
        self.kvs = kvs_client
        self.arn = kvs_arn
        self._etag: str | None = None

    def set(self, **keys: str) -> None:
        """Write all keys in one all-or-nothing UpdateKeys call."""
        if self._etag is None:
            self._etag = self.kvs.describe_key_value_store(KvsARN=self.arn)["ETag"]
        puts = [{"Key": k, "Value": v} for k, v in keys.items()]
        try:
            out = self.kvs.update_keys(KvsARN=self.arn, IfMatch=self._etag, Puts=puts)
        except self.kvs.exceptions.ConflictException:
            # Someone else wrote since our cached ETag; refresh once and retry.
            self._etag = self.kvs.describe_key_value_store(KvsARN=self.arn)["ETag"]
            out = self.kvs.update_keys(KvsARN=self.arn, IfMatch=self._etag, Puts=puts)
        # Cache the new ETag so the next flip is one API call, not two.
        self._etag = out["ETag"]

    def go_live(self, *, origin: str, region: str, hop_id: str) -> None:
        self.set(current_origin=origin, current_region=region, hop_id=hop_id, status="live")

    def rest(self) -> None:
        self.set(status="resting")

"""Candidate facts and the context adapters gather them in."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

FRESHNESS = ("live", "today", "evergreen")


@dataclass(frozen=True)
class Fact:
    category: str
    freshness: str  # live | today | evergreen
    template_key: str
    values: dict[str, Any]
    source_name: str
    source_url: str = ""
    now: bool = False  # may open a stay (slot 1 is a "right now" fact)
    pack: bool = False  # from the curated evergreen pack
    rarity: float = 0.0  # extra score for unusual facts: a big quake, a holiday today
    id: str = ""  # stable id; derived from the content when empty

    @property
    def fact_id(self) -> str:
        if self.id:
            return self.id
        blob = json.dumps([self.category, self.template_key, self.values], sort_keys=True, default=str)
        return f"{self.category[:4]}-{hashlib.sha1(blob.encode()).hexdigest()[:8]}"  # noqa: S324 - an id, not security


@dataclass
class Context:
    region: str
    profile: dict[str, Any]
    now: datetime  # timezone-aware, UTC
    hop_id: str
    hop_count: int = 0
    from_region: str = ""
    fetch: Callable[[str], Any] = field(default=lambda url: _offline(url))

    @property
    def near(self) -> str:
        return str(self.profile.get("near") or f"in {self.region}")

    @property
    def country(self) -> str:
        return str(self.profile.get("country") or "")

    @property
    def country_name(self) -> str:
        """The country as it reads mid-sentence: 'the United States', 'Japan'."""
        name = self.country
        return f"the {name}" if name.startswith(("United ", "Republic ")) else name


def _offline(url: str) -> Any:
    raise ConnectionError(f"no network: {url}")

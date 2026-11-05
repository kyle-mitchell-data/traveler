"""Region profiles: static facts about where each allowlisted region is."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent / "regions"


@cache
def profile(code: str) -> dict[str, Any]:
    path = _DIR / f"{code}.json"
    if not path.is_file():
        return {"code": code, "city": code, "near": f"in {code}"}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def known() -> list[str]:
    return sorted(p.stem for p in _DIR.glob("*.json"))

"""Regenerate site/map/land.txt, the map's land outline as one SVG path.

    python -m scripts.build_map ne_110m_land.geojson

Source: Natural Earth 1:110m land (public domain), naturalearthdata.com, from
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson
Run once; the output is committed so builds need no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from scripts.site_build import BOTTOM_LAT, SITE, TOP_LAT, project


def _ring(points: list[list[float]]) -> str:
    out: list[str] = []
    last = None
    for lon, lat in points:
        x, y = project(lon, max(BOTTOM_LAT - 2, min(TOP_LAT + 2, lat)))
        if (x, y) == last:
            continue
        out.append(f"{x:g},{y:g}")
        last = (x, y)
    return "M" + "L".join(out) + "Z" if len(out) > 2 else ""


def land_path(geojson: dict[str, Any]) -> str:
    parts: list[str] = []
    for feature in geojson["features"]:
        geom = feature["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            outer = poly[0]
            if max(lat for _, lat in outer) < BOTTOM_LAT:
                continue  # Antarctica and nearby islands are off the map
            parts.extend(r for r in (_ring(ring) for ring in poly) if r)
    return "".join(parts)


def main(argv: list[str]) -> int:
    data = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out = SITE / "map" / "land.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    path = land_path(data)
    out.write_text(path + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(path):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

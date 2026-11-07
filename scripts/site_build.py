"""Build the static site into a directory ready for S3.

    python -m scripts.site_build [OUT]     default build/site

Inlines the land outline and the region profiles into index.html, and gives the CSS
and JS content-hashed names so they can be cached for a year (index.html is cached
for 60 s). The map projection lives here and in site/model.js; tests keep them equal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from traveler import regions

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
ASSETS = ("model.js", "app.js", "app.css")

# Equal Earth projection (Šavrič, Patterson, Jenny 2018), scaled to a 1000-unit-wide
# map. Antarctica is cropped: the map runs from 84°N down to 58°S.
_A1, _A2, _A3, _A4 = 1.340264, -0.081106, 0.000893, 0.003796
_M = math.sqrt(3) / 2
WIDTH = 1000.0
TOP_LAT, BOTTOM_LAT = 84.0, -58.0


def _raw(lon: float, lat: float) -> tuple[float, float]:
    t = math.asin(_M * math.sin(math.radians(lat)))
    t2 = t * t
    t6 = t2 * t2 * t2
    denom = 3 * (9 * _A4 * t6 * t2 + 7 * _A3 * t6 + 3 * _A2 * t2 + _A1)
    x = 2 * math.sqrt(3) * math.radians(lon) * math.cos(t) / denom
    y = t * (_A1 + _A2 * t2 + t6 * (_A3 + _A4 * t2))
    return x, y


SCALE = WIDTH / (2 * _raw(180, 0)[0])
_TOP = _raw(0, TOP_LAT)[1]
HEIGHT = round(SCALE * (_TOP - _raw(0, BOTTOM_LAT)[1]), 1)


def project(lon: float, lat: float) -> tuple[float, float]:
    x, y = _raw(lon, lat)
    return round(WIDTH / 2 + SCALE * x, 1), round(SCALE * (_TOP - y), 1)


def region_data() -> list[dict[str, Any]]:
    out = []
    for code in regions.known():
        p = regions.profile(code)
        x, y = project(p["lon"], p["lat"])
        out.append(
            {
                "code": code,
                "city": p["city"],
                "near": p["near"],
                "country": p.get("country", ""),
                "lat": p["lat"],
                "lon": p["lon"],
                "tz": p["tz"],
                "x": x,
                "y": y,
            }
        )
    return out


def _hashed(name: str, body: bytes) -> str:
    stem, ext = name.rsplit(".", 1)
    return f"{stem}.{hashlib.sha256(body).hexdigest()[:10]}.{ext}"


def build(out: Path) -> dict[str, str]:
    """Write the site to `out`; returns {source name: published name}."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        if old.is_file():
            old.unlink()

    names: dict[str, str] = {}
    model = (SITE / "model.js").read_bytes()
    names["model.js"] = _hashed("model.js", model)
    app = (SITE / "app.js").read_text(encoding="utf-8").replace("'./model.js'", f"'./{names['model.js']}'")
    names["app.js"] = _hashed("app.js", app.encode())
    css = (SITE / "app.css").read_bytes()
    names["app.css"] = _hashed("app.css", css)
    (out / names["model.js"]).write_bytes(model)
    (out / names["app.js"]).write_text(app, encoding="utf-8", newline="\n")
    (out / names["app.css"]).write_bytes(css)

    land = (SITE / "map" / "land.txt").read_text(encoding="utf-8").strip()
    data = json.dumps({"regions": region_data(), "width": WIDTH, "height": HEIGHT}, separators=(",", ":"))
    html = (SITE / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{LAND}}", land).replace("{{MAP_DATA}}", data.replace("</", "<\\/"))
    html = html.replace("{{VIEWBOX}}", f"0 0 {WIDTH:g} {HEIGHT:g}")
    for src, dst in names.items():
        html = html.replace(f'"{src}"', f'"{dst}"')
    left = re.findall(r"\{\{[A-Z_]+\}\}", html)
    if left:
        raise ValueError(f"unfilled placeholders in index.html: {left}")
    (out / "index.html").write_text(html, encoding="utf-8", newline="\n")
    return names


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else ROOT / "build" / "site"
    names = build(out)
    print(f"built {out}: index.html, " + ", ".join(names.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

import json
import re
from pathlib import Path

import pytest

from scripts import site_build


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site")
    site_build.build(out)
    return out


def test_only_the_page_and_hashed_assets_are_published(built: Path) -> None:
    names = sorted(re.sub(r"\.[0-9a-f]{10}\.", ".#.", p.name) for p in built.iterdir())
    assert names == ["app.#.css", "app.#.js", "index.html", "model.#.js"]


def test_page_references_the_hashed_files(built: Path) -> None:
    html = (built / "index.html").read_text(encoding="utf-8")
    app = next(built.glob("app.*.js")).read_text(encoding="utf-8")
    assert f'href="{next(built.glob("app.*.css")).name}"' in html
    assert f'src="{next(built.glob("app.*.js")).name}"' in html
    assert f"'./{next(built.glob('model.*.js')).name}'" in app
    assert "{{" not in html


def test_page_loads_nothing_from_other_hosts(built: Path) -> None:
    html = (built / "index.html").read_text(encoding="utf-8")
    assert not re.findall(r'(?:src|href)="(?:https?:)?//', html)


def test_map_data_has_every_region_with_its_pin(built: Path) -> None:
    html = (built / "index.html").read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="map-data">(.*?)</script>', html, re.S)
    assert raw
    data = json.loads(raw.group(1))
    codes = [r["code"] for r in data["regions"]]
    assert len(codes) == 17
    for r in data["regions"]:
        assert 0 <= r["x"] <= data["width"] and 0 <= r["y"] <= data["height"]
        assert r["near"].startswith(("near ", "in "))


def test_js_projection_fixture_matches_the_build() -> None:
    # site/model.test.js checks model.js against this file; keep it in step with Python.
    fixture = json.loads((site_build.SITE / "model.test.pins.json").read_text(encoding="utf-8"))
    assert fixture == [{k: r[k] for k in ("code", "lat", "lon", "x", "y")} for r in site_build.region_data()]


def test_known_places_land_where_expected() -> None:
    w, h = site_build.WIDTH, site_build.HEIGHT
    x, y = site_build.project(0, 0)
    assert x == w / 2 and 0.5 < y / h < 0.75  # the equator sits below the middle: the map stops at 58°S
    assert site_build.project(-180, 0)[0] == 0
    assert site_build.project(180, 0)[0] == w
    assert site_build.project(0, site_build.TOP_LAT)[1] == 0

import io
import zipfile

from scripts.ops import build_zip


def test_package_is_small_self_contained_and_reproducible() -> None:
    data = build_zip()
    assert len(data) < 2 * 1024 * 1024  # spec: under 2 MB, it travels every hop
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "traveler/handler.py" in names
    assert "traveler/regions/eu-central-1.json" in names
    assert not any("__pycache__" in n or n.startswith("tests/") for n in names)
    assert build_zip() == data  # fixed timestamps: same source, same bytes

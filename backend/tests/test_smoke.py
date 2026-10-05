"""Smoke tests that run without PyTorch, model weights or network access."""
import sys
import types
from datetime import date

import numpy as np

# Stand-in for app.model so the tests do not need torch or the weights file.
CLASSES = ["AnnualCrop", "Forest", "HerbaceousVegetation", "Highway", "Industrial",
           "Pasture", "PermanentCrop", "Residential", "River", "SeaLake"]


def fake_predict(tiles):
    """Bright tiles -> Residential, dark tiles -> Forest."""
    bright = tiles.mean(axis=(1, 2, 3)) > 0
    probs = np.full((len(tiles), 10), 0.01, dtype=np.float32)
    probs[bright, 7] = 0.91
    probs[~bright, 1] = 0.91
    return probs


stub = types.ModuleType("app.model")
stub.CLASSES = CLASSES
stub.Classifier = lambda: fake_predict
sys.modules["app.model"] = stub

from fastapi.testclient import TestClient  # noqa: E402

from app import analysis, imagery, main  # noqa: E402


def fake_scene(grid, target, **_):
    n = grid["n"]
    rgb = np.full((n, n, 3), 30, dtype=np.uint8)            # dark = "forest"
    if target.year >= 2025:
        rgb[: n // 2, : n // 2] = 220                        # top-left quarter gets built up
    water = np.zeros((n // 4, n // 4), dtype=bool)
    water[-(n // 16):] = True                                # a strip of sea at the bottom
    return {"rgb": rgb, "water": water, "nodata": np.zeros_like(water),
            "date": target.isoformat(), "bad": 0.0}


imagery.fetch_scene = fake_scene
client = TestClient(main.app)
PLACE = {"lat": 41.26, "lon": 28.74, "size_km": 20}


def test_grid_geometry():
    grid = imagery.make_grid(41.26, 28.74, 20)
    assert grid["n"] == 1984 and grid["crs"] == "EPSG:32635"
    c = imagery.corners(grid)
    assert len(c) == 4 and c[0][0] < 28.74 < c[1][0] and c[2][1] < 41.26 < c[0][1]


def test_cell_sizes():
    grid = imagery.make_grid(41.26, 28.74, 20)
    scene = fake_scene(grid, date(2025, 7, 20))
    for cell, rows in [(320, 61), (640, 31), (1280, 15)]:
        r = analysis.classify(scene, fake_predict, cell)
        assert r["groups"].shape == (rows, rows), (cell, r["groups"].shape)
        assert set(np.unique(r["groups"])) <= {1, 2, 3}      # vegetation, built, water


def test_water_mask_overrides_model():
    grid = imagery.make_grid(41.26, 28.74, 20)
    r = analysis.classify(fake_scene(grid, date(2016, 7, 20)), fake_predict, 640)
    water = analysis.GROUP_KEYS.index("water")
    assert (r["groups"][-1] == water).all() and (r["groups"][0] != water).all()


def test_analyze_endpoint():
    res = client.get("/api/analyze", params={**PLACE, "when": "2025"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["image"]["src"].startswith("data:image/jpeg;base64,")
    assert abs(sum(g["share"] for g in body["stats"]["groups"]) - 100) < 0.5
    assert body["grid"]["rows"] == 31 and len(body["grid"]["groups"]) == 31


def test_compare_endpoint():
    res = client.get("/api/compare", params={**PLACE, "before": "2016", "after": "2025"})
    assert res.status_code == 200, res.text
    body = res.json()
    quarter_km2 = (1984 // 2 // 64) ** 2 * 0.4096        # tiles fully inside the built quarter
    assert body["built_km2"] >= quarter_km2 * 0.9
    assert body["transitions"][0] == {"from": "vegetation", "to": "built", "km2": body["built_km2"]}
    assert body["season_warning"] is False


def test_season_warning_and_validation():
    res = client.get("/api/compare", params={**PLACE, "before": "2016-01-15", "after": "2025-07-20"})
    assert res.json()["season_warning"] is True
    assert client.get("/api/compare", params={**PLACE, "before": "2025", "after": "2016"}).status_code == 422
    assert client.get("/api/analyze", params={**PLACE, "cell_m": 500}).status_code == 422


def test_timeseries_endpoint():
    res = client.get("/api/timeseries", params={**PLACE, "start": 2023, "end": 2025})
    assert res.status_code == 200, res.text
    points = res.json()["points"]
    assert [p["year"] for p in points] == [2023, 2024, 2025] and "image" not in points[0]

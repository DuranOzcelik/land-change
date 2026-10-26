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
from app.security import SECURITY_HEADERS  # noqa: E402


def fake_scene(grid, target, **_):
    n = grid["n"]
    rgb = np.full((n, n, 3), 30, dtype=np.uint8)            # dark = "forest"
    if target.year >= 2025:
        rgb[: n // 2, : n // 2] = 220                        # top-left quarter gets built up
    water = np.zeros((n // 4, n // 4), dtype=bool)
    water[-(n // 16):] = True                                # a strip of sea at the bottom
    cloud = np.zeros_like(water)
    if target.year == 2020:
        cloud[: n // 8, n // 8:] = True                      # clouds over the top-right half
        rgb[: n // 2, n // 2:] = 250
    return {"rgb": rgb, "water": water, "nodata": np.zeros_like(water), "cloud": cloud,
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


def test_numbers_do_not_depend_on_map_scale():
    answers = [client.get("/api/compare", params={**PLACE, "before": "2016", "after": "2025", "cell_m": c}).json()
               for c in (320, 640, 1280)]
    assert len({(a["built_km2"], a["net_built_km2"], a["changed_km2"]) for a in answers}) == 1
    assert [a["before"]["grid"]["rows"] for a in answers] == [61, 31, 15]
    assert answers[0]["before"]["stats"] == answers[2]["before"]["stats"]


def test_cloudy_cells_are_left_out():
    clear = client.get("/api/analyze", params={**PLACE, "when": "2016"}).json()
    cloudy = client.get("/api/analyze", params={**PLACE, "when": "2020"}).json()
    assert cloudy["stats"]["total_km2"] < clear["stats"]["total_km2"] == clear["stats"]["area_km2"]
    assert cloudy["stats"]["area_km2"] == clear["stats"]["area_km2"]
    built = {g["key"]: g["km2"] for g in cloudy["stats"]["groups"]}["built"]
    assert built == 0                                        # bright clouds must not count as built-up
    assert 0 in cloudy["grid"]["valid"][0] and 1 in cloudy["grid"]["valid"][0]

    change = client.get("/api/compare", params={**PLACE, "before": "2016", "after": "2020"}).json()
    assert change["built_km2"] == 0 and change["net_built_km2"] == 0
    assert change["valid_km2"] == change["before"]["stats"]["total_km2"] < change["before"]["stats"]["area_km2"]


def test_consistent_headline_is_not_flagged():
    body = client.get("/api/compare", params={**PLACE, "before": "2016", "after": "2025"}).json()
    assert body["uncertain"] is False and abs(body["built_km2"] - body["net_built_km2"]) < 1


def test_security_headers_and_closed_docs():
    res = client.get("/health")
    assert res.headers["x-frame-options"] == "DENY" and res.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors" in res.headers["content-security-policy"]
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_cors_only_for_known_origins():
    foreign = client.get("/api/meta", headers={"Origin": "https://example.org"})
    local = client.get("/api/meta", headers={"Origin": "http://localhost:8080"})
    assert "access-control-allow-origin" not in foreign.headers
    assert local.headers["access-control-allow-origin"] == "http://localhost:8080"


def test_rate_limit():
    from app.security import RateLimiter, client_address
    limiter = RateLimiter(limit=3, window_s=60)
    assert [limiter.allow("a")[0] for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b")[0] is True
    assert client_address({"x-forwarded-for": "6.6.6.6, 203.0.113.9"}) == "203.0.113.9"

    main.limiter = RateLimiter(limit=2, window_s=60)
    try:
        codes = [client.get("/api/dates", params={**PLACE, "year": 1999}).status_code for _ in range(3)]
        assert codes[:2] == [422, 422] and codes[2] == 429
        assert client.get("/health").status_code == 200       # only the API is throttled
    finally:
        main.limiter = RateLimiter(limit=10_000, window_s=60)


def test_folder_addresses_redirect_to_their_slash():
    from pathlib import Path

    from fastapi import FastAPI
    site = FastAPI()
    site.mount("/", main.Site(directory=Path(__file__).resolve().parents[2] / "docs", html=True))
    pages = TestClient(site)
    res = pages.get("/tr", follow_redirects=False)
    assert res.status_code == 301 and res.headers["location"] == "/tr/"
    assert pages.get("/tr?x=1", follow_redirects=False).headers["location"] == "/tr/?x=1"
    turkish = pages.get("/tr/")
    assert turkish.status_code == 200 and '<html lang="tr">' in turkish.text
    assert pages.get("/").status_code == 200 and pages.get("/app.js").status_code == 200


def test_gzip_keeps_security_headers():
    gzip = {"Accept-Encoding": "gzip"}
    big = client.get("/api/analyze", params={**PLACE, "when": "2025"}, headers=gzip)
    assert big.status_code == 200 and big.headers["content-encoding"] == "gzip"
    small = client.get("/health", headers=gzip)
    assert small.status_code == 200 and "content-encoding" not in small.headers
    for res in (big, small):
        for name, value in SECURITY_HEADERS.items():
            assert res.headers[name] == value, name

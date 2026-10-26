"""Land Change API: land-cover statistics and change reports from Sentinel-2 imagery."""
from __future__ import annotations

import base64
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import analysis, imagery
from .model import CLASSES, Classifier
from .security import SECURITY_HEADERS, RateLimiter, client_address

# Interactive API docs are off unless explicitly enabled (API_DOCS=1).
_docs = os.environ.get("API_DOCS") == "1"
app = FastAPI(title="Land Change API", version="1.1.0",
              docs_url="/docs" if _docs else None, redoc_url=None,
              openapi_url="/openapi.json" if _docs else None)

# Browsers on other sites may not call the API. The site itself is same-origin and needs no entry.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
                   allow_methods=["GET"], allow_headers=["*"])
# Cloud Run does not compress responses; scripts, styles and JSON shrink several times with gzip.
app.add_middleware(GZipMiddleware, minimum_size=1000)

limiter = RateLimiter(limit=int(os.environ.get("RATE_LIMIT", "90")),
                      window_s=int(os.environ.get("RATE_WINDOW_S", "600")))
_heavy = threading.Semaphore(2)     # at most two analyses at once on a small instance


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/meta":
        allowed, retry = limiter.allow(client_address(request.headers))
        if not allowed:
            return JSONResponse({"detail": "Too many requests. Please try again later."}, status_code=429,
                                headers={"Retry-After": str(retry), **SECURITY_HEADERS})
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path.startswith(("/data/", "/vendor/")):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    else:
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


Lat = Query(..., ge=-60, le=75, description="Latitude")
Lon = Query(..., ge=-180, le=180, description="Longitude")
Size = Query(20, ge=5, le=25, description="Side of the analysed square in km")
Cell = Query(640, description="Cell size of the map grid in metres: 320, 640 or 1280")


@lru_cache(maxsize=1)
def classifier() -> Classifier:
    return Classifier()


def midsummer(year: int, lat: float) -> date:
    """Peak-vegetation reference day: 20 July in the north, 20 January in the south."""
    return date(year, 7, 20) if lat >= 0 else date(year, 1, 20)


def parse_when(value: str, lat: float) -> date:
    """'2021' means midsummer of that year; 'YYYY-MM-DD' is used as given."""
    try:
        when = midsummer(int(value), lat) if len(value) == 4 else date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "Dates must be YYYY or YYYY-MM-DD.")
    if when > date.today():
        raise HTTPException(422, f"No imagery for {value} yet.")
    if when < date(2015, 7, 1):
        raise HTTPException(422, "Sentinel-2 imagery starts in mid-2015.")
    return when


def check_cell(cell_m: int) -> int:
    if cell_m not in analysis.CELL_SIZES:
        raise HTTPException(422, f"cell_m must be one of {analysis.CELL_SIZES}.")
    return cell_m


def get_scene(grid, when: date) -> dict:
    try:
        return imagery.fetch_scene(grid, when)
    except imagery.NoImagery as err:
        raise HTTPException(404, str(err))


def evaluate(scene: dict, cell_m: int) -> tuple[dict, dict]:
    """(grid drawn on the map, grid the numbers come from).

    Statistics and change are always computed at the model's native 640 m cells, so the chosen
    map scale changes how the result looks, never what it says.
    """
    shown = analysis.classify(scene, classifier(), cell_m)
    base = shown if cell_m == analysis.BASE_CELL else analysis.classify(scene, classifier(), analysis.BASE_CELL)
    return shown, base


def jpeg(rgb) -> str:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def layer(grid, scene, shown, base, mask=None, with_image=True) -> dict:
    """Everything the front end needs to draw one date: image, cell grid and statistics."""
    out = {
        "date": scene["date"],
        "unusable_share": round(100 * scene["bad"], 1),
        "grid": {
            "rows": shown["rows"], "cols": shown["cols"],
            "corners": imagery.corners(grid, shown["inset_px"],
                                       shown["rows"] * shown["cell_px"], shown["cols"] * shown["cell_px"]),
            "groups": shown["groups"].tolist(),
            "classes": shown["classes"].tolist(),
            "confidence": (shown["conf"] * 100).round().astype(int).tolist(),
            "valid": shown["valid"].astype(int).tolist(),      # 0 = cloud, shadow or missing data
        },
        "stats": analysis.stats(base, mask),
    }
    if with_image:
        out["image"] = {"src": jpeg(scene["rgb"]), "corners": imagery.corners(grid)}
    return out


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/meta")
def meta():
    return {"classes": CLASSES, "groups": analysis.GROUPS, "cell_sizes": analysis.CELL_SIZES,
            "stats_cell_m": analysis.BASE_CELL}


@app.get("/api/dates")
def dates(lat: float = Lat, lon: float = Lon, size_km: float = Size,
          year: int = Query(..., ge=2015, le=2100)):
    """Acquisition dates available for a place in a given year."""
    grid = imagery.make_grid(lat, lon, size_km)
    end = min(date(year, 12, 31), date.today())
    return {"dates": imagery.available_dates(grid, date(year, 1, 1), end)}


@app.get("/api/analyze")
def analyze(lat: float = Lat, lon: float = Lon, size_km: float = Size, cell_m: int = Cell,
            when: str = Query(None, description="YYYY or YYYY-MM-DD; default is the latest summer")):
    """Current land-cover distribution of a place."""
    check_cell(cell_m)
    if when:
        target = parse_when(when, lat)
    else:
        today = date.today()
        target = midsummer(today.year, lat)
        if target > today:
            target = midsummer(today.year - 1, lat)
    grid = imagery.make_grid(lat, lon, size_km)
    with _heavy:
        scene = get_scene(grid, target)
        shown, base = evaluate(scene, cell_m)
    return {"cell_m": cell_m, "cell_km2": analysis.cell_km2(shown), **layer(grid, scene, shown, base)}


@app.get("/api/compare")
def compare(lat: float = Lat, lon: float = Lon, size_km: float = Size, cell_m: int = Cell,
            before: str = Query(..., description="YYYY or YYYY-MM-DD"),
            after: str = Query(..., description="YYYY or YYYY-MM-DD"),
            min_conf: float = Query(0.8, ge=0.5, le=0.99)):
    """Land-cover change between two dates."""
    check_cell(cell_m)
    day_a, day_b = parse_when(before, lat), parse_when(after, lat)
    if day_a >= day_b:
        raise HTTPException(422, "'before' must be earlier than 'after'.")
    grid = imagery.make_grid(lat, lon, size_km)
    with _heavy:
        with ThreadPoolExecutor(2) as pool:
            fa, fb = pool.submit(get_scene, grid, day_a), pool.submit(get_scene, grid, day_b)
            scene_a, scene_b = fa.result(), fb.result()
        shown_a, base_a = evaluate(scene_a, cell_m)
        shown_b, base_b = evaluate(scene_b, cell_m)
    change = analysis.compare(base_a, base_b, min_conf)                    # the numbers
    drawn = change if shown_a is base_a else analysis.compare(shown_a, shown_b, min_conf)   # the map

    doy_a = date.fromisoformat(scene_a["date"]).timetuple().tm_yday
    doy_b = date.fromisoformat(scene_b["date"]).timetuple().tm_yday
    gap = min(abs(doy_a - doy_b), 365 - abs(doy_a - doy_b))
    both = change["both_valid"]
    return {
        "cell_m": cell_m, "cell_km2": analysis.cell_km2(shown_a), "stats_cell_m": analysis.BASE_CELL,
        "before": layer(grid, scene_a, shown_a, base_a, both),
        "after": layer(grid, scene_b, shown_b, base_b, both),
        "changed": drawn["changed"].astype(int).tolist(),
        "changed_km2": change["changed_km2"], "built_km2": change["built_km2"],
        "net_built_km2": change["net_built_km2"], "valid_km2": change["valid_km2"],
        "uncertain": bool(change["uncertain"]),
        "transitions": change["transitions"],
        "season_gap_days": gap, "season_warning": gap > 45,
    }


@app.get("/api/timeseries")
def timeseries(lat: float = Lat, lon: float = Lon, size_km: float = Size, cell_m: int = Cell,
               start: int = Query(2016, ge=2016), end: int = Query(None)):
    """Group shares for every summer between two years."""
    check_cell(cell_m)
    end = end or date.today().year
    if not start <= end <= date.today().year or end - start > 12:
        raise HTTPException(422, "Choose at most 13 years, none in the future.")
    grid = imagery.make_grid(lat, lon, size_km)

    def one(year):
        target = midsummer(year, lat)
        if target > date.today():
            return None
        try:
            scene = imagery.fetch_scene(grid, target)
        except imagery.NoImagery:
            return None
        shown, base = evaluate(scene, cell_m)
        return {"year": year, **layer(grid, scene, shown, base, with_image=False)}

    with _heavy, ThreadPoolExecutor(3) as pool:
        points = [p for p in pool.map(one, range(start, end + 1)) if p]
    return {"cell_m": cell_m, "points": points}


class Site(StaticFiles):
    """The front end. A folder address without its slash (/tr) moves permanently to the folder (/tr/).
    The redirect carries only the path: behind Cloud Run the app sees plain http, so a full URL
    would send visitors to http."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (307, 308) and "location" in response.headers:
            target = urlsplit(response.headers["location"])
            return RedirectResponse(target.path + (f"?{target.query}" if target.query else ""), status_code=301)
        return response


# The front end is served by the same service. Registered last so the API routes win.
_site = Path(__file__).resolve().parent.parent / "static"
if _site.is_dir():
    app.mount("/", Site(directory=_site, html=True), name="site")

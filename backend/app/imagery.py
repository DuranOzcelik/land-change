"""Sentinel-2 retrieval from the Microsoft Planetary Computer STAC archive."""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform as warp_points
from rasterio.warp import transform_bounds

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
TILE = 64          # model input size in pixels
PIXEL_M = 10       # Sentinel-2 visual band resolution in metres
MASK_SCALE = 4     # scene classification layer is read at 40 m
SCL_WATER = 6
SCL_UNUSABLE = (0, 3, 8, 9, 10)   # no data, cloud shadow, clouds, cirrus
SCL_CLOUD = (3, 8, 9, 10)
CLEAN_SHARE = 0.03                # a scene with less unusable area than this is taken as clean
MAX_BATCHES = 3                   # how many batches of candidate dates to inspect

_catalog = None
_catalog_lock = threading.Lock()


def catalog():
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
        return _catalog


def make_grid(lat: float, lon: float, size_km: float) -> dict:
    """A square UTM pixel grid centred on (lat, lon); its side is a multiple of the tile size."""
    crs = f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}"
    xs, ys = warp_points("EPSG:4326", crs, [lon], [lat])
    n = int(size_km * 1000 / PIXEL_M) // TILE * TILE
    half = n * PIXEL_M / 2
    left = round((xs[0] - half) / PIXEL_M) * PIXEL_M
    top = round((ys[0] + half) / PIXEL_M) * PIXEL_M
    bbox = transform_bounds(crs, "EPSG:4326", left, top - n * PIXEL_M, left + n * PIXEL_M, top)
    return {"crs": crs, "left": left, "top": top, "n": n,
            "transform": from_origin(left, top, PIXEL_M, PIXEL_M), "bbox": list(bbox)}


def corners(grid: dict, inset_px: int = 0, height_px: int | None = None, width_px: int | None = None):
    """Lon/lat of a pixel rectangle inside the grid: top-left, top-right, bottom-right, bottom-left."""
    height_px = grid["n"] - 2 * inset_px if height_px is None else height_px
    width_px = grid["n"] - 2 * inset_px if width_px is None else width_px
    x0 = grid["left"] + inset_px * PIXEL_M
    y0 = grid["top"] - inset_px * PIXEL_M
    x1, y1 = x0 + width_px * PIXEL_M, y0 - height_px * PIXEL_M
    lons, lats = warp_points(grid["crs"], "EPSG:4326", [x0, x1, x1, x0], [y0, y0, y1, y1])
    return [[round(a, 6), round(b, 6)] for a, b in zip(lons, lats)]


def _read_mosaic(items, asset, bands, grid, scale=1):
    """Read one asset from same-day items onto the grid, filling gaps from neighbouring tiles."""
    size = grid["n"] // scale
    tf = grid["transform"] * rasterio.Affine.scale(scale)
    out = None
    for item in items:
        with rasterio.open(item.assets[asset].href) as src, WarpedVRT(
                src, crs=grid["crs"], transform=tf, width=size, height=size,
                resampling=Resampling.nearest) as vrt:
            arr = vrt.read(bands)
        if out is None:
            out = arr
        else:
            empty = (out == 0).all(axis=0)
            out[:, empty] = arr[:, empty]
        if not (out == 0).all(axis=0).any():
            break
    return out


def _search(grid, start: date, end: date, max_cloud: int):
    search = catalog().search(
        collections=[COLLECTION], bbox=grid["bbox"],
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lt": max_cloud}})
    by_date: dict[str, list] = {}
    for item in search.items():
        by_date.setdefault(item.properties["datetime"][:10], []).append(item)
    return by_date


def available_dates(grid, start: date, end: date, max_cloud: int = 30):
    """Acquisition dates with their scene-level cloud cover (cheap: metadata only)."""
    by_date = _search(grid, start, end, max_cloud)
    return [{"date": d, "cloud": round(float(np.mean([i.properties["eo:cloud_cover"] for i in items])), 1)}
            for d, items in sorted(by_date.items())]


class NoImagery(Exception):
    pass


_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
CACHE_SIZE = 12


def fetch_scene(grid, target: date, window_days: int = 50, max_cloud: int = 30, tries: int = 4) -> dict:
    """Closest usable acquisition to `target`: the nearest dates are checked for clouds and gaps
    inside the grid (not the whole Sentinel tile) and the nearest clean one wins."""
    key = (grid["crs"], grid["left"], grid["top"], grid["n"], target.isoformat())
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    by_date = _search(grid, target - timedelta(days=window_days),
                      min(target + timedelta(days=window_days), date.today()), max_cloud)
    if not by_date:
        raise NoImagery(f"No usable Sentinel-2 image within {window_days} days of {target.isoformat()}.")
    ordered = sorted(by_date, key=lambda d: abs((date.fromisoformat(d) - target).days))

    def check(d):
        scl = _read_mosaic(by_date[d], "SCL", [1], grid, scale=MASK_SCALE)[0]
        return float(np.isin(scl, SCL_UNUSABLE).mean()), d, scl

    # Check the nearest dates first; if none of them is clean, look further away in batches.
    checked: list = []
    for start in range(0, min(len(ordered), tries * MAX_BATCHES), tries):
        batch = ordered[start:start + tries]
        with ThreadPoolExecutor(len(batch)) as pool:
            checked += list(pool.map(check, batch))         # ordered by closeness to the target
        if any(c[0] < CLEAN_SHARE for c in checked):
            break
    clean = [c for c in checked if c[0] < CLEAN_SHARE]
    bad, chosen, scl = clean[0] if clean else min(checked, key=lambda c: c[0])

    rgb = _read_mosaic(by_date[chosen], "visual", [1, 2, 3], grid)
    scene = {"rgb": np.ascontiguousarray(rgb.transpose(1, 2, 0)), "water": scl == SCL_WATER,
             "nodata": scl == 0, "cloud": np.isin(scl, SCL_CLOUD), "date": chosen, "bad": round(bad, 4)}
    with _cache_lock:
        _cache[key] = scene
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return scene

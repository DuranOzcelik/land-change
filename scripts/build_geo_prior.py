"""Build docs/data/geo/: well-known geography the web app uses as prior knowledge.

Source: Natural Earth 1:10m (public domain), https://www.naturalearthdata.com
  ne_10m_ocean, ne_10m_lakes, ne_10m_glaciated_areas,
  ne_10m_geography_marine_polys, ne_10m_geography_regions_polys
The GeoJSON files are downloaded into scripts/.ne_cache/ on the first run.

The world is cut into 5-degree tiles. Each tile file holds
  w  open water well away from any shore: ocean and lakes shrunk by WATER_BUFFER_KM, because the
     Natural Earth coastline can be off by a kilometre or more. Cells whose centre lies here are
     counted as water whatever the model said.
  i  glaciers and ice caps shrunk by ICE_BUFFER_KM. Cells here are left out of the statistics,
     since EuroSAT has no ice class.
  n  named areas (seas, gulfs, lakes, deserts, mountain ranges, islands...) with Turkish and
     English names, for place labels and for the desert/mountain warning.
Polygons are stored as rings of flat [lon, lat, lon, lat, ...] arrays.

Usage:  python scripts/build_geo_prior.py      (needs: pip install shapely)
"""
import json
import math
import sys
import urllib.request
from pathlib import Path

from shapely import affinity
from shapely.geometry import box, shape, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

import datajs

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "scripts" / ".ne_cache"
OUT = ROOT / "docs" / "data" / "geo"
SOURCE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"

TILE = 5                  # degrees
LAT_RANGE = (-65, 80)     # the API accepts -60..75; a margin keeps labels near the limits
WATER_BUFFER_KM = 2.0
ICE_BUFFER_KM = 0.5
CLIP_MARGIN = 0.25        # degrees; keeps clip edges far from the tile before shrinking

MARINE = {"sea", "gulf", "bay", "strait", "sound", "channel", "lagoon", "fjord", "ocean", "inlet"}
REGIONS = {"Desert": "desert", "Range/mtn": "mountain", "Island": "island", "Island group": "island",
           "Peninsula": "peninsula", "Plateau": "plateau", "Tundra": "tundra", "Delta": "delta"}


def load(name):
    path = CACHE / f"{name}.geojson"
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name}")
        urllib.request.urlretrieve(SOURCE.format(name), path)
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def lake_name(props):
    """Turkish lake names in Natural Earth often lack the generic word ('Van' for Lake Van)."""
    tr = props.get("name_tr") or props.get("name") or ""
    if tr and not any(w in tr.lower() for w in ("göl", "baraj", "gölet")):
        tr += " Baraj Gölü" if props.get("featurecla") == "Reservoir" else " Gölü"
    return {"tr": tr, "en": props.get("name") or props.get("name_en") or ""}


def to_km(geom, lat0, lon0):
    k = math.cos(math.radians(lat0))
    return affinity.affine_transform(geom, [k * 111.32, 0, 0, 110.574, -lon0 * k * 111.32, -lat0 * 110.574])


def from_km(geom, lat0, lon0):
    k = math.cos(math.radians(lat0))
    return affinity.affine_transform(geom, [1 / (k * 111.32), 0, 0, 1 / 110.574, lon0, lat0])


def shrink(geom, km, tile_box):
    """Clip with a margin, shrink by km in a local metric projection, clip to the tile."""
    minx, miny, maxx, maxy = tile_box.bounds
    part = geom.intersection(box(minx - CLIP_MARGIN, miny - CLIP_MARGIN, maxx + CLIP_MARGIN, maxy + CLIP_MARGIN))
    if part.is_empty:
        return part
    lat0, lon0 = (miny + maxy) / 2, (minx + maxx) / 2
    core = from_km(to_km(part, lat0, lon0).buffer(-km), lat0, lon0)
    return core.intersection(tile_box)


def polygons(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def encode(geom, digits):
    out = []
    for poly in polygons(geom):
        rings = [poly.exterior, *poly.interiors]
        out.append([[round(v, digits) for xy in ring.coords for v in xy] for ring in rings])
    return out


def main():
    ocean = unary_union([shape(f["geometry"]) for f in load("ne_10m_ocean")])
    lakes = [(shape(f["geometry"]).buffer(0), f["properties"]) for f in load("ne_10m_lakes")]
    water = unary_union([ocean, *[g for g, _ in lakes]])
    ice_feats = [(shape(f["geometry"]).buffer(0), f["properties"]) for f in load("ne_10m_glaciated_areas")]
    ice = unary_union([g for g, _ in ice_feats])

    named = []
    for f in load("ne_10m_geography_marine_polys"):
        p = f["properties"]
        if p.get("featurecla") in MARINE and p.get("name"):
            named.append(("sea", shape(f["geometry"]).buffer(0), {"tr": p.get("name_tr") or p["name"], "en": p.get("name_en") or p["name"]}))
    for g, p in lakes:
        if p.get("name"):
            named.append(("lake", g, lake_name(p)))
    for f in load("ne_10m_geography_regions_polys"):
        p = f["properties"]
        kind = REGIONS.get(p.get("FEATURECLA"))
        if kind and p.get("NAME"):
            named.append((kind, shape(f["geometry"]).buffer(0), {"tr": p.get("NAME_TR") or p["NAME"], "en": p.get("NAME_EN") or p["NAME"]}))
    for g, p in ice_feats:
        if p.get("name"):
            named.append(("ice", g, {"tr": p["name"], "en": p["name"]}))

    tree = STRtree([geom for _, geom, _ in named])
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.js"):
        old.unlink()
    keys = []
    for lat in range(LAT_RANGE[0], LAT_RANGE[1], TILE):
        for lon in range(-180, 180, TILE):
            tile = box(lon, lat, lon + TILE, lat + TILE)
            data = {"w": [], "i": [], "n": []}
            if water.intersects(tile):
                data["w"] = encode(shrink(water, WATER_BUFFER_KM, tile).simplify(0.002), 4)
            if ice.intersects(tile):
                data["i"] = encode(shrink(ice, ICE_BUFFER_KM, tile).simplify(0.002), 4)
            for i in sorted(tree.query(tile, predicate="intersects")):
                kind, geom, name = named[i]
                rings = encode(geom.intersection(tile).simplify(0.01), 3)
                if rings:
                    data["n"].append({"k": kind, "name": name, "p": rings})
            if not any(data.values()):
                continue
            key = f"{lat // TILE}_{lon // TILE}"
            datajs.write(OUT / f"{key}.js", f"geo/{key}", data,
                         "Natural Earth (public domain). Built by scripts/build_geo_prior.py.", compact=True)
            keys.append(key)
        print(f"lat {lat}: {len(keys)} tiles so far", flush=True)
    datajs.write(OUT / "index.js", "geo/index", {
        "tile": TILE, "water_buffer_km": WATER_BUFFER_KM, "ice_buffer_km": ICE_BUFFER_KM, "tiles": keys,
        "source": "Natural Earth 1:10m, public domain (naturalearthdata.com)",
    }, "Index of prior-knowledge tiles. Built by scripts/build_geo_prior.py.")
    print(f"wrote {len(keys)} tiles to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

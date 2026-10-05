"""Write scripts/cities.csv by geocoding a fixed list of city names with Photon (komoot).

Coordinates come from the geocoder, never from memory. Every row records what the geocoder
matched (`geocoded_as`) and starts with `reviewed=no`; the project owner checks each row,
fixes it if needed and sets `reviewed=yes`. build_rankings.py skips unreviewed rows unless
it is run with --include-unreviewed.

Usage:  python scripts/geocode_cities.py            # writes scripts/cities.csv
        python scripts/geocode_cities.py --dry-run  # prints rows only
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PHOTON = "https://photon.komoot.io/api/"
OUT = Path(__file__).with_name("cities.csv")
USER_AGENT = "land-change-rankings/1.0 (geocoding a fixed city list)"

# Popular cities around the world, grouped by continent. A fourth value is the query when the
# geocoder only knows the local name. EuroSAT was trained on European scenes, so every city
# outside Europe and Turkey is marked experimental.
CITIES = [
    ("İstanbul", "TR", "europe"), ("London", "GB", "europe"), ("Paris", "FR", "europe"),
    ("Madrid", "ES", "europe"), ("Rome", "IT", "europe", "Roma"),
    ("Tokyo", "JP", "asia", "東京都"), ("Delhi", "IN", "asia"), ("Shanghai", "CN", "asia", "上海市"),
    ("Dubai", "AE", "asia"), ("Singapore", "SG", "asia"),
    ("Cairo", "EG", "africa", "Cairo Egypt"), ("Lagos", "NG", "africa"), ("Nairobi", "KE", "africa"),
    ("New York", "US", "north_america", "New York City"), ("Los Angeles", "US", "north_america"),
    ("Mexico City", "MX", "north_america", "Ciudad de México"),
    ("São Paulo", "BR", "south_america"), ("Buenos Aires", "AR", "south_america"),
    ("Sydney", "AU", "oceania"), ("Melbourne", "AU", "oceania"),
]
EUROPEAN = {"europe"}
# Turkish display names (the ranking shows them in the Turkish interface).
TR_NAMES = {'İstanbul': 'İstanbul', 'London': 'Londra', 'Paris': 'Paris', 'Madrid': 'Madrid', 'Rome': 'Roma', 'Tokyo': 'Tokyo', 'Delhi': 'Delhi', 'Shanghai': 'Şanghay', 'Dubai': 'Dubai', 'Singapore': 'Singapur', 'Cairo': 'Kahire', 'Lagos': 'Lagos', 'Nairobi': 'Nairobi', 'New York': 'New York', 'Los Angeles': 'Los Angeles', 'Mexico City': 'Meksiko', 'São Paulo': 'São Paulo', 'Buenos Aires': 'Buenos Aires', 'Sydney': 'Sidney', 'Melbourne': 'Melbourne'}

PLACE_TYPES = {"city", "town"}


def geocode(query, country):
    """Return the first city/town match in the given country, or None."""
    params = urllib.parse.urlencode({"q": query, "limit": 10, "osm_tag": "place"})
    req = urllib.request.Request(f"{PHOTON}?{params}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        features = json.load(res).get("features", [])
    in_country = [f for f in features if f.get("properties", {}).get("countrycode", "").upper() == country]
    # Prefer a city or town node; some capitals (Oslo) are only tagged as a city-type area.
    for test in (lambda p: p.get("osm_value") in PLACE_TYPES, lambda p: p.get("type") == "city"):
        for f in in_country:
            p = f["properties"]
            if test(p):
                lon, lat = f["geometry"]["coordinates"]
                label = ", ".join(x for x in (p.get("name"), p.get("state"), p.get("country")) if x)
                return round(lat, 5), round(lon, 5), label
    return None


def entries():
    for name, country, continent, *query in CITIES:
        yield name, country, continent, query[0] if query else name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print rows instead of writing the file")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between geocoder requests")
    args = ap.parse_args()

    rows, misses = [], []
    for name, country, continent, query in entries():
        hit = geocode(query, country)
        if hit is None:
            misses.append(f"{name} ({country})")
        else:
            lat, lon, label = hit
            rows.append({"name": name, "name_tr": TR_NAMES.get(name, name), "country": country, "continent": continent, "lat": lat, "lon": lon,
                         "experimental": 0 if continent in EUROPEAN else 1, "geocoded_as": label, "reviewed": "no"})
        time.sleep(args.sleep)

    if args.dry_run:
        for r in rows:
            print(r)
    else:
        with OUT.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} rows to {OUT}")
    if misses:
        print("no city/town match for: " + ", ".join(misses), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

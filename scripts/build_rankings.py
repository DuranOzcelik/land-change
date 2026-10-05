"""Build docs/data/rankings.js: built-up growth since 2016 for the cities in scripts/cities.csv.

The live API is called once per city, one request at a time, with a pause between requests
and retries on server or network errors. Each city's summary is cached in
scripts/.rankings_cache.json, so an interrupted run continues where it stopped.

A city goes to the "unavailable" list instead of the table when either image has more than
5% cloudy or missing area, or the two images are more than 45 days apart in the season.
Before giving up on a city, up to three clearer date pairs from the same season of the same two
years are tried (see datepick.py); the dates used are stored with each city.

Usage:
  python scripts/build_rankings.py                       # reviewed rows only
  python scripts/build_rankings.py --only Ankara,Paris   # a subset (by name)
  python scripts/build_rankings.py --include-unreviewed  # also rows with reviewed=no
"""
import argparse
import csv
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import datajs
import datepick

ROOT = Path(__file__).resolve().parent.parent
CITIES = ROOT / "scripts" / "cities.csv"
CACHE = ROOT / "scripts" / ".rankings_cache.json"
OUT = ROOT / "docs" / "data" / "rankings.js"
API = "https://land-change-api-1098531335280.europe-west1.run.app"

MAX_UNUSABLE = 5.0      # percent of the image that is cloudy or missing
MAX_SEASON_GAP = 45     # days between the two images within the year


def last_summer(today=None):
    """The most recent summer that has already happened (same rule as the web app)."""
    today = today or dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def built_share(layer):
    return next(g["share"] for g in layer["stats"]["groups"] if g["key"] == "built")


def summarize(city, body):
    """Reduce a /api/compare response to the fields the ranking table needs."""
    before, after = body["before"], body["after"]
    b, a = built_share(before), built_share(after)
    return {
        "name": city["name"], "country": city["country"], "continent": city.get("continent", ""),
        "lat": float(city["lat"]), "lon": float(city["lon"]),
        "experimental": city.get("experimental") in ("1", 1, True, "yes"),
        "before_date": before["date"], "after_date": after["date"],
        "built_before": b, "built_after": a, "built_change_pts": round(a - b, 1),
        "built_km2": body["built_km2"],
        "unusable_share": [before["unusable_share"], after["unusable_share"]],
        "season_gap_days": body["season_gap_days"],
    }


def exclusion(row):
    """Why a summarised city cannot be ranked, or None."""
    if max(row["unusable_share"]) > MAX_UNUSABLE:
        return "unusable"
    if row["season_gap_days"] > MAX_SEASON_GAP:
        return "season"
    return None


def split_results(results):
    """Sort cached results into the ranked table and the unavailable list."""
    ranked, unavailable = [], []
    for r in results:
        if "error" in r:
            unavailable.append({k: r.get(k) for k in ("name", "names", "country", "continent", "experimental")} | {"reason": r["error"]})
            continue
        reason = exclusion(r)
        if reason:
            unavailable.append(r | {"reason": reason})
        else:
            ranked.append(r)
    ranked.sort(key=lambda r: r["built_change_pts"], reverse=True)
    return ranked, unavailable


def fetch_compare(city, before, after, api, retries, pause):
    params = urllib.parse.urlencode({
        "lat": f"{float(city['lat']):.5f}", "lon": f"{float(city['lon']):.5f}",
        "size_km": 20, "cell_m": 640, "before": before, "after": after,
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(f"{api}/api/compare?{params}", timeout=320) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"error": "no_imagery"}
            if e.code == 422:
                return {"error": "invalid"}
            if attempt == retries:
                return {"error": "server"}
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                return {"error": "server"}
        time.sleep(pause * (attempt + 1))
    return {"error": "server"}


def retry_with_dates(city, first, before, after, args, cache):
    """Try clearer same-season date pairs; return the first that passes, else the best attempt."""
    key = f"{city['name']}|{city['country']}|{city['lat']}|{city['lon']}|{before}|{after}|picked"
    if key in cache:
        return cache[key]
    attempts = [first]
    try:
        anchors = (first.get("before_date") or datepick.mid_summer(before, city["lat"]),
                   first.get("after_date") or datepick.mid_summer(after, city["lat"]))
        candidates = datepick.pairs(args.api, city["lat"], city["lon"], *anchors)
    except Exception:  # the dates endpoint failing should not stop the run
        candidates = []
    for b, a in candidates:
        body = fetch_compare(city, b, a, args.api, args.retries, args.sleep)
        time.sleep(args.sleep)
        if "error" in body or not datepick.kept_window(body, *anchors):
            continue
        row = summarize(city, body) | {"dates_picked": True}
        attempts.append(row)
        if not exclusion(row):
            break
    usable = [r for r in attempts if "error" not in r]
    best = min(usable, key=lambda r: (exclusion(r) is not None, max(r["unusable_share"]))) if usable else first
    cache[key] = best
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated city names")
    ap.add_argument("--include-unreviewed", action="store_true", help="also use rows with reviewed=no")
    ap.add_argument("--after", type=int, default=last_summer(), help="later year (default: last summer)")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between requests")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--fresh", action="store_true", help="ignore the cache")
    ap.add_argument("--api", default=API)
    args = ap.parse_args()

    with CITIES.open(encoding="utf-8") as fh:
        cities = list(csv.DictReader(fh))
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        cities = [c for c in cities if c["name"] in wanted]
    if not args.include_unreviewed:
        skipped = [c["name"] for c in cities if c.get("reviewed") != "yes"]
        cities = [c for c in cities if c.get("reviewed") == "yes"]
        if skipped:
            print(f"skipping {len(skipped)} unreviewed rows (use --include-unreviewed)", file=sys.stderr)
    if not cities:
        print("no cities to process", file=sys.stderr)
        return 1

    before, after = "2016", str(args.after)
    cache = {} if args.fresh or not CACHE.exists() else json.loads(CACHE.read_text(encoding="utf-8"))
    results = []
    for i, city in enumerate(cities, 1):
        key = f"{city['name']}|{city['country']}|{city['lat']}|{city['lon']}|{before}|{after}"
        if key not in cache:
            body = fetch_compare(city, before, after, args.api, args.retries, args.sleep)
            cache[key] = body if "error" in body else summarize(city, body)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(args.sleep)
        entry = cache[key]
        if "error" in entry or exclusion(entry):
            entry = retry_with_dates(city, entry, before, after, args, cache)
        if "error" in entry:
            entry = {"name": city["name"], "country": city["country"], "continent": city.get("continent", ""),
                     "experimental": city.get("experimental") == "1", "error": entry["error"]}
        results.append(entry)
        print(f"[{i}/{len(cities)}] {city['name']}: {entry.get('error') or entry['built_change_pts']}", flush=True)

    names = {c["name"]: {"tr": c.get("name_tr") or c["name"], "en": c["name"]} for c in cities}
    for r in results:
        r["names"] = names[r["name"]]
    ranked, unavailable = split_results(results)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    datajs.write(OUT, "rankings", {
        "generated": dt.date.today().isoformat(),
        "params": {"before": before, "after": after, "size_km": 20, "cell_m": 640,
                   "max_unusable": MAX_UNUSABLE, "max_season_gap_days": MAX_SEASON_GAP},
        "complete": not args.only,
        "reviewed": all(c.get("reviewed") == "yes" for c in cities),
        "cities": ranked, "unavailable": unavailable,
    }, "Built by scripts/build_rankings.py. Do not edit by hand.")
    print(f"wrote {OUT}: {len(ranked)} ranked, {len(unavailable)} unavailable")
    return 0


if __name__ == "__main__":
    sys.exit(main())

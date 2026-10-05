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

# Turkey: the 81 province centres. Where the centre has its own name, that name is the query.
TURKEY = [
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Amasya", "Ankara", "Antalya", "Artvin",
    "Aydın", "Balıkesir", "Bilecik", "Bingöl", "Bitlis", "Bolu", "Burdur", "Bursa", "Çanakkale",
    "Çankırı", "Çorum", "Denizli", "Diyarbakır", "Edirne", "Elazığ", "Erzincan", "Erzurum",
    "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane", "Hakkari", ("Hatay", "Antakya"), "Isparta",
    "Mersin", "İstanbul", "İzmir", "Kars", "Kastamonu", "Kayseri", "Kırklareli", "Kırşehir",
    ("Kocaeli", "İzmit"), "Konya", "Kütahya", "Malatya", "Manisa", "Kahramanmaraş", "Mardin",
    "Muğla", "Muş", "Nevşehir", "Niğde", "Ordu", "Rize", ("Sakarya", "Adapazarı"), "Samsun",
    "Siirt", "Sinop", "Sivas", "Tekirdağ", "Tokat", "Trabzon", "Tunceli", "Şanlıurfa", "Uşak",
    "Van", "Yozgat", "Zonguldak", "Aksaray", "Bayburt", "Karaman", "Kırıkkale", "Batman",
    "Şırnak", "Bartın", "Ardahan", "Iğdır", "Yalova", "Karabük", "Kilis", "Osmaniye", "Düzce",
]

# Large European cities (capitals and the biggest metropolitan areas). A third value is the
# query when the geocoder only knows the local name.
EUROPE = [
    ("London", "GB"), ("Manchester", "GB"), ("Birmingham", "GB"), ("Paris", "FR"), ("Lyon", "FR"),
    ("Marseille", "FR"), ("Berlin", "DE"), ("Hamburg", "DE"), ("Munich", "DE"), ("Cologne", "DE"),
    ("Madrid", "ES"), ("Barcelona", "ES"), ("Valencia", "ES"), ("Rome", "IT"), ("Milan", "IT"),
    ("Naples", "IT", "Napoli"), ("Amsterdam", "NL"), ("Rotterdam", "NL"), ("Brussels", "BE"), ("Vienna", "AT"),
    ("Zurich", "CH"), ("Prague", "CZ"), ("Warsaw", "PL"), ("Kraków", "PL"), ("Budapest", "HU"),
    ("Bucharest", "RO"), ("Sofia", "BG"), ("Athens", "GR"), ("Thessaloniki", "GR"), ("Belgrade", "RS"),
    ("Zagreb", "HR"), ("Lisbon", "PT", "Lisboa"), ("Porto", "PT"), ("Dublin", "IE"), ("Copenhagen", "DK"),
    ("Stockholm", "SE"), ("Oslo", "NO"), ("Helsinki", "FI"),
]

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
    for item in TURKEY:
        name, query = item if isinstance(item, tuple) else (item, item)
        yield name, "TR", query
    for name, country, *query in EUROPE:
        yield name, country, query[0] if query else name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print rows instead of writing the file")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between geocoder requests")
    args = ap.parse_args()

    rows, misses = [], []
    for name, country, query in entries():
        hit = geocode(query, country)
        if hit is None:
            misses.append(f"{name} ({country})")
        else:
            lat, lon, label = hit
            # EuroSAT was trained on European scenes; Turkey and Europe are the first-release scope.
            rows.append({"name": name, "country": country, "lat": lat, "lon": lon, "experimental": 0,
                         "geocoded_as": label, "reviewed": "no"})
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

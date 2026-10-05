"""Pick clearer image dates when the default image is missing or cloudy.

For a bare year the API uses the cloud-free image closest to mid-summer (20 July in the north,
20 January in the south). In some places (monsoon, haze, cloudy coasts) that image fails the
quality limits. These helpers ask /api/dates what exists and propose date pairs close to the
dates the API would use, clearest first.

Each candidate must lie within MAX_SEASON_GAP days of its anchor: the default date the API
picked, or the mid-summer date when the API found none. Going further is not safe: a clear sky
in another season can mean a different land surface (dust before the monsoon, bare fields after
harvest), which once turned Delhi's 67% built-up into 12%.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request

MAX_SEASON_GAP = 45   # days, as in build_rankings.py


def available(api, lat, lon, year):
    params = urllib.parse.urlencode({"lat": f"{float(lat):.5f}", "lon": f"{float(lon):.5f}", "year": year})
    with urllib.request.urlopen(f"{api}/api/dates?{params}", timeout=120) as res:
        return json.load(res).get("dates", [])


def day_of_year(date):
    """Day of year on a common non-leap calendar, so leap years do not shift the season by a day."""
    d = dt.date.fromisoformat(date)
    return d.replace(year=2001, day=min(d.day, 28) if d.month == 2 else d.day).timetuple().tm_yday


def season_gap(a, b):
    d = abs(day_of_year(a) - day_of_year(b))
    return min(d, 365 - d)


def mid_summer(year, lat):
    """The API's own target for a bare year."""
    return f"{year}-07-20" if float(lat) >= 0 else f"{year}-01-20"


def days_apart(a, b):
    return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days)


def near(api, lat, lon, anchor):
    """Available images within MAX_SEASON_GAP calendar days of the anchor date."""
    a = dt.date.fromisoformat(anchor)
    span = dt.timedelta(days=MAX_SEASON_GAP)
    seen, out = set(), []
    for year in sorted({(a - span).year, a.year, (a + span).year}):
        for d in available(api, lat, lon, year):
            if d["date"] not in seen and days_apart(d["date"], anchor) <= MAX_SEASON_GAP:
                seen.add(d["date"])
                out.append(d)
    return out


def kept_window(body, before_anchor, after_anchor):
    """The API answers a day with the nearest usable image, which can fall outside the window."""
    return (days_apart(body["before"]["date"], before_anchor) <= MAX_SEASON_GAP
            and days_apart(body["after"]["date"], after_anchor) <= MAX_SEASON_GAP)


def pairs(api, lat, lon, before_anchor, after_anchor, limit=3):
    """Up to `limit` (before, after) date pairs near the two anchors, clearest first."""
    after = near(api, lat, lon, after_anchor)
    before = near(api, lat, lon, before_anchor)
    after.sort(key=lambda d: (d["cloud"], days_apart(d["date"], after_anchor)))
    out = []
    for a in after:
        same_season = [b for b in before if season_gap(b["date"], a["date"]) <= MAX_SEASON_GAP]
        if not same_season:
            continue
        b = min(same_season, key=lambda d: (d["cloud"], days_apart(d["date"], before_anchor)))
        out.append((b["date"], a["date"]))
        if len(out) == limit:
            break
    return out

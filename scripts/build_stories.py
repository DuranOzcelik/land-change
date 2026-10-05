"""Save the API response for every example in docs/data/stories.js, so examples open instantly.

For each example this calls /api/compare once and writes
  docs/data/stories/<id>.js          the response (images stay embedded, so the map can show them
                                     even when the page is opened from disk)
  docs/data/stories/<id>-thumb.jpg   a small preview of the later image for the example card
The satellite images are scaled down to at most MAX_PX pixels and re-encoded, which keeps a
20 km example near 1 MB instead of 3-4 MB; the map shows them at a few hundred pixels anyway.
Existing examples are skipped unless --fresh is given. Needs Pillow.

Usage:  python scripts/build_stories.py [--only istanbul-airport,kepez] [--fresh]
"""
import argparse
import base64
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
STORIES = ROOT / "docs" / "data" / "stories.js"
OUT = ROOT / "docs" / "data" / "stories"
API = "https://land-change-api-1098531335280.europe-west1.run.app"


MAX_PX = 1024
QUALITY = 78


def shrink_image(layer):
    """Scale the layer's data-URL image down to MAX_PX and re-encode it (the extent is unchanged)."""
    from io import BytesIO
    from PIL import Image

    header, data = layer["image"]["src"].split(",", 1)
    image = Image.open(BytesIO(base64.b64decode(data))).convert("RGB")
    image.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=QUALITY, optimize=True)
    layer["image"]["src"] = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def write_thumbnail(layer, story_id, size=480):
    """Save a small JPEG preview of the layer's data-URL image for the example card."""
    from io import BytesIO
    from PIL import Image

    header, data = layer["image"]["src"].split(",", 1)
    if "image/jpeg" not in header:
        raise ValueError(f"unexpected image type: {header[:40]}")
    image = Image.open(BytesIO(base64.b64decode(data)))
    image.thumbnail((size, size))
    image.save(OUT / f"{story_id}-thumb.jpg", quality=82)


def fetch(story, api, retries=3, pause=10, before=None, after=None):
    loc, dates = story["location"], story["dates"]
    params = urllib.parse.urlencode({
        "lat": loc["lat"], "lon": loc["lon"], "size_km": loc["size_km"], "cell_m": loc["cell_m"],
        "before": before or dates["before"], "after": after or dates["after"],
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(f"{api}/api/compare?{params}", timeout=320) as res:
                return json.load(res)
        except (urllib.error.URLError, TimeoutError) as err:
            if isinstance(err, urllib.error.HTTPError) and err.code in (404, 422):
                raise
            if attempt == retries:
                raise
            time.sleep(pause * (attempt + 1))


def best_body(story, api, limit=5.0):
    """The default years first; if that fails or is cloudy, clearer same-season pairs (datepick.py)."""
    attempts = []
    try:
        attempts.append(fetch(story, api))
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
    unusable = lambda b: max(b["before"]["unusable_share"], b["after"]["unusable_share"])
    if attempts and unusable(attempts[0]) <= limit:
        return attempts[0]
    loc, dates = story["location"], story["dates"]
    anchors = ((attempts[0]["before"]["date"], attempts[0]["after"]["date"]) if attempts else
               (datepick.mid_summer(dates["before"], loc["lat"]), datepick.mid_summer(dates["after"], loc["lat"])))
    for before, after in datepick.pairs(api, loc["lat"], loc["lon"], *anchors):
        try:
            body = fetch(story, api, before=before, after=after)
        except urllib.error.HTTPError:
            continue
        if not datepick.kept_window(body, *anchors):
            continue
        attempts.append(body)
        if unusable(body) <= limit:
            break
    if not attempts:
        raise RuntimeError("no usable images for these years")
    return min(attempts, key=unusable)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated story ids")
    ap.add_argument("--fresh", action="store_true", help="rebuild stories that already exist")
    ap.add_argument("--api", default=API)
    args = ap.parse_args()

    stories = datajs.read(STORIES)["stories"]
    if args.only:
        wanted = set(args.only.split(","))
        stories = [s for s in stories if s["id"] in wanted]
    OUT.mkdir(parents=True, exist_ok=True)
    failed = 0
    for story in stories:
        target = OUT / f"{story['id']}.js"
        if target.exists() and not args.fresh:
            print(f"{story['id']}: exists, skipped")
            continue
        try:
            body = best_body(story, args.api)
        except Exception as err:  # keep going with the other stories
            print(f"{story['id']}: failed ({err})", file=sys.stderr)
            failed += 1
            continue
        write_thumbnail(body["after"], story["id"])
        shrink_image(body["before"])
        shrink_image(body["after"])
        datajs.write(target, f"stories/{story['id']}", body,
                     "Saved /api/compare response. Built by scripts/build_stories.py.", compact=True)
        print(f"{story['id']}: {body['before']['date']} -> {body['after']['date']}, built +{body['built_km2']} km2")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

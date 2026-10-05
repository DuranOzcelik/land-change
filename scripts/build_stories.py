"""Save the API response for every example in docs/data/stories.js, so examples open instantly.

For each example this calls /api/compare once and writes
  docs/data/stories/<id>.js          the response (images stay embedded, so the map can show them
                                     even when the page is opened from disk)
  docs/data/stories/<id>-thumb.jpg   a small preview of the later image for the example card
Existing examples are skipped unless --fresh is given. Needs Pillow for the thumbnail.

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

ROOT = Path(__file__).resolve().parent.parent
STORIES = ROOT / "docs" / "data" / "stories.js"
OUT = ROOT / "docs" / "data" / "stories"
API = "https://land-change-api-1098531335280.europe-west1.run.app"


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


def fetch(story, api, retries=3, pause=10):
    loc, dates = story["location"], story["dates"]
    params = urllib.parse.urlencode({
        "lat": loc["lat"], "lon": loc["lon"], "size_km": loc["size_km"], "cell_m": loc["cell_m"],
        "before": dates["before"], "after": dates["after"],
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
            body = fetch(story, args.api)
        except Exception as err:  # keep going with the other stories
            print(f"{story['id']}: failed ({err})", file=sys.stderr)
            failed += 1
            continue
        write_thumbnail(body["after"], story["id"])
        datajs.write(target, f"stories/{story['id']}", body,
                     "Saved /api/compare response. Built by scripts/build_stories.py.", compact=True)
        print(f"{story['id']}: {body['before']['date']} -> {body['after']['date']}, built +{body['built_km2']} km2")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

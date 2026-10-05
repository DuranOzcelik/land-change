"""Save the API response for every story in docs/data/stories.json, so stories open instantly.

For each story this calls /api/compare once and writes
  docs/data/stories/<id>.json          the response, with image sources pointing at the files below
  docs/data/stories/<id>-before.jpg    the earlier satellite image
  docs/data/stories/<id>-after.jpg     the later satellite image
Existing stories are skipped unless --fresh is given.

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

ROOT = Path(__file__).resolve().parent.parent
STORIES = ROOT / "docs" / "data" / "stories.json"
OUT = ROOT / "docs" / "data" / "stories"
API = "https://land-change-api-1098531335280.europe-west1.run.app"


def split_image(layer, story_id, side):
    """Write the data-URL image to a JPEG file and point the layer at it (path relative to docs/)."""
    header, data = layer["image"]["src"].split(",", 1)
    if "image/jpeg" not in header:
        raise ValueError(f"unexpected image type: {header[:40]}")
    name = f"{story_id}-{side}.jpg"
    (OUT / name).write_bytes(base64.b64decode(data))
    layer["image"]["src"] = f"data/stories/{name}"


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

    stories = json.loads(STORIES.read_text(encoding="utf-8"))["stories"]
    if args.only:
        wanted = set(args.only.split(","))
        stories = [s for s in stories if s["id"] in wanted]
    OUT.mkdir(parents=True, exist_ok=True)
    failed = 0
    for story in stories:
        target = OUT / f"{story['id']}.json"
        if target.exists() and not args.fresh:
            print(f"{story['id']}: exists, skipped")
            continue
        try:
            body = fetch(story, args.api)
        except Exception as err:  # keep going with the other stories
            print(f"{story['id']}: failed ({err})", file=sys.stderr)
            failed += 1
            continue
        split_image(body["before"], story["id"], "before")
        split_image(body["after"], story["id"], "after")
        target.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{story['id']}: {body['before']['date']} -> {body['after']['date']}, built +{body['built_km2']} km2")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

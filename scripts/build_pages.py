"""Build the static pages that sit next to the app, so search engines find every language.

  docs/tr/index.html   the app page in Turkish: docs/index.html with lang="tr" and the Turkish
                       title, description, link-preview tags and the text that reads without
                       scripts, all taken from the "tr" values in docs/i18n.js
  docs/examples/<id>/index.html, docs/tr/examples/<id>/index.html
                       one script-free page per example in docs/data/stories.js, with the figures
                       the app shows for it (docs/data/stories/<id>.js after the Natural Earth
                       correction), and docs/examples/index.html, docs/tr/examples/index.html
                       listing them all

The output depends only on the files in docs/, so a second run changes nothing. Run it again
after editing docs/index.html, docs/i18n.js or the examples; never edit the generated files by hand.

Usage:  python scripts/build_pages.py
"""
import html
import json
import math
import re
import subprocess
import sys
import urllib.parse
from functools import lru_cache
from pathlib import Path

import datajs

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = "https://greenloss.com"
REPO = "https://github.com/DuranOzcelik/land-change"
LOCALES = {"en": "en_US", "tr": "tr_TR"}
GENERATED = "<!-- Built by scripts/build_pages.py; edit the source files, not this one. -->"


def esc(value):
    """Escape text or an attribute value the way app.js does."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def read_langs():
    """docs/i18n.js holds a JS object literal (bare keys, double-quoted strings); read it as JSON."""
    text = (DOCS / "i18n.js").read_text(encoding="utf-8")
    body = text[text.index("{", text.index("window.LANGS")):text.rindex("}") + 1]
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            j = i + 1
            while body[j] != '"':
                j += 2 if body[j] == "\\" else 1
            out.append(body[i:j + 1])
            i = j + 1
        elif body.startswith("//", i):
            i = body.index("\n", i)
        elif ch.isalpha() or ch == "_":
            j = i
            while body[j].isalnum() or body[j] == "_":
                j += 1
            out.append(json.dumps(body[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return json.loads(re.sub(r",(\s*})", r"\1", "".join(out)))


def num(value, lang, digits=1):
    """Intl.NumberFormat(lang, {maximumFractionDigits}) for the values the pages show."""
    from decimal import ROUND_HALF_UP, Decimal
    q = Decimal(abs(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{q:f}".partition(".")
    frac = frac.rstrip("0")
    group, point = (".", ",") if lang == "tr" else (",", ".")
    whole = f"{int(whole):,}".replace(",", group)
    return ("-" if value < 0 else "") + whole + (point + frac if frac else "")


def page_url(lang, path=""):
    return f"{SITE}/{'tr/' if lang == 'tr' else ''}{path}"


def alternates(path=""):
    """hreflang links for a page that exists in both languages."""
    return "\n".join(f'  <link rel="alternate" hreflang="{code}" href="{page_url(lang, path)}">'
                     for code, lang in (("en", "en"), ("tr", "tr"), ("x-default", "en")))


# ------------------------------------------------------------------ the app page in Turkish

MARKUP = re.compile(r"(<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>|<[^>]+>)", re.S)
NEUTRAL = re.compile(r"^[\s.,:;()]*$|^GitHub$")       # text that reads the same in every language
UNIT = re.compile(r"^(\d+(?:\.\d+)?) (m|km)$")


def set_attr(tag, name, value):
    return re.sub(rf'(\s{name}=")[^"]*(")', lambda m: m.group(1) + esc(value) + m.group(2), tag, count=1)


def translate_app_page(source, langs, lang):
    """docs/index.html (English) -> the same page in `lang`; fails on text it cannot translate."""
    L, en = langs[lang], langs["en"]
    by_text = {}
    for key, value in en.items():
        by_text.setdefault(" ".join(html.unescape(value).split()), set()).add(L.get(key, value))
    parts = MARKUP.split(source)
    missing = []
    for i in range(0, len(parts), 2):
        text = parts[i]
        words = " ".join(html.unescape(text).split())
        if not words:
            continue
        tag = parts[i - 1] if i else ""
        key = re.search(r'data-i18n="(\w+)"', tag)
        unit = UNIT.match(words)
        if "lang-link" in tag:
            continue                                     # named after the other language below
        if key:
            if words != " ".join(html.unescape(en[key.group(1)]).split()):
                sys.exit(f"index.html and i18n.js differ for {key.group(1)}: {words}")
            new = L[key.group(1)]
        elif unit:
            new = f"{num(float(unit.group(1)), lang)} {unit.group(2)}"
        elif NEUTRAL.match(words):
            continue
        elif len(by_text.get(words, ())) == 1:
            new = next(iter(by_text[words]))
        else:
            missing.append(words)
            continue
        lead, trail = re.match(r"^\s*", text).group(), re.search(r"\s*$", text).group()
        parts[i] = lead + esc(new) + trail
    if missing:
        sys.exit("No translation for: " + " | ".join(missing))

    for i in range(1, len(parts), 2):
        tag = parts[i]
        if tag.startswith("<html"):
            tag = set_attr(tag, "lang", lang)
        elif tag.startswith('<meta name="description"'):
            tag = set_attr(tag, "content", L["pageDescription"])
        elif tag.startswith('<link rel="canonical"'):
            tag = set_attr(tag, "href", page_url(lang))
        elif tag.startswith("<meta property=\"og:"):
            prop = re.search(r'property="([^"]+)"', tag).group(1)
            value = {"og:title": L["pageTitle"], "og:description": L["pageDescription"],
                     "og:url": page_url(lang), "og:image:alt": L["ogImageAlt"], "og:site_name": L["title"],
                     "og:locale": LOCALES[lang],
                     "og:locale:alternate": next(v for k, v in LOCALES.items() if k != lang)}.get(prop)
            if value is not None:
                tag = set_attr(tag, "content", value)
        elif tag.startswith('<script type="application/ld+json">'):
            for key, value in (("name", L["title"]), ("url", page_url(lang)),
                               ("description", L["pageDescription"]), ("inLanguage", lang)):
                tag = re.sub(rf'("{key}": )"[^"]*"', lambda m: m.group(1) + json.dumps(value, ensure_ascii=False),
                             tag, count=1)
            json.loads(tag[tag.index(">") + 1:tag.rindex("<")])      # still valid JSON
        elif "lang-link" in tag:
            other = "en" if lang == "tr" else "tr"
            tag = set_attr(set_attr(set_attr(tag, "href", page_url(other).removeprefix(SITE)), "hreflang", other),
                           "lang", other)
            parts[i + 1] = esc(langs[other]["name"])
        elif "data-i18n-label" in tag:
            tag = set_attr(tag, "aria-label", L[re.search(r'data-i18n-label="(\w+)"', tag).group(1)])
        # Asset paths from the site root, so they resolve under /tr/ as well.
        tag = re.sub(r'\b(src|href)="(?![a-z]+:|/|#)([^"]*)"',
                     lambda m: f'{m.group(1)}="/{m.group(2)}"' if m.group(2) != "./" else f'{m.group(1)}="{page_url(lang).removeprefix(SITE)}"',
                     tag)
        parts[i] = tag
    page = "".join(parts)
    page = re.sub(r"<title>[^<]*</title>", f"<title>{esc(L['pageTitle'])}</title>", page, count=1)
    return page.replace("<!doctype html>\n", f"<!doctype html>\n{GENERATED}\n", 1)


# ------------------------------------------------------------------ figures, as the app shows them

# Every report in the app goes through the Natural Earth correction first ("prior knowledge" in
# app.js, on by default): open water far from shore counts as water, glacier cells are left out.
# The pages show the same figures, so these functions repeat app.js step for step.
GROUPS = ["agriculture", "vegetation", "built", "water"]
CONTINENTS = ["europe", "asia", "africa", "north_america", "south_america", "oceania"]
PRIOR_WARN = ["desert", "mountain"]
NOISE_PTS = 1


def js_round(value, digits):
    """Math.round(value * 10 ** digits) / 10 ** digits"""
    x = value * 10 ** digits
    r = math.floor(x)
    return (r + 1 if x - r >= 0.5 else r) / 10 ** digits


class Geo:
    """The Natural Earth tiles in docs/data/geo."""

    def __init__(self):
        self.index = datajs.read(DOCS / "data" / "geo" / "index.js")
        self.tiles = {}

    def tile(self, lat, lon):
        key = f"{math.floor(lat / self.index['tile'])}_{math.floor(lon / self.index['tile'])}"
        if key not in self.index["tiles"]:
            return None
        if key not in self.tiles:
            self.tiles[key] = datajs.read(DOCS / "data" / "geo" / f"{key}.js")
        return self.tiles[key]


def in_rings(rings, x, y):
    inside = False
    for r in rings:
        j = len(r) - 2
        for i in range(0, len(r), 2):
            xi, yi, xj, yj = r[i], r[i + 1], r[j], r[j + 1]
            if (yi > y) != (yj > y) and x < ((xj - xi) * (y - yi)) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def in_any(polys, x, y):
    return any(in_rings(rings, x, y) for rings in polys)


def cell_centres(grid):
    tl, tr, br, bl = grid["corners"]
    out = []
    for r in range(grid["rows"]):
        for c in range(grid["cols"]):
            u, v = (c + 0.5) / grid["cols"], (r + 0.5) / grid["rows"]
            top = [tl[0] + (tr[0] - tl[0]) * u, tl[1] + (tr[1] - tl[1]) * u]
            bottom = [bl[0] + (br[0] - bl[0]) * u, bl[1] + (br[1] - bl[1]) * u]
            out.append([top[0] + (bottom[0] - top[0]) * v, top[1] + (bottom[1] - top[1]) * v])
    return out


def prior_mask(grid, geo):
    centres = cell_centres(grid)
    water, ice = [0] * len(centres), [0] * len(centres)
    for i, (lon, lat) in enumerate(centres):
        tile = geo.tile(lat, lon)
        if not tile:
            continue
        if in_any(tile["w"], lon, lat):
            water[i] = 1
        elif in_any(tile["i"], lon, lat):
            ice[i] = 1
    lon, lat = centres[len(centres) // 2]
    tile = geo.tile(lat, lon)
    regions = [f for f in tile["n"] if f["k"] in PRIOR_WARN and in_any(f["p"], lon, lat)] if tile else []
    return {"water": water, "ice": ice, "regions": regions}


def valid_grid(side):
    return side["grid"].get("valid")


def both_valid(body):
    a, b = valid_grid(body["before"]), valid_grid(body["after"])
    if not a and not b:
        return None
    return [[1 if (not a or a[r][c]) and (not b or b[r][c]) else 0 for c in range(len(row))]
            for r, row in enumerate(a or b)]


def correct_layer(layer, mask, cell_km2, valid):
    """The group statistics of app.js correctLayer (the pages show no class table)."""
    rows, cols = layer["grid"]["rows"], layer["grid"]["cols"]
    d_group = {}

    def move(frm, to=None):
        d_group[frm] = d_group.get(frm, 0) - cell_km2
        if to:
            d_group[to] = d_group.get(to, 0) + cell_km2

    relabelled = removed = 0
    for r in range(rows):
        for c in range(cols):
            i, g = r * cols + c, layer["grid"]["groups"][r][c]
            if valid and valid[r][c] == 0:
                continue
            if mask["ice"][i]:
                move(GROUPS[g])
                removed += 1
            elif mask["water"][i] and GROUPS[g] != "water":
                move(GROUPS[g], "water")
                relabelled += 1
    if not relabelled and not removed:
        return layer, relabelled, removed
    total = layer["stats"]["total_km2"] - removed * cell_km2
    out = [{**it, "km2": max(0, it["km2"] + d_group.get(it["key"], 0))} for it in layer["stats"]["groups"]]
    for key, value in d_group.items():
        if not any(it["key"] == key for it in out) and value > 0:
            out.append({"key": key, "km2": value})
    groups = [{**it, "km2": js_round(it["km2"], 2),
               "share": js_round((it["km2"] / total) * 100, 1) if total > 0 else 0} for it in out]
    return {**layer, "stats": {**layer["stats"], "total_km2": js_round(total, 2), "groups": groups}}, relabelled, removed


def correct_compare(body, mask):
    """app.js correctCompare: returns (body, relabelled, removed)."""
    cell_km2 = body.get("cell_km2") or (body["cell_m"] / 1000) ** 2
    both = both_valid(body)
    a, _, removed_a = correct_layer(body["before"], mask, cell_km2, both)
    b, _, removed_b = correct_layer(body["after"], mask, cell_km2, both)
    if a is body["before"] and b is body["after"]:
        return body, 0, 0
    before, after = body["before"]["grid"]["groups"], body["after"]["grid"]["groups"]
    cols = body["before"]["grid"]["cols"]
    changed_km2, built_km2, relabelled = body["changed_km2"], body["built_km2"], 0
    net_km2, valid_km2 = body.get("net_built_km2"), body.get("valid_km2")
    if net_km2 is not None or valid_km2 is not None:
        for r, row in enumerate(before):
            for c in range(len(row)):
                i = r * cols + c
                if (both and not both[r][c]) or not (mask["water"][i] or mask["ice"][i]):
                    continue
                gb, ga = GROUPS[before[r][c]], GROUPS[after[r][c]]
                if net_km2 is not None:
                    net_km2 -= ((ga == "built") - (gb == "built")) * cell_km2
                if valid_km2 is not None and mask["ice"][i]:
                    valid_km2 -= cell_km2
    moves = {f"{m['from']}>{m['to']}": dict(m) for m in body["transitions"]}
    for r, row in enumerate(body["changed"]):
        for c, v in enumerate(row):
            i = r * cols + c
            gb, ga = GROUPS[before[r][c]], GROUPS[after[r][c]]
            if mask["water"][i] and (gb != "water" or ga != "water"):
                relabelled += 1
            if not v or not (mask["water"][i] or mask["ice"][i]):
                continue
            changed_km2 -= cell_km2
            if ga == "built" and gb != "built":
                built_km2 -= cell_km2
            if f"{gb}>{ga}" in moves:
                moves[f"{gb}>{ga}"]["km2"] -= cell_km2
    transitions = sorted(({**m, "km2": js_round(m["km2"], 2)} for m in moves.values() if m["km2"] > 0.005),
                         key=lambda m: -m["km2"])
    out = {**body, "before": a, "after": b, "transitions": transitions,
           "changed_km2": js_round(max(0, changed_km2), 2), "built_km2": js_round(max(0, built_km2), 2)}
    if net_km2 is not None:
        out["net_built_km2"] = js_round(net_km2, 2)
    if valid_km2 is not None:
        out["valid_km2"] = js_round(max(0, valid_km2), 2)
    return out, relabelled, max(removed_a, removed_b)


def report(raw, geo):
    """What the app shows for a saved example: (body, prior notes) after the correction."""
    mask = prior_mask(raw["before"]["grid"], geo)
    body, relabelled, removed = correct_compare(raw, mask)
    share = lambda layer: next(g["share"] for g in layer["stats"]["groups"] if g["key"] == "built")  # noqa: E731
    # The app's main figure; saved examples predate net_built_km2 and use the built-up share.
    net = body.get("net_built_km2")
    if net is None:
        net = ((share(body["after"]) - share(body["before"])) / 100) * body["before"]["stats"]["total_km2"]
    return body, net, {"relabelled": relabelled, "removed": removed, "regions": mask["regions"]}


# ------------------------------------------------------------------ example pages

def fill(template, **values):
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def pct(value, lang):
    return f"%{num(value, lang)}" if lang == "tr" else f"{num(value, lang)}%"


def signed(value, lang):
    return ("+" if value > 0 else "") + num(value, lang)


def first_fitting(candidates, limit, what):
    for text in candidates:
        if len(text) <= limit:
            return text
    sys.exit(f"No {what} fits in {limit} characters: {candidates[-1]}")


def page_title(L, text):
    """The page's own title, followed by the site name when it still fits in 60 characters."""
    return [f"{text} | {L['title']}", text]


def story_title(story, lang):
    return story["title"].get(lang) or story["title"]["en"]


def map_link(story, lang):
    """The app's share link for this example, as openStory and updateLink write it."""
    loc = story["location"]
    params = [("lat", f"{loc['lat']:.5f}"), ("lon", f"{loc['lon']:.5f}"), ("size_km", loc["size_km"]),
              ("cell_m", loc["cell_m"]), ("before", story["dates"]["before"]), ("after", story["dates"]["after"]),
              ("story", story["id"]), ("mode", "change"), ("q", story_title(story, lang))]
    query = "&".join(f"{k}={urllib.parse.quote_plus(str(v), safe='*')}" for k, v in params)
    return f"{page_url(lang).removeprefix(SITE)}#{query}"


def page_head(lang, L, path, title, description, image, image_alt, image_px, csp, name):
    other = "en" if lang == "tr" else "tr"
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": name, "url": page_url(lang, path),
          "description": description, "inLanguage": lang,
          "isPartOf": {"@type": "WebSite", "name": L["title"], "url": page_url(lang)},
          "image": image}
    ld_text = json.dumps(ld, ensure_ascii=False, indent=2).replace("</", "<\\/").replace("\n", "\n  ")
    return f"""<!doctype html>
{GENERATED}
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <link rel="canonical" href="{page_url(lang, path)}">
{alternates(path)}
  <meta name="description" content="{esc(description)}">
  {csp}
  <meta name="referrer" content="strict-origin-when-cross-origin">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <meta name="theme-color" content="#163300">
  <meta property="og:type" content="website">
  <meta property="og:locale" content="{LOCALES[lang]}">
  <meta property="og:locale:alternate" content="{LOCALES[other]}">
  <meta property="og:site_name" content="{esc(L['title'])}">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(description)}">
  <meta property="og:url" content="{page_url(lang, path)}">
  <meta property="og:image" content="{image}">
  <meta property="og:image:width" content="{image_px[0]}">
  <meta property="og:image:height" content="{image_px[1]}">
  <meta property="og:image:alt" content="{esc(image_alt)}">
  <meta name="twitter:card" content="{'summary' if image_px[0] == image_px[1] else 'summary_large_image'}">
  <script type="application/ld+json">
  {ld_text}
  </script>
  <link rel="stylesheet" href="/styles.css">
</head>"""


def page_body(lang, langs, path, main):
    L, other = langs[lang], "en" if lang == "tr" else "tr"
    home = page_url(lang).removeprefix(SITE)
    return f"""
<body class="page">
  <header class="page-bar">
    <a class="wordmark" href="{home}">{esc(L['title'])}</a>
    <nav aria-label="{esc(L['exploreLabel'])}">
      <a class="pill-button ghost" href="{home}examples/">{esc(L['examplesTitle'])}</a>
      <a class="pill-button ghost" href="{page_url(other, path).removeprefix(SITE)}" hreflang="{other}" lang="{other}">{esc(langs[other]['name'])}</a>
    </nav>
  </header>
  <main class="page-main">
{main}
  </main>
  <footer class="page-foot">
    <p>{esc(L['credit'])}</p>
    <p><a href="{REPO}">{esc(L['sourceCode'])}</a></p>
  </footer>
</body>
</html>
"""


def thumb(story):
    return f"/data/stories/{story['id']}-thumb.jpg"


def example_page(story, raw, langs, lang, stories, csp, geo):
    L = langs[lang]
    body, net, prior = report(raw, geo)
    a, b = body["before"], body["after"]
    place, y1, y2 = story_title(story, lang), a["date"][:4], b["date"][:4]
    built, net_text = num(body["built_km2"], lang), signed(net, lang)
    path = f"examples/{story['id']}/"
    title = first_fitting(page_title(L, fill(L["exTitle"], place=place, built=built, **{"from": y1, "to": y2}))
                          + page_title(L, fill(L["exTitleShort"], place=place, built=built)), 60, "title")
    description = first_fitting([fill(L[k], place=place, built=built, net=net_text, **{"from": y1, "to": y2})
                                 for k in ("exDescription", "exDescriptionShort")], 160, "description")
    heading = fill(L["exHeading"], place=place, **{"from": y1, "to": y2})
    alt = fill(L["exImageAlt"], place=place, date=b["date"])

    rows = []
    for i, g in enumerate(a["stats"]["groups"]):
        after = b["stats"]["groups"][i]
        delta = after["share"] - g["share"]
        trend = ("trendFlat" if abs(js_round(delta * 10, 0) / 10) < NOISE_PTS
                 else "trendUp" if delta > 0 else "trendDown")
        rows.append(f"""          <tr><td><span class="name"><i class="swatch c-{g['key']}"></i>{esc(L[g['key']])}</span>
            <small class="trend">{esc(L[trend])}</small></td>
            <td>{pct(g['share'], lang)}</td><td>{pct(after['share'], lang)}</td><td>{signed(delta, lang)}</td></tr>""")
    moves = "\n".join(f"""          <li><span>{esc(L[m['from']])} → {esc(L[m['to']])}</span><span>{num(m['km2'], lang)} km²</span></li>"""
                      for m in body["transitions"][:5])
    notes = []
    if body.get("uncertain"):
        notes.append(L["uncertain"])
    if body.get("season_warning"):
        notes.append(L["seasonWarning"])
    if any(side["unusable_share"] > 5 for side in (a, b)):
        notes.append(L["unusableWarning"])
    if prior["relabelled"]:
        notes.append(fill(L["priorWater"], n=prior["relabelled"]))
    if prior["removed"]:
        notes.append(fill(L["priorIce"], n=prior["removed"]))
    for region in prior["regions"]:
        notes.append(fill(L[f"priorRegion_{region['k']}"], name=region["name"].get(lang) or region["name"]["en"]))
    text = story["description"].get(lang) or ""
    sources = [f'<a href="{esc(s["url"])}">{esc(s.get("title") or s["url"])}</a>'
               for s in story.get("sources", []) if re.match(r"^https?://", s.get("url", ""))]
    area = num(body.get("valid_km2") if body.get("valid_km2") is not None else a["stats"]["total_km2"], lang, 0)
    limits = [L["limitEstimate"], L["limitScale"], L["experimentalNote"] if story.get("experimental") else L["limitEurope"],
              L["limitSeason"]]
    others = "\n".join(f'        <li><a href="{page_url(lang).removeprefix(SITE)}examples/{s["id"]}/">{esc(story_title(s, lang))}</a></li>'
                       for s in stories if s["id"] != story["id"])
    home = page_url(lang).removeprefix(SITE)

    main = f"""    <article class="page-article">
      <div class="page-head">
        <div class="page-intro">
          <h1>{esc(heading)}</h1>
          <p class="lead">{esc(fill(L['exIntro'], place=place, size=num(story['location']['size_km'], lang, 0), before=a['date'], after=b['date']))}</p>
          {f'<p>{esc(text)}</p>' if text else ''}
          {f'<p class="sources">{esc(L["sources"])}: {", ".join(sources)}</p>' if sources else ''}
          <dl class="page-dates">
            <div><dt>{esc(L['before'])}</dt><dd>{esc(a['date'])}</dd></div>
            <div><dt>{esc(L['after'])}</dt><dd>{esc(b['date'])}</dd></div>
            <div><dt>{esc(L['totalArea'])}</dt><dd>{area} km²</dd></div>
          </dl>
          <p><a class="pill-button primary page-cta" href="{esc(map_link(story, lang))}">{esc(L['exOpenMap'])}</a></p>
        </div>
        <img src="{thumb(story)}" alt="{esc(alt)}" width="480" height="480">
      </div>

      <section>
        <h2>{esc(L['exChangeTitle'])}</h2>
        <p class="figure"><strong>{net_text} km²</strong><span>{esc(L['builtNet'])}</span></p>
        <p>{esc(fill(L['builtConfident'], built=built, changed=num(body['changed_km2'], lang)))}</p>
{"".join(f'        <p class="notice">{esc(n)}</p>' + chr(10) for n in notes)}      </section>

      <section>
        <h2>{esc(L['exSharesTitle'])}</h2>
        <table>
          <caption>{esc(L['exSharesNote'])}</caption>
          <thead><tr><th></th><th>{y1}</th><th>{y2}</th><th>±</th></tr></thead>
          <tbody>
{chr(10).join(rows)}
          </tbody>
        </table>
      </section>

      <section>
        <h2>{esc(L['transitions'])}</h2>
        {f'<ul class="transitions">{chr(10)}{moves}{chr(10)}        </ul>' if moves else f'<p>{esc(L["noChange"])}</p>'}
      </section>

      <section>
        <h2>{esc(L['aboutLimits'])}</h2>
        <ul class="limits-list">
{chr(10).join(f'          <li>{esc(x)}</li>' for x in limits)}
        </ul>
      </section>
    </article>

    <nav class="page-others" aria-label="{esc(L['exOthers'])}">
      <h2>{esc(L['exOthers'])}</h2>
      <ul>
{others}
      </ul>
      <p><a href="{home}examples/">{esc(L['exAll'])}</a></p>
    </nav>"""
    image = SITE + thumb(story)
    head = page_head(lang, L, path, title, description, image, alt, (480, 480), csp, heading)
    return head + page_body(lang, langs, path, main), {"title": title, "description": description,
                                                        "net": net, "built": body["built_km2"], "body": body}


def examples_index(stories, figures, langs, lang, csp):
    L = langs[lang]
    path = "examples/"
    years = sorted({y for f in figures.values() for y in (f["body"]["before"]["date"][:4], f["body"]["after"]["date"][:4])})
    n = len(stories)
    title = first_fitting(page_title(L, fill(L["exIndexTitle"], n=n)), 60, "title")
    description = first_fitting([fill(L["exIndexDescription"], n=n, **{"from": years[0], "to": years[-1]})], 160,
                                "description")
    heading = L["examplesTitle"]
    home = page_url(lang).removeprefix(SITE)

    def card(s):
        f = figures[s["id"]]
        tag = f' <span class="tag">{esc(L["experimental"])}</span>' if s.get("experimental") else ""
        return f"""          <li class="story">
            <a class="story-open" href="{home}examples/{s['id']}/">
              <img src="{thumb(s)}" alt="" loading="lazy" width="320" height="240">
              <strong>{esc(story_title(s, lang))}</strong>
              <span class="meta">{esc(f['body']['before']['date'][:4])} → {esc(f['body']['after']['date'][:4])} &nbsp; {signed(f['net'], lang)} km² {esc(L['builtNet'])}{tag}</span>
            </a>
          </li>"""

    groups = [g for g in ["turkey", *CONTINENTS] if any(s["group"] == g for s in stories)]
    blocks = "\n".join(f"""      <div class="story-group{' wide' if g == 'turkey' else ''}">
        <h2>{esc(L['groupTurkey'] if g == 'turkey' else L[f'continent_{g}'])}</h2>
        <ul class="stories">
{chr(10).join(card(s) for s in stories if s['group'] == g)}
        </ul>
      </div>""" for g in groups)
    main = f"""    <h1>{esc(heading)}</h1>
    <p class="lead">{esc(L['exIndexIntro'])}</p>
    <p class="meta">{esc(L['experimentalNote'])}</p>
    <div class="story-groups">
{blocks}
    </div>"""
    head = page_head(lang, L, path, title, description, f"{SITE}/og-image.png", L["ogImageAlt"], (1200, 630), csp,
                     heading)
    return head + page_body(lang, langs, path, main)


# ------------------------------------------------------------------ sitemap

@lru_cache(maxsize=None)
def content_date(*paths):
    """The day the given source files last changed, from git history: stable across reruns (never
    the build time), and unaffected by edits to unrelated files. A path with no commit yet (added
    in the same change as this run) falls back to today, which the next commit fixes."""
    best = None
    for path in paths:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", str(path)],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        if out:
            best = out if best is None else max(best, out)
    import datetime
    return best or datetime.date.today().isoformat()


def sitemap(stories, langs):
    index_src = (DOCS / "index.html", DOCS / "i18n.js")
    examples_src = tuple((DOCS / "data" / "stories" / f"{s['id']}.js") for s in stories) + (DOCS / "data" / "stories.js",)
    entries = [("", content_date(*index_src))]
    entries += [(f"examples/{s['id']}/", content_date(DOCS / "data" / "stories" / f"{s['id']}.js")) for s in stories]
    entries.append(("examples/", content_date(*examples_src)))

    def xml_url(path, lastmod):
        links = "".join(f'<xhtml:link rel="alternate" hreflang="{code}" href="{esc(page_url(lang, path))}"/>'
                        for code, lang in (("en", "en"), ("tr", "tr"), ("x-default", "en")))
        urls = [f"<url><loc>{esc(page_url('en', path))}</loc><lastmod>{lastmod}</lastmod>{links}</url>"]
        urls.append(f"<url><loc>{esc(page_url('tr', path))}</loc><lastmod>{lastmod}</lastmod>{links}</url>")
        return "\n  ".join(urls)

    urls = "\n  ".join(xml_url(path, lastmod) for path, lastmod in entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">
  {urls}
</urlset>
"""


def write(path, text, written):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    written.append(path)


def main():
    langs = read_langs()
    written = []
    source = (DOCS / "index.html").read_text(encoding="utf-8")
    write(DOCS / "tr" / "index.html", translate_app_page(source, langs, "tr"), written)

    csp = re.search(r'<meta http-equiv="Content-Security-Policy"[^>]*>', source).group()
    stories = datajs.read(DOCS / "data" / "stories.js")["stories"]
    geo = Geo()
    for lang in ("en", "tr"):
        base = DOCS / ("tr" if lang == "tr" else "") / "examples"
        figures = {}
        for story in stories:
            raw = datajs.read(DOCS / "data" / "stories" / f"{story['id']}.js")
            page, figures[story["id"]] = example_page(story, raw, langs, lang, stories, csp, geo)
            write(base / story["id"] / "index.html", page, written)
        write(base / "index.html", examples_index(stories, figures, langs, lang, csp), written)

    write(DOCS / "sitemap.xml", sitemap(stories, langs), written)
    print(f"{len(written)} files written")


if __name__ == "__main__":
    main()

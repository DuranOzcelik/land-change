"""Build the static pages that sit next to the app, so search engines find every language.

  docs/tr/index.html   the app page in Turkish: docs/index.html with lang="tr" and the Turkish
                       title, description, link-preview tags and the text that reads without
                       scripts, all taken from the "tr" values in docs/i18n.js

The output depends only on the files in docs/, so a second run changes nothing. Run it again
after editing docs/index.html or docs/i18n.js; never edit the generated files by hand.

Usage:  python scripts/build_pages.py
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = "https://greenloss.com"
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


def write(path, text, written):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    written.append(path)


def main():
    langs = read_langs()
    written = []
    source = (DOCS / "index.html").read_text(encoding="utf-8")
    write(DOCS / "tr" / "index.html", translate_app_page(source, langs, "tr"), written)
    print(f"{len(written)} files written")


if __name__ == "__main__":
    main()

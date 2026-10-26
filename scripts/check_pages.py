"""Check the HTML pages in docs/ for the basics search engines read.

For every index.html: a title of at most 60 characters, a description of at most 160, exactly
one <h1>, a canonical URL equal to the page's own address, and hreflang links (en, tr,
x-default) that point at pages which exist and link back. Exits with 1 on any problem.

Usage:  python scripts/check_pages.py
"""
import html
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SITE = "https://greenloss.com"


def address(path):
    rel = path.parent.relative_to(DOCS).as_posix()
    return f"{SITE}/" if rel == "." else f"{SITE}/{rel}/"


def read(path):
    text = path.read_text(encoding="utf-8")
    meta = lambda pattern: (m := re.search(pattern, text)) and html.unescape(m.group(1))  # noqa: E731
    return {
        "title": meta(r"<title>([^<]*)</title>"),
        "description": meta(r'<meta name="description" content="([^"]*)"'),
        "canonical": meta(r'<link rel="canonical" href="([^"]*)"'),
        "h1": len(re.findall(r"<h1[\s>]", text)),
        "hreflang": dict(re.findall(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)"', text)),
    }


def main():
    pages = {address(p): read(p) for p in sorted(DOCS.rglob("index.html"))}
    problems = []
    for url, page in pages.items():
        def bad(message):
            problems.append(f"{url}: {message}")
        if not page["title"] or len(page["title"]) > 60:
            bad(f"title has {len(page['title'] or '')} characters")
        if not page["description"] or len(page["description"]) > 160:
            bad(f"description has {len(page['description'] or '')} characters")
        if page["h1"] != 1:
            bad(f"{page['h1']} <h1> elements")
        if page["canonical"] != url:
            bad(f"canonical is {page['canonical']}")
        if set(page["hreflang"]) != {"en", "tr", "x-default"}:
            bad(f"hreflang set is {sorted(page['hreflang'])}")
        if url not in page["hreflang"].values():
            bad("hreflang does not list the page itself")
        for code, target in page["hreflang"].items():
            if target not in pages:
                bad(f"hreflang {code} points to a missing page {target}")
            elif pages[target]["hreflang"] != page["hreflang"]:
                bad(f"hreflang {code} page {target} does not link back the same way")
    for line in problems:
        print(line)
    print(f"{len(pages)} pages checked, {len(problems)} problems")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()

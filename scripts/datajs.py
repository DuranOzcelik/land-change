"""Read and write the web app's data files.

Data files are small scripts that register one object on window.LC_DATA, for example
    (window.LC_DATA = window.LC_DATA || {})["rankings"] = {...};
so the page can load them with a <script> tag. That also works when index.html is opened
straight from disk (file://), where fetch() of local files is blocked.
"""
import json
from pathlib import Path

PREFIX = "(window.LC_DATA = window.LC_DATA || {})"


def write(path, name, obj, header="", compact=False):
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compact else None,
                      indent=None if compact else 1)
    comment = "".join(f"// {line}\n" for line in header.splitlines())
    Path(path).write_text(f"{comment}{PREFIX}[{json.dumps(name)}] = {body};\n", encoding="utf-8", newline="\n")


def read(path):
    text = Path(path).read_text(encoding="utf-8")
    start = text.index(PREFIX)
    start = text.index("=", text.index("]", start)) + 1
    end = text.rindex(";")
    return json.loads(text[start:end])

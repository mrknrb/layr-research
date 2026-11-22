"""The plugin's core knows no method: no code path, label, default or example in it may be about one kind of
assay. What belongs to one method lives in its method file (backend/lab/methods/), its example (backend/lab/examples/)
or a toolbox add-on (backend/toolbox/<add-on>/). Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
BACKEND = PLUGIN / "backend"
sys.path.insert(0, str(BACKEND))
# Words that belong to the gas chromatography assay (its method, its example and its toolbox add-on).
WORDS = re.compile(r"gc[-_ ]?assay|gaml|chromatogram|peak|compound|biomass|injection|retention|Chromeleon|\bGC\b",
                   re.IGNORECASE)
ALLOWED = [re.compile(r"compound statement", re.IGNORECASE)]  # Python's own term (an if, a for, a with ...)
TEXT = {".py", ".js", ".json", ".txt", ".md", ".html", ".css"}


def core_files() -> list[Path]:
    """Everything under backend/ except the methods, the examples and the toolbox's add-ons; and the panel files
    and the manifest that must stay general."""
    from toolbox import registry

    addons = [BACKEND / "toolbox" / m.split(".", 1)[1] for m in registry.addons()]
    skip = [BACKEND / "lab" / "methods", BACKEND / "lab" / "examples", *addons]
    files = [p for p in BACKEND.rglob("*") if p.is_file() and p.suffix in TEXT and "__pycache__" not in p.parts
             and not any(p.is_relative_to(s) for s in skip)]
    return files + [PLUGIN / "panels" / "pages.js", PLUGIN / "panels" / "guide.js", PLUGIN / "layr.json"]


def test_the_core_names_no_method():
    found = []
    for path in core_files():
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            text = line
            for allowed in ALLOWED:
                text = allowed.sub("", text)
            for m in WORDS.finditer(text):
                found.append(f"{path.relative_to(PLUGIN).as_posix()}:{number}: {m.group(0)!r} in {line.strip()[:100]}")
    assert not found, "Method-specific words in the core:\n" + "\n".join(found)


def test_the_guard_sees_the_files_it_must():
    names = {p.relative_to(PLUGIN).as_posix() for p in core_files()}
    assert {"backend/server.py", "backend/lab/tools.py", "backend/toolbox/__init__.py", "panels/pages.js",
            "layr.json"} <= names
    assert not any(n.startswith(("backend/lab/methods/", "backend/lab/examples/", "backend/toolbox/gc/"))
                   for n in names)

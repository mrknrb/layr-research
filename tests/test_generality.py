"""The plugin's core knows no method: no code path, label, default or example in it may be about one kind of
assay. What belongs to one method lives in its method file (backend/lab/methods/), its example (backend/lab/examples/)
or a toolbox add-on (backend/toolbox/<add-on>/). The guard reads the backend, every panel file, the manifest, the
README (but its sections about the GC method, add-on and example) and the panel test bench's helpers.
Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
BACKEND = PLUGIN / "backend"
PANELS = PLUGIN / "panels"
BENCH = PLUGIN / "tests" / "panel"
sys.path.insert(0, str(BACKEND))
# Words that belong to the gas chromatography assay (its method, its example and its toolbox add-on).
WORDS = re.compile(r"gc[-_ ]?assay|gaml|chromatogram|peak|compound|biomass|injection|retention|Chromeleon|\bGC\b",
                   re.IGNORECASE)
ALLOWED = [re.compile(r"compound statement", re.IGNORECASE)]  # Python's own term (an if, a for, a with ...)
TEXT = {".py", ".js", ".mjs", ".json", ".txt", ".md", ".html", ".css"}
# README sections that describe one method, its add-on or its example: their heading names it.
METHOD_SECTION = re.compile(r"\bGC\b")
# A script of the panel test bench that makes the GC example by its id uses it on purpose (its settings, its pages).
USES_GC_EXAMPLE = re.compile(r"""which:\s*["']gc["']""")


def core_files() -> list[Path]:
    """Everything under backend/ except the methods, the examples and the toolbox's add-ons; every panel file but
    the vendored libraries; the manifest; the panel test bench but the scripts that make the GC example."""
    from toolbox import registry

    addons = [BACKEND / "toolbox" / m.split(".", 1)[1] for m in registry.addons()]
    skip = [BACKEND / "lab" / "methods", BACKEND / "lab" / "examples", *addons]
    files = [p for p in BACKEND.rglob("*") if p.is_file() and p.suffix in TEXT and "__pycache__" not in p.parts
             and not any(p.is_relative_to(s) for s in skip)]
    files += [p for p in PANELS.rglob("*") if p.is_file() and p.suffix in TEXT
              and not p.is_relative_to(PANELS / "vendor")]
    files += [p for p in BENCH.glob("*") if p.is_file() and p.suffix in TEXT
              and not USES_GC_EXAMPLE.search(p.read_text("utf-8"))]
    return files + [PLUGIN / "layr.json"]


def readme_lines() -> list[tuple[int, str]]:
    """The README's lines outside the sections about the GC method, add-on and example (numbered as in the file)."""
    out = []
    skipping = False
    for number, line in enumerate((PLUGIN / "README.md").read_text("utf-8").splitlines(), 1):
        if line.startswith("## "):
            skipping = bool(METHOD_SECTION.search(line))
        if not skipping:
            out.append((number, line))
    return out


def hits(name: str, lines) -> list[str]:
    found = []
    for number, line in lines:
        text = line
        for allowed in ALLOWED:
            text = allowed.sub("", text)
        for m in WORDS.finditer(text):
            found.append(f"{name}:{number}: {m.group(0)!r} in {line.strip()[:100]}")
    return found


def test_the_core_names_no_method():
    found = []
    for path in core_files():
        found += hits(path.relative_to(PLUGIN).as_posix(), enumerate(path.read_text("utf-8").splitlines(), 1))
    found += hits("README.md", readme_lines())
    assert not found, "Method-specific words in the core:\n" + "\n".join(found)


def test_the_guard_sees_the_files_it_must():
    names = {p.relative_to(PLUGIN).as_posix() for p in core_files()}
    assert {"backend/server.py", "backend/lab/tools.py", "backend/toolbox/__init__.py", "panels/pages.js",
            "panels/lab.js", "panels/flow.js", "panels/lab.css", "panels/lab.html", "panels/guide.js",
            "panels/rounds.js", "panels/dev-editor.html", "layr.json", "tests/panel/harness.py",
            "tests/panel/lib.mjs", "tests/panel/README.md", "tests/panel/rounds.e2e.mjs"} <= names
    assert not any(n.startswith(("backend/lab/methods/", "backend/lab/examples/", "backend/toolbox/gc/",
                                 "panels/vendor/")) for n in names)
    # the scripts that make the GC example on purpose are left out, and only those
    assert "tests/panel/tabs.e2e.mjs" not in names and "tests/panel/smoke.e2e.mjs" not in names
    # the README is read, but its GC sections are not
    sections = [line for _, line in readme_lines() if line.startswith("## ")]
    assert sections and not any(METHOD_SECTION.search(s) for s in sections)
    assert any(METHOD_SECTION.search(line) for line in (PLUGIN / "README.md").read_text("utf-8").splitlines()
               if line.startswith("## "))

"""Example experiments, to try the plugin without real data (Try an example in the panel, the `lab_sample` tool).

Each example is one module in this folder; the plugin lists whatever is here. A module holds:

    ID = "survey"                       # what `lab_sample(which=...)` takes (the file's name)
    TITLE = "A survey"                  # the menu's words
    DESCRIPTION = "Scale scores, reliability, group means, a regression"
    METHOD = "survey-scores"            # the built-in method it runs
    EXPERIMENT = "Example: made-up attitude survey"   # the new experiment's title
    ORDER = 1                           # its place in the menu
    SETTINGS = {...}                    # the experiment's settings (the rest: the method's defaults)
    CORRECTIONS = [...]                 # corrections it starts with: {"kind", field: value, ..., "reason"}

    def files(folder: Path) -> dict[str, Path]:
        '''Write the made-up raw files into `folder`: {the recipe's input id: the file}.'''

An example whose method reads results of other experiments (an input marked `[result]`, lab/sources.py) makes those
experiments first, runs them, and picks their tables (`files` may then be left out):

    SOURCES = {"spring": {"title": "Example: survey in spring", "method": "survey-scores", "settings": {...},
                          "files": spring_files}, ...}          # each made and run first, in this order
    PICKS = {"results": [{"source": "spring", "result": "scores"}, ...]}   # the new experiment's sources
"""

from __future__ import annotations

import importlib
import pkgutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from ..errors import LabError

FOLDER = Path(__file__).resolve().parent


def modules() -> list[ModuleType]:
    """Every example module, in menu order."""
    found = []
    for m in pkgutil.iter_modules([str(FOLDER)]):
        if m.name.startswith("_"):
            continue
        found.append(importlib.import_module(f"{__name__}.{m.name}"))
    return sorted(found, key=lambda x: (getattr(x, "ORDER", 99), x.ID))


def listing() -> list[dict[str, Any]]:
    """[{id, title, description, method}] for the menu."""
    return [{"id": m.ID, "title": m.TITLE, "description": m.DESCRIPTION, "method": m.METHOD} for m in modules()]


def get(which: str) -> ModuleType:
    for m in modules():
        if m.ID == which:
            return m
    known = ", ".join(m.ID for m in modules()) or "none"
    raise LabError(f"There is no example '{which}'. Examples: {known}.")


def stored_files(store, files: ModuleType | Callable | None) -> dict[str, dict[str, Any]]:
    """An example's raw files written and put into the store (the same file twice is stored once): {input: ref}.
    `files`: the example module (its `files`), or a function writing them (an experiment of its SOURCES)."""
    write = getattr(files, "files", None) if isinstance(files, ModuleType) else files
    if write is None:
        return {}
    with tempfile.TemporaryDirectory() as d:
        return {name: store.add_file(path, move=True) for name, path in write(Path(d)).items()}

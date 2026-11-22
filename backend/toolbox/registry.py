"""What the toolbox and its add-ons announce about themselves, so the plugin can offer them without knowing them.

- **Add-ons** are the toolbox's sub-packages (`toolbox/<name>/`, imported as `toolbox.<name>`): ready-made functions
  for one kind of data or instrument. A method imports them like the rest (`from toolbox.<name> import ...`). The
  general toolbox never imports an add-on itself; `addons()` finds them by their folders and `load_addons()` imports
  them when something asks for what they register.
- **Readers** (`register_reader`): functions that read a raw file into something a method works with. The flow
  offers them when a raw file is added: which kinds of file each reads, the name its result usually gets and the
  name of the recipe's input.
- **Curve readers** (`register_curve_reader`): for a kind of file the plugin can't read as a table, a function that
  gives the file's curves, one per item, for the review cards of an experiment's pages:

      def curves(path, settings) -> list[dict]:
          # each curve: {"item": the item's key (a number or a text), "name": optional, "x": [...], "y": [...]}

  `settings` is the experiment's settings (its values, with the method's defaults for the rest), so a reader can
  find what it needs to know about the file (which channel, which sheet...).
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_readers: dict[str, dict[str, Any]] = {}  # function name -> what the flow offers
_curve_readers: dict[str, Callable] = {}  # ".ext" -> fn(path, settings) -> [curves]
_loaded = False


def addons() -> list[str]:
    """The add-ons' module names (`toolbox.<folder>`), in alphabetical order."""
    folder = Path(__file__).resolve().parent
    return [f"toolbox.{m.name}" for m in sorted(pkgutil.iter_modules([str(folder)]), key=lambda m: m.name)
            if m.ispkg and not m.name.startswith("_")]


def load_addons() -> list[Any]:
    """Import every add-on (once), so what they register is known. An add-on that can't be imported is skipped."""
    global _loaded
    # Not under _lock: an add-on registers itself (taking _lock) while it is imported, and Python's import lock
    # already keeps two threads from importing it twice; holding both could deadlock two threads.
    modules = []
    for name in addons():
        try:
            modules.append(importlib.import_module(name))
        except Exception:  # an add-on is optional: a broken one must not break the toolbox
            continue
    _loaded = True
    return modules


def _ext(extension: str) -> str:
    e = str(extension).strip().lower()
    return e if e.startswith(".") else "." + e


def register_reader(function: Callable, label: str, extensions: list[str], out: str, param: str) -> Callable:
    """Announce a reader of raw files: its label for people ("A table (CSV, Excel...)"), the file extensions it
    reads, the usual name of its result (`table`) and of the recipe's input it reads (`table_file`)."""
    if not param.endswith("_file"):
        raise ValueError(f"A reader's input name ends in _file ({param!r} doesn't).")
    with _lock:
        _readers[function.__name__] = {"function": function.__name__, "module": _public_module(function),
                                       "label": label, "extensions": [_ext(e) for e in extensions], "out": out,
                                       "param": param}
    return function


def readers() -> list[dict[str, Any]]:
    """Every reader of the toolbox and its add-ons: [{function, module, label, extensions, out, param}]."""
    load_addons()
    with _lock:
        general = [r for r in _readers.values() if r["module"] == "toolbox"]
        rest = sorted((r for r in _readers.values() if r["module"] != "toolbox"), key=lambda r: r["module"])
        return [dict(r) for r in general + rest]


def register_curve_reader(extension: str, function: Callable) -> Callable:
    """Announce how the curves of a kind of file are read (see the module's docstring for `function`)."""
    with _lock:
        _curve_readers[_ext(extension)] = function
    return function


def curve_reader(extension: str) -> Callable | None:
    """The curve reader for a file extension (".xyz"), or None; the add-ons are imported the first time."""
    e = _ext(extension)
    if e not in _curve_readers and not _loaded:
        load_addons()
    with _lock:
        return _curve_readers.get(e)


def _public_module(obj: Any) -> str:
    """`toolbox` for a function of the general toolbox, `toolbox.<add-on>` for one of an add-on."""
    parts = (getattr(obj, "__module__", "") or "").split(".")
    if len(parts) >= 2 and parts[0] == "toolbox" and f"toolbox.{parts[1]}" in addons():
        return f"toolbox.{parts[1]}"
    return "toolbox"


def module_of(obj: Any) -> str:
    """Where a method imports a toolbox object from: `toolbox`, or `toolbox.<add-on>`."""
    return _public_module(obj)


def everything() -> dict[str, tuple[str, Any]]:
    """Every public name of the toolbox and its add-ons: {name: (module to import it from, the object)}. A name the
    general toolbox has comes from there."""
    import toolbox

    out: dict[str, tuple[str, Any]] = {n: ("toolbox", getattr(toolbox, n)) for n in toolbox.__all__}
    for mod in load_addons():
        for n in getattr(mod, "__all__", []):
            if n not in out and hasattr(mod, n):
                out[n] = (mod.__name__, getattr(mod, n))
    return out


def find(name: str, module: str | None = None) -> tuple[str, Any] | None:
    """(module, object) for a public toolbox name, or None. With `module`, only from that module."""
    import toolbox

    if module in (None, "toolbox") and name in toolbox.__all__:
        return "toolbox", getattr(toolbox, name)
    if module == "toolbox":
        return None
    for mod in load_addons():
        if (module is None or mod.__name__ == module) and name in getattr(mod, "__all__", []) and hasattr(mod, name):
            return mod.__name__, getattr(mod, name)
    return None


def groups() -> list[dict[str, Any]]:
    """The toolbox and each add-on as a group: [{module, title, sentence, functions: [names]}]."""
    import toolbox

    def head(mod) -> tuple[str, str]:
        doc = (mod.__doc__ or "").strip()
        first = doc.splitlines()[0].strip() if doc else mod.__name__
        title, _, sentence = first.partition(": ")
        return (title, sentence) if sentence else (first, "")

    out = []
    for mod in [toolbox, *load_addons()]:
        title, sentence = head(mod)
        out.append({"module": mod.__name__, "title": title, "sentence": sentence,
                    "functions": list(getattr(mod, "__all__", []))})
    return out

"""Recording every call of a method's functions while it runs: name, inputs, result, time, error.

The method file has no decorators and imports nothing from layr. `load` runs the file as a module, then replaces
each of its top-level functions (and the toolbox functions it imported) by a wrapper that records the call and calls
the original. The wrappers are put back into the module under the same names, so calls between the method's own
functions go through them too.

- Calls made directly by the recipe are the steps (their tables are saved with the run).
- Calls nested deeper are the formulas and helpers used inside a step.
- A function called many times keeps the first `limit` calls in full and a count of all of them.
- Whole columns (a formula used on a column) keep their index and values, so a row can be looked up.
"""

from __future__ import annotations

import functools
import inspect
import math
import time
import types
from collections import Counter
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np
import pandas as pd

LIMIT = 1000  # calls kept in full per function
MAX_VALUES = 5000  # values kept of a column or array
MAX_ITEMS = 30  # items kept of a list or dict


def summarize(v: Any, depth: int = 0) -> Any:
    """A small JSON description of a value: numbers as they are, columns with index and values, tables by shape."""
    if v is None or isinstance(v, (bool, str)):
        return v if not isinstance(v, str) or len(v) <= 500 else v[:500] + "…"
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if math.isnan(f):
            return {"kind": "number", "value": None, "text": "NaN"}
        if math.isinf(f):
            return {"kind": "number", "value": None, "text": "inf" if f > 0 else "-inf"}
        return f
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, pd.DataFrame):
        return {"kind": "table", "rows": int(len(v)), "columns": [str(c) for c in v.columns]}
    if isinstance(v, pd.Series):
        n = len(v)
        part = v.iloc[:MAX_VALUES]
        return {"kind": "column", "name": None if v.name is None else str(v.name), "length": n,
                "index": [_plain(i) for i in part.index], "values": [_plain(x) for x in part.to_numpy()],
                "truncated": n > MAX_VALUES}
    if isinstance(v, np.ndarray):
        flat = v.ravel()
        return {"kind": "array", "shape": list(v.shape), "values": [_plain(x) for x in flat[:MAX_VALUES]],
                "truncated": flat.size > MAX_VALUES}
    if isinstance(v, tuple) and hasattr(v, "_fields"):
        return {"kind": "record", "type": type(v).__name__,
                "fields": {f: summarize(getattr(v, f), depth + 1) for f in v._fields}}
    if isinstance(v, dict):
        items = list(v.items())[:MAX_ITEMS]
        if depth > 2:
            return {"kind": "dict", "length": len(v)}
        return {"kind": "dict", "length": len(v), "items": {str(k): summarize(x, depth + 1) for k, x in items}}
    if isinstance(v, (list, tuple, set)):
        seq = list(v)
        if depth > 2:
            return {"kind": "list", "length": len(seq)}
        return {"kind": "list", "length": len(seq), "items": [summarize(x, depth + 1) for x in seq[:MAX_ITEMS]]}
    if isinstance(v, (bytes, bytearray)):
        return {"kind": "bytes", "size": len(v)}
    if type(v).__name__ == "Figure":
        return {"kind": "figure"}
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    text = repr(v)
    return {"kind": "object", "type": type(v).__name__, "text": text if len(text) <= 120 else text[:120] + "…"}


def _plain(x: Any) -> Any:
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if x is None or isinstance(x, (int, str)):
        return x
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    return str(x)


class Recorder:
    def __init__(self, limit: int = LIMIT, on_step: Callable[[dict, Any], None] | None = None):
        self.limit = limit
        self.calls: list[dict[str, Any]] = []
        self.counts: Counter = Counter()
        self.kept: Counter = Counter()
        self.stack: list[int] = []
        self.next_id = 0
        self.recipe_call: int | None = None
        self.on_step = on_step  # (call record, result) for each step, when it returns
        self.on_step_start: Callable[[str], None] | None = None

    def wrap(self, name: str, fn: Callable, kind: str, recipe: str | None) -> Callable:
        sig = None
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            pass
        rec = self

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            call_id = rec.next_id
            rec.next_id += 1
            parent = rec.stack[-1] if rec.stack else None
            depth = len(rec.stack)
            if name == recipe and rec.recipe_call is None:
                rec.recipe_call = call_id
            is_step = parent is not None and parent == rec.recipe_call
            if is_step and rec.on_step_start:
                rec.on_step_start(name)
            rec.counts[name] += 1
            keep = rec.kept[name] < rec.limit or is_step or name == recipe
            entry: dict[str, Any] | None = None
            if keep:
                rec.kept[name] += 1
                entry = {"id": call_id, "function": name, "kind": kind, "parent": parent, "depth": depth,
                         "step": is_step, "n": rec.counts[name]}
                entry["inputs"] = rec._inputs(sig, args, kwargs, skip_tables=is_step or name == recipe)
                if is_step or name == recipe:
                    entry["_raw_inputs"] = rec._raw(sig, args, kwargs)
            rec.stack.append(call_id)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                if entry is not None:
                    entry["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                    entry["error"] = f"{type(e).__name__}: {e}"
                    entry.pop("_raw_inputs", None)
                    rec.calls.append(entry)
                raise
            finally:
                rec.stack.pop()
            if entry is not None:
                entry["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                entry["result"] = summarize(result)
                raw = entry.pop("_raw_inputs", None)
                rec.calls.append(entry)
                if is_step and rec.on_step:
                    rec.on_step(entry, result, raw)
            return result

        wrapper.__layr_original__ = fn
        return wrapper

    @staticmethod
    def _bind(sig, args, kwargs) -> dict[str, Any]:
        if sig is None:
            return {f"arg{i}": a for i, a in enumerate(args)} | dict(kwargs)
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            return {f"arg{i}": a for i, a in enumerate(args)} | dict(kwargs)

    def _inputs(self, sig, args, kwargs, skip_tables: bool) -> dict[str, Any]:
        return {k: summarize(v) for k, v in self._bind(sig, args, kwargs).items()}

    def _raw(self, sig, args, kwargs) -> dict[str, Any]:
        return self._bind(sig, args, kwargs)

    def report(self) -> dict[str, Any]:
        calls = sorted(self.calls, key=lambda c: c["id"])
        return {"calls": calls, "counts": dict(self.counts), "kept": dict(self.kept), "limit": self.limit,
                "recipe_call": self.recipe_call}


def load(source: str, filename: str | Path, recorder: Recorder | None, kinds: dict[str, str], recipe: str,
         module_name: str = "method") -> types.ModuleType:
    """Run the method file as a module; with a recorder, wrap its functions (and imported toolbox functions)."""
    module = types.ModuleType(module_name)
    module.__file__ = str(filename)
    code = compile(source, str(filename), "exec")
    exec(code, module.__dict__)
    if recorder is None:
        return module
    for name, value in list(module.__dict__.items()):
        if not inspect.isfunction(value):
            continue
        own = value.__code__.co_filename == str(filename)
        from_toolbox = (value.__module__ or "").startswith("toolbox")
        if own:
            module.__dict__[name] = recorder.wrap(name, value, kinds.get(name, "helper"), recipe)
        elif from_toolbox:
            module.__dict__[name] = recorder.wrap(name, value, "toolbox", recipe)
    return module

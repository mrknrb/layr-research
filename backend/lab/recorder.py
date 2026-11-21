"""Recording what a method does while it runs: every call of its functions, and every value inside them.

The method file has no decorators and imports nothing from layr. `load` runs the file as a module, then replaces
each of its top-level functions (and the toolbox functions it imported) by a wrapper that records the call and calls
the original. The wrappers are put back into the module under the same names, so calls between the method's own
functions go through them too.

- Calls made directly by the recipe are the steps (their tables are saved with the run).
- Calls nested deeper are the formulas and helpers used inside a step.
- A function called many times keeps the first `limit` calls in full and a count of all of them.
- Whole columns (a formula used on a column) keep their index and values, so a row can be looked up.

**Inside the functions** (`Inside`): with `sys.monitoring` (Python 3.12), line events are switched on for the code
of the method file's own functions only, so pandas and the toolbox run at full speed. For every kept call, each
statement that gives names a value (`x = …`, `x += …`, `table[col] = …`, `rows.append(…)`, a loop's variables,
`with … as x`) keeps those names' values after it ran. A statement written over several lines is one statement. In
a loop the first 3 and the last 3 rounds are kept, and the round that failed; all rounds are counted. A function
called many times keeps the values inside its first `INSIDE_LIMIT` calls (a step always); its later calls run at
full speed (their calls are still in calls.json, as far as `LIMIT`). The record is `inside.json` in the run's folder
(its shape: `Inside.report`), joined to `calls.json` by call id.

**Remembered steps** (`StepCache`): a step whose code and inputs are the same as in an earlier run isn't run again:
its result and its whole record (calls, values inside, tables) come back from the cache.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import inspect
import math
import os
import pickle
import shutil
import sys
import time
import types
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np
import pandas as pd

LIMIT = 1000  # calls kept in full per function
MAX_VALUES = 5000  # values kept of a column or array
MAX_ITEMS = 30  # items kept of a list or dict
ROUNDS = 3  # rounds of a loop kept at its start, and as many at its end
INSIDE_LIMIT = 50  # calls per function whose values inside are recorded (the first ones; steps always)


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
                "index": _plain_list(part.index.to_numpy()), "values": _plain_list(part.to_numpy()),
                "truncated": n > MAX_VALUES}
    if isinstance(v, np.ndarray):
        flat = v.ravel()
        return {"kind": "array", "shape": list(v.shape), "values": _plain_list(flat[:MAX_VALUES]),
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
    if callable(v) and (inspect.isfunction(v) or inspect.isbuiltin(v) or hasattr(v, "__layr_original__")):
        return {"kind": "function", "name": getattr(v, "__name__", None) or repr(v)}
    text = repr(v)
    return {"kind": "object", "type": type(v).__name__, "text": text if len(text) <= 120 else text[:120] + "…"}


def _copy(v: Any) -> Any:
    """A copy of a record made of dicts and lists (the summaries)."""
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


def _plain_list(a: np.ndarray) -> list:
    """`[_plain(x) for x in a]`, at NumPy's speed for numbers and booleans."""
    kind = a.dtype.kind
    if kind in "biu":
        return a.tolist()
    if kind == "f":
        bad = ~np.isfinite(a)
        if not bad.any():
            return a.tolist()
        out = a.astype(object)
        out[bad] = None
        return out.tolist()
    return [_plain(x) for x in a]


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


# ---------------------------------------------------------------------------------------------------- calls


class Recorder:
    def __init__(self, limit: int = LIMIT, on_step: Callable[[dict, Any, dict | None], None] | None = None,
                 inside: Inside | None = None, cache: StepCache | None = None):
        self.limit = limit
        self.calls: list[dict[str, Any]] = []
        self.counts: Counter = Counter()
        self.kept: Counter = Counter()
        self.inside_kept: Counter = Counter()  # calls whose values inside are recorded, per function
        self.inside_limit = INSIDE_LIMIT
        self.stack: list[int] = []
        self.next_id = 0
        self.recipe_call: int | None = None
        self.on_step = on_step  # (call record, result, its inputs) for each step, when it returns
        self.on_step_start: Callable[[str], None] | None = None
        self.inside = inside
        self.cache = cache
        self.patched: list[tuple[types.ModuleType, str, Any]] = []  # (module, name, original) to put back
        # The remembered step's folder while `on_step` runs for a step that was remembered now or came from the
        # cache (the worker keeps or takes its files there), else None.
        self.step_origin: Path | None = None

    def restore(self) -> None:
        """Put back the functions wrapped inside the toolbox (the calculation process runs many methods)."""
        while self.patched:
            module, name, original = self.patched.pop()
            module.__dict__[name] = original

    def wrap(self, name: str, fn: Callable, kind: str, recipe: str | None) -> Callable:
        sig = None
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            pass
        rec = self
        code = getattr(fn, "__code__", None) if kind != "toolbox" else None
        simple = _simple_params(sig)

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
            raw: dict[str, Any] | None = None
            if keep:
                rec.kept[name] += 1
                entry = {"id": call_id, "function": name, "kind": kind, "parent": parent, "depth": depth,
                         "step": is_step, "n": rec.counts[name]}
                bound = rec._bind(sig, args, kwargs, simple)
                entry["inputs"] = {k: summarize(v) for k, v in bound.items()}
                if is_step or name == recipe:
                    raw = bound
            remember = None  # (key, the inputs' fingerprints): the step can be remembered
            refused = False
            rec.step_origin = None
            if is_step and rec.cache is not None and raw is not None:
                t0 = time.perf_counter()
                remember = rec.cache.key(name, kind, raw)
                if remember is not None and rec.cache.read:
                    hit = rec.cache.get(remember[0])
                    if hit is not None and "result" in hit:
                        try:
                            rec._replay(entry, hit, call_id, depth, time.perf_counter() - t0)
                        except Exception as e:  # files of the remembered step are gone: run it
                            print(f"couldn't use a remembered step ({name}): {e}", file=sys.stderr)
                        else:
                            if rec.on_step:
                                rec.on_step(entry, hit["result"], raw)
                            return hit["result"]
                    elif hit is not None and hit.get("why"):
                        entry["cache"] = hit["why"]
                        refused = True
            mark = (len(rec.calls), Counter(rec.counts), Counter(rec.kept), Counter(rec.inside_kept)) \
                if remember is not None else None
            if is_step and rec.inside is not None:
                rec.inside.values.touched = set()  # the tables its record will refer to
            if rec.inside is not None and code is not None:
                if entry is not None and (is_step or name == recipe or rec.inside_kept[name] < rec.inside_limit):
                    rec.inside_kept[name] += 1
                    rec.inside.watch_again(code)
                    rec.inside.expect(code, call_id, is_step)
                else:
                    rec.inside.unwatch(code)  # no more calls of it keep their values: let it run at full speed
            rec.stack.append(call_id)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                if rec.inside is not None and entry is not None and code is not None:
                    rec.inside.failed(call_id, e)
                if entry is not None:
                    entry["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                    entry["error"] = f"{type(e).__name__}: {e}"
                    rec.calls.append(entry)
                if is_step and rec.cache is not None:
                    rec.cache.distrust()  # it may have changed a value before it failed
                raise
            finally:
                rec.stack.pop()
                if rec.inside is not None:
                    rec.inside.pending = None
            if entry is not None:
                entry["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                entry["result"] = summarize(result)
                rec.calls.append(entry)
                if is_step and rec.cache is not None:
                    if remember is None:
                        rec.cache.distrust()  # nobody checked what it did to its inputs
                    elif rec.cache.changed_inputs(raw, remember[1]):
                        if not refused:
                            entry["cache"] = "not remembered: it changes a value it was given"
                            rec.cache.refuse(remember[0], entry["cache"])
                    elif not refused and mark is not None:
                        rec._remember(remember[0], entry, result, call_id, depth, mark)
                if is_step and rec.on_step:
                    rec.on_step(entry, result, raw)
            return result

        wrapper.__layr_original__ = fn
        return wrapper

    # --------------------------------------------------------------- remembered steps

    def _remember(self, key: str, entry: dict, result: Any, call_id: int, depth: int,
                  mark: tuple[int, Counter, Counter, Counter]) -> None:
        """After a step ran: keep its result and record for the next run with the same code and inputs."""
        start, counts, kept, inside_kept = mark
        span = self.next_id - call_id
        sub = []
        for c in self.calls[start:-1]:
            c2 = dict(c)
            c2["id"] = c["id"] - call_id
            c2["parent"] = None if c.get("parent") is None else c["parent"] - call_id
            c2["depth"] = c.get("depth", 0) - depth
            c2["n"] = c.get("n", 0) - counts[c["function"]]
            sub.append(c2)
        me = {k: v for k, v in entry.items() if not k.startswith("_")}
        inside = {}
        if self.inside is not None:
            names = {c["id"]: c["function"] for c in self.calls[start:]}
            for cid in range(call_id, call_id + span):
                r = self.inside.records.get(cid)
                if r is not None:  # lines counted from the function's first line: the function may move
                    inside[cid - call_id] = self.inside.moved(r, names.get(cid), -1)
        values = self.inside.values if self.inside else None
        stored = self.cache.put(key, {"result": result, "entry": me, "calls": sub, "inside": inside, "span": span,
                                      "counts": dict(self.counts - counts), "kept": dict(self.kept - kept),
                                      "inside_kept": dict(self.inside_kept - inside_kept),
                                      "ms": entry.get("ms")}, values, values.touched if values is not None else None)
        if stored != "stored":
            entry["cache"] = stored
        else:
            self.step_origin = self.cache.folder_of(key)

    def _replay(self, entry: dict, hit: dict, call_id: int, depth: int, seconds: float) -> None:
        """A remembered step: its record comes back under this run's call ids."""
        if self.inside is not None and hit.get("files"):
            self.inside.values.adopt(hit["folder"], hit["files"])
        counts = Counter(self.counts)  # the step itself is counted already, as when it was remembered
        for c in hit["calls"]:
            c2 = dict(c)
            c2["id"] = c["id"] + call_id
            c2["parent"] = None if c.get("parent") is None else c["parent"] + call_id
            c2["depth"] = c.get("depth", 0) + depth
            c2["n"] = c.get("n", 0) + counts[c["function"]]
            self.calls.append(c2)
        stored = hit["entry"]
        for k in ("inputs", "result", "error"):
            if k in stored:
                entry[k] = _copy(stored[k])  # the run adds file names to these: the remembered ones stay as they are
        entry["ms"] = round(seconds * 1000, 2)
        entry["ms_fresh"] = stored.get("ms")
        entry["cached"] = True
        self.calls.append(entry)
        self.counts.update(hit["counts"])  # the calls inside it
        self.kept.update(hit["kept"])
        self.inside_kept.update(hit.get("inside_kept") or {})
        if self.inside is not None:
            names = {c["id"]: c["function"] for c in hit["calls"]}
            names[0] = entry["function"]
            for rel, r in hit["inside"].items():
                r2 = self.inside.moved(r, names.get(int(rel)), 1)
                r2["call"] = int(rel) + call_id
                self.inside.records[int(rel) + call_id] = r2
        self.next_id = max(self.next_id, call_id + hit["span"])
        self.step_origin = hit["folder"]

    # --------------------------------------------------------------- inputs

    @staticmethod
    def _bind(sig, args, kwargs, simple: tuple | None = None) -> dict[str, Any]:
        """The call's arguments by parameter name, defaults included (`simple`: `_simple_params(sig)`)."""
        if simple is not None and len(args) <= len(simple[0]):
            names, defaults = simple
            rest = names[len(args):]
            if all(k in rest for k in kwargs):
                out = dict(zip(names, args))
                for name in rest:
                    if name in kwargs:
                        out[name] = kwargs[name]
                    elif name in defaults:
                        out[name] = defaults[name]
                    else:
                        break  # a missing input: inspect's way below, as before
                else:
                    return out
        if sig is None:
            return {f"arg{i}": a for i, a in enumerate(args)} | dict(kwargs)
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            return {f"arg{i}": a for i, a in enumerate(args)} | dict(kwargs)

    def report(self) -> dict[str, Any]:
        calls = sorted(self.calls, key=lambda c: c["id"])
        return {"calls": calls, "counts": dict(self.counts), "kept": dict(self.kept), "limit": self.limit,
                "recipe_call": self.recipe_call}


def _simple_params(sig: inspect.Signature | None) -> tuple[tuple[str, ...], dict[str, Any]] | None:
    """A signature made only of plain parameters (no *args, **kwargs, keyword-only or positional-only ones): their
    names and defaults, to bind arguments without inspect's slower way. None otherwise."""
    if sig is None:
        return None
    names, defaults = [], {}
    for p in sig.parameters.values():
        if p.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
            return None
        names.append(p.name)
        if p.default is not inspect.Parameter.empty:
            defaults[p.name] = p.default
    return tuple(names), defaults


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
            if recorder.inside is not None:
                recorder.inside.watch(value.__code__)
                if name == recipe:
                    recorder.inside.recipe = value.__code__
            module.__dict__[name] = recorder.wrap(name, value, kinds.get(name, "helper"), recipe)
        elif from_toolbox:
            module.__dict__[name] = recorder.wrap(name, value, "toolbox", recipe)
    # The ready-made steps (toolbox.blocks) call other toolbox functions (fit_line ...) from their own module: wrap
    # them there too, so those calls are recorded. `Recorder.restore` puts the originals back.
    blocks = sys.modules.get("toolbox.blocks")
    if blocks is not None:
        for name, value in list(blocks.__dict__.items()):
            if inspect.isfunction(value) and (value.__module__ or "").startswith("toolbox") \
                    and value.__module__ != blocks.__name__ and not name.startswith("_"):
                recorder.patched.append((blocks, name, value))
                blocks.__dict__[name] = recorder.wrap(name, value, "toolbox", recipe)
    return module


# ---------------------------------------------------------------------------------------------------- statements


@dataclass(frozen=True)
class Statement:
    start: int
    end: int
    names: tuple[str, ...]
    body: tuple[int, int] | None = None  # a for loop: its body's lines (a round is counted when the body follows)


def targets_of(stmt: ast.stmt) -> list[str]:
    """The names a statement gives a value to (also `table[column] = …`, `rows.append(…)`, the loop variable)."""
    out: list[str] = []

    def base(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                base(e)
        elif isinstance(t, ast.Starred):
            base(t.value)
        elif isinstance(t, (ast.Subscript, ast.Attribute)):
            base(t.value)

    if isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            base(t)
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        if not isinstance(stmt, ast.AnnAssign) or stmt.value is not None:
            base(stmt.target)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        base(stmt.target)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                base(item.optional_vars)
    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and \
            isinstance(stmt.value.func, ast.Attribute):
        base(stmt.value.func.value)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        out += [(a.asname or a.name).split(".")[0] for a in stmt.names]
    return list(dict.fromkeys(out))


_BODIES = ("body", "orelse", "finalbody", "handlers", "cases")


def statement_map(fn: ast.FunctionDef) -> dict[int, Statement]:
    """Every line of a function's body → the statement it belongs to (for a compound statement: its header)."""
    by_line: dict[int, Statement] = {}

    def add(s: Statement) -> None:
        for line in range(s.start, s.end + 1):
            old = by_line.get(line)
            if old is None or (not old.names and s.names):
                by_line[line] = s

    def visit(body: list) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ExceptHandler):
                end = stmt.body[0].lineno - 1 if stmt.body else stmt.lineno
                add(Statement(stmt.lineno, max(stmt.lineno, end), (stmt.name,) if stmt.name else ()))
                visit(stmt.body)
                continue
            if isinstance(stmt, ast.match_case):
                visit(stmt.body)
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = stmt.decorator_list[0].lineno if stmt.decorator_list else stmt.lineno
                inner = stmt.body[0].lineno - 1 if stmt.body else stmt.lineno
                add(Statement(first, max(stmt.lineno, inner), (stmt.name,)))
                continue
            children = [c for f in _BODIES for c in (getattr(stmt, f, None) or [])]
            if children:
                header_end = max(stmt.lineno, min(c.lineno for c in children) - 1)
                body = None
                if isinstance(stmt, (ast.For, ast.AsyncFor)) and stmt.body:
                    body = (stmt.body[0].lineno, max((c.end_lineno or c.lineno) for c in stmt.body))
                add(Statement(stmt.lineno, header_end, tuple(targets_of(stmt)), body))
                for f in _BODIES:
                    visit(getattr(stmt, f, None) or [])
            else:
                add(Statement(stmt.lineno, stmt.end_lineno or stmt.lineno, tuple(targets_of(stmt))))

    visit(fn.body)
    return by_line


def guards(fn: ast.FunctionDef) -> list[dict[str, Any]]:
    """Every `if` that only stops with a message: its lines, its condition and the message when it is written out."""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and not node.orelse and node.body and all(isinstance(s, ast.Raise)
                                                                              for s in node.body):
            message = None
            exc = node.body[0].exc
            if isinstance(exc, ast.Call) and exc.args:
                a = exc.args[0]
                message = a.value if isinstance(a, ast.Constant) else ast.unparse(a)
            out.append({"line": node.lineno, "end_line": node.end_lineno or node.lineno,
                        "condition": ast.unparse(node.test), "message": message})
    return sorted(out, key=lambda g: g["line"])


def docstring_lines(fn: ast.FunctionDef) -> tuple[int, int] | None:
    """The first and last line of a function's docstring, when it has one."""
    first = fn.body[0] if fn.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first.lineno, first.end_lineno or first.lineno
    return None


# ---------------------------------------------------------------------------------------------------- inside


@functools.lru_cache(maxsize=8)
def _statement_maps(source: str) -> tuple[dict[int, dict[int, Statement]], dict[str, int]]:
    """Every function's statements by line (keyed by its first line), and each function's `def` line. Kept for
    the next runs of the same source (the calculation process stays warm); never changed once made."""
    maps: dict[int, dict[int, Statement]] = {}
    firsts: dict[str, int] = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            maps[first] = statement_map(node)
            firsts[node.name] = node.lineno
    return maps, firsts


_MISSING = object()
_HOLD_CELLS = 200_000  # a table or array held for a later round is copied when it is at most this big


class _Held:
    """A value kept for a late round of a loop, summarized only if it stays among the last rounds."""

    __slots__ = ("kind", "value", "length", "head", "last")

    def __init__(self, kind: str, value: Any = None, length: int = 0, head: list | None = None, last: Any = _MISSING):
        self.kind, self.value, self.length, self.head, self.last = kind, value, length, head, last


def _hold(v: Any) -> Any:
    if isinstance(v, (pd.DataFrame, pd.Series, np.ndarray)):
        size = v.size if not isinstance(v, pd.DataFrame) else v.shape[0] * max(1, v.shape[1])
        return _Held("copy", v.copy() if size <= _HOLD_CELLS else v)
    if isinstance(v, list):
        return _Held("list", length=len(v), head=v[:3], last=v[-1] if len(v) > 3 else _MISSING)
    if isinstance(v, dict):
        items = list(v.items()) if len(v) <= 10 else list(v.items())[:10]
        last = next(reversed(v.items())) if len(v) > 10 else _MISSING
        return _Held("dict", length=len(v), head=items, last=last)
    if isinstance(v, set):
        return _Held("copy", set(v) if len(v) <= 1000 else v)
    return v


class Inside:
    """What every statement of the method's own functions gave its names, per kept call (see the module's
    docstring). Uses `sys.monitoring` tool id 4 (or the next free one) while a run lasts."""

    def __init__(self, source: str, filename: str, values: Any, rounds: int = ROUNDS):
        self.filename = filename
        self.values = values  # lab.values.ValueStore
        self.rounds = rounds
        # function's first line → its statements; function → the line of its `def`
        self.maps, self.firsts = _statement_maps(source)
        self.codes: dict[types.CodeType, dict[int, Statement]] = {}
        self.records: dict[int, dict[str, Any]] = {}
        self.frames: dict[int, dict[str, Any]] = {}  # id(frame) → the state of that call
        self.by_call: dict[int, dict[str, Any]] = {}
        self.pending: tuple[types.CodeType, int, bool] | None = None
        self.tool: int | None = None
        self.errors = 0
        self.recipe: types.CodeType | None = None  # the recipe's code (see `known`)
        self.unwatched: set[types.CodeType] = set()  # functions past their limit: no events
        self.calls_limit = INSIDE_LIMIT
        # Fingerprints known already (a StepCache that trusts its memory, see StepCache.remember_fingerprints):
        # used for the values of the recipe's own statements (only steps can change those) and learnt from what a
        # step returns.
        self.known: Any = None

    # --------------------------------------------------------------- switching on and off

    def watch(self, code: types.CodeType) -> None:
        m = self.maps.get(code.co_firstlineno)
        if m is not None:
            self.codes[code] = m

    def unwatch(self, code: types.CodeType) -> None:
        if self.tool is not None and code in self.codes and code not in self.unwatched:
            self.unwatched.add(code)
            try:
                sys.monitoring.set_local_events(self.tool, code, 0)
            except ValueError:
                pass

    def watch_again(self, code: types.CodeType) -> None:
        """A function no longer watched is called as a step (always recorded): watch it again."""
        if code in self.unwatched and self.tool is not None:
            self.unwatched.discard(code)
            ev = sys.monitoring.events
            try:
                sys.monitoring.set_local_events(self.tool, code, ev.PY_START | ev.LINE | ev.PY_RETURN)
            except ValueError:
                pass

    def start(self) -> bool:
        mon = getattr(sys, "monitoring", None)
        if mon is None:
            return False
        for tool in (4, 3, 5):
            if mon.get_tool(tool) is None:
                mon.use_tool_id(tool, "layr research")
                self.tool = tool
                break
        if self.tool is None:
            return False
        ev = mon.events
        mon.register_callback(self.tool, ev.PY_START, self._start)
        mon.register_callback(self.tool, ev.LINE, self._line)
        mon.register_callback(self.tool, ev.PY_RETURN, self._return)
        for code in self.codes:
            mon.set_local_events(self.tool, code, ev.PY_START | ev.LINE | ev.PY_RETURN)
        return True

    def stop(self) -> None:
        if self.tool is None:
            return
        mon = sys.monitoring
        for code in self.codes:
            try:
                mon.set_local_events(self.tool, code, 0)
            except ValueError:
                pass
        ev = mon.events
        for e in (ev.PY_START, ev.LINE, ev.PY_RETURN):
            mon.register_callback(self.tool, e, None)
        mon.free_tool_id(self.tool)
        self.tool = None
        self.frames.clear()
        self.by_call.clear()

    def expect(self, code: types.CodeType, call_id: int, step: bool = False) -> None:
        """The wrapper is about to call this code as call `call_id` (a step of the recipe or not)."""
        if code in self.codes:
            self.pending = (code, call_id, step)

    def _summary(self, st: dict, v: Any, learn: bool = False) -> Any:
        """A value's summary; in the recipe's own frame (or for what a step returns) with fingerprints known."""
        values = self.values
        if self.known is None or not (st["recipe"] or learn):
            return values.summary(v)
        values.lookup = self.known.known if st["recipe"] else None
        values.learn = self.known.learn
        try:
            return values.summary(v)
        finally:
            values.lookup = values.learn = None

    # --------------------------------------------------------------- events

    def _start(self, code, offset):
        p = self.pending
        if p is None or p[0] is not code:
            return  # a call nobody keeps (past the limit, or not through the wrapper)
        self.pending = None
        try:
            frame = sys._getframe(1)
            st = {"call": p[1], "code": code, "map": self.codes[code], "frame": id(frame), "prev": None,
                  "lines": {}, "args": {}, "step": p[2], "recipe": code is self.recipe}
            self.frames[id(frame)] = st
            self.by_call[p[1]] = st
            st["args"] = {k: self._summary(st, v) for k, v in frame.f_locals.items()}
        except Exception:
            self.errors += 1

    def _line(self, code, line):
        # An error raised here would surface in her method: every callback catches everything.
        try:
            frame = sys._getframe(1)
            st = self.frames.get(id(frame))
            if st is None:
                return
            prev = st["prev"]
            st["prev"] = line
            if prev is None or prev == line:
                return
            s = st["map"].get(prev)
            if s is None or s.start <= line <= s.end:
                return
            if s.body is not None and not s.body[0] <= line <= s.body[1]:
                return  # the loop is over: its header ran once more to find that out
            self._note(st, s, frame)
        except Exception:
            self.errors += 1

    def _return(self, code, offset, retval):
        try:
            frame = sys._getframe(1)
            st = self.frames.pop(id(frame), None)
            if st is None:
                return
            self.by_call.pop(st["call"], None)
            s = st["map"].get(st["prev"])
            if s is not None and s.body is None:
                self._note(st, s, frame)
            st["returned"] = self._summary(st, retval, learn=st["step"])
            self._finish(st)
        except Exception:
            self.errors += 1

    def failed(self, call_id: int, exc: BaseException) -> None:
        """The call ended with an error: its statement in progress is the failed round."""
        st = self.by_call.pop(call_id, None)
        if st is None:
            return
        self.frames.pop(st["frame"], None)
        frame = None
        tb = exc.__traceback__
        while tb is not None:
            if id(tb.tb_frame) == st["frame"] and tb.tb_frame.f_code is st["code"]:
                frame = tb.tb_frame
            tb = tb.tb_next
        s = st["map"].get(st["prev"])
        if s is not None:
            self._note(st, s, frame, failed=True)
        st["error"] = f"{type(exc).__name__}: {exc}"
        try:
            self._finish(st)
        except Exception:
            self.errors += 1

    # --------------------------------------------------------------- values

    def _note(self, st: dict, s: Statement, frame, failed: bool = False) -> None:
        rec = st["lines"].get(s.start)
        if rec is None:
            rec = st["lines"][s.start] = {"n": 0, "first": [], "last": deque(maxlen=self.rounds), "s": s}
        rec["n"] += 1
        k = rec["n"]
        if not s.names and not failed:
            return
        try:
            loc = frame.f_locals if frame is not None else {}
            got = {n: loc[n] for n in s.names if n in loc}
            if failed:
                rec["failed"] = {"round": k, "values": {n: self._summary(st, v) for n, v in got.items()}}
            elif k <= self.rounds:
                rec["first"].append({"round": k, "values": {n: self._summary(st, v) for n, v in got.items()}})
            else:
                rec["last"].append((k, {n: _hold(v) for n, v in got.items()}))
        except Exception:  # a value that can't be described must never stop her method
            self.errors += 1

    def moved(self, record: dict, function: str | None, sign: int) -> dict:
        """A call's record with its statements' lines counted from the function's `def` line (sign -1, to be
        remembered) or back in this file (sign 1)."""
        first = self.firsts.get(function or "")
        if not first or not record.get("lines"):
            return dict(record)
        shift = sign * first
        lines = [{**st, "line": st["line"] + shift, "end_line": st["end_line"] + shift} for st in record["lines"]]
        return {**record, "lines": lines}

    def _held(self, v: Any) -> Any:
        if not isinstance(v, _Held):
            return self.values.summary(v)
        if v.kind == "copy":
            return self.values.summary(v.value)
        if v.kind == "list":
            out = {"kind": "list", "length": v.length, "items": [self.values.summary(x, 1) for x in v.head]}
            if v.last is not _MISSING:
                out["last"] = self.values.summary(v.last, 1)
            return out
        out = {"kind": "dict", "length": v.length, "items": {str(k): self.values.summary(x, 1) for k, x in v.head}}
        if v.last is not _MISSING:
            out["last"] = {"key": str(v.last[0]), "value": self.values.summary(v.last[1], 1)}
        return out

    def _finish(self, st: dict) -> None:
        lines = []
        for start in sorted(st["lines"]):
            rec = st["lines"][start]
            s = rec["s"]
            if not s.names and "failed" not in rec:
                continue
            rounds = list(rec["first"])
            for k, held in rec["last"]:
                if k > self.rounds:
                    try:
                        rounds.append({"round": k, "values": {n: self._held(v) for n, v in held.items()}})
                    except Exception:
                        self.errors += 1
            if "failed" in rec:
                rounds.append({**rec["failed"], "failed": True})
            item = {"line": s.start, "end_line": s.end, "names": list(s.names), "n": rec["n"], "rounds": rounds}
            if s.body is not None:
                item["loop"] = True
            lines.append(item)
        out = {"call": st["call"], "args": st.get("args") or {}, "lines": lines}
        if "returned" in st:
            out["returned"] = st["returned"]
        if "error" in st:
            out["error"] = st["error"]
        self.records[st["call"]] = out

    def report(self, calls: list[dict[str, Any]]) -> dict[str, Any]:
        """The record written as inside.json:

        {"version": 1, "rounds": 3, "calls_limit": 50 (calls per function with their values inside: the first
         ones; steps always), "values": {dir, tables, bytes, budget, sampled, left_out},
         "calls": {"<call id>": {"call", "function", "args": {name: summary}, "lines": [{"line", "end_line",
                   "names", "n": rounds counted, "loop"?, "rounds": [{"round", "values": {name: summary},
                   "failed"?}]}], "returned"?: summary, "error"?: text}}}
        """
        names = {c["id"]: c["function"] for c in calls}
        out = {}
        for cid in sorted(self.records):
            r = self.records[cid]
            out[str(cid)] = {"call": cid, "function": names.get(cid), **{k: v for k, v in r.items() if k != "call"}}
        return {"version": 1, "rounds": self.rounds, "calls_limit": self.calls_limit, "values": self.values.report(),
                "calls": out, "problems": self.errors}


# ---------------------------------------------------------------------------------------------------- the cache


CACHE_FORMAT = 3
CACHE_MAX = 2_000_000_000  # bytes on disk for remembered steps; the oldest are dropped past it
ENTRY_MAX = 50_000_000  # a step whose result is bigger isn't remembered (reading it would cost more than running)
WARM_MAX = 400_000_000  # bytes of remembered steps' results kept in memory between runs
TOUCH_EVERY = 3600  # seconds between two marks that a remembered step was used (for pruning the oldest)


class _Warm:
    """What the calculation process keeps between runs (it stays warm for the next one): the fingerprints of the
    objects the steps were given, and the remembered steps it read. Both are only used while nothing but checked
    steps can have changed an object (StepCache.trusted); anything else forgets them."""

    def __init__(self) -> None:
        self.fps: dict[int, tuple[Any, str]] = {}  # id → (the object, its fingerprint)
        self.used: set[int] = set()  # ids looked up or learnt in this run
        self.hits: dict[str, tuple[dict, tuple, int]] = {}  # key → (entry, entry.pkl's size and time, bytes)
        self.bytes = 0
        self.touched: dict[str, float] = {}

    def forget(self) -> None:
        self.fps.clear()
        self.used.clear()
        self.hits.clear()
        self.bytes = 0

    def keep(self, key: str, hit: dict, sig: tuple) -> None:
        from .values import approx_size

        size = approx_size(hit.get("result"))
        if size > WARM_MAX // 4:
            return
        old = self.hits.pop(key, None)
        if old is not None:
            self.bytes -= old[2]
        while self.hits and self.bytes + size > WARM_MAX:
            self.bytes -= self.hits.pop(next(iter(self.hits)))[2]
        self.hits[key] = (hit, sig, size)
        self.bytes += size


WARM = _Warm()


@functools.lru_cache(maxsize=1)
def toolbox_fingerprint() -> str:
    """A fingerprint of the toolbox's code (every .py file of it)."""
    import toolbox

    h = hashlib.sha256()
    folder = Path(toolbox.__file__).resolve().parent
    for p in sorted(folder.rglob("*.py")):
        h.update(p.relative_to(folder).as_posix().encode() + b"\x00" + p.read_bytes() + b"\x00")
    return h.hexdigest()[:24]


@functools.lru_cache(maxsize=1)
def _versions() -> str:
    import matplotlib
    import scipy

    return f"{sys.version} numpy {np.__version__} pandas {pd.__version__} scipy {scipy.__version__} " \
           f"matplotlib {matplotlib.__version__}"


def _segment(lines: list[str], node: ast.AST) -> str:
    """A top-level statement's source text (as ast.get_source_segment, without splitting the file each time)."""
    first, last = node.lineno - 1, node.end_lineno - 1
    if first == last:
        return lines[first].encode("utf-8")[node.col_offset:node.end_col_offset].decode("utf-8", "replace")
    head = lines[first].encode("utf-8")[node.col_offset:].decode("utf-8", "replace")
    tail = lines[last].encode("utf-8")[:node.end_col_offset].decode("utf-8", "replace")
    return "".join([head, *lines[first + 1:last], tail])


def _defined_names(node: ast.stmt) -> list[str] | None:
    """The names a module-level statement defines, or None when it isn't a plain definition (it then counts for
    every step)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        if any(a.name == "*" for a in node.names):
            return None
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return targets_of(node) or None
    return None


@functools.lru_cache(maxsize=8)
def _parse_for_cache(source: str) -> tuple[dict[str, str], dict[str, str], dict[str, frozenset], str]:
    """For the keys of remembered steps: the method's functions' sources; every module-level definition's source
    by the name it defines (an import by each name it brings in, so adding a name to an import line touches only
    that name); the names each definition uses; and the module-level code that is no definition."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    functions: dict[str, str] = {}
    defs: dict[str, list[str]] = {}
    uses: dict[str, set[str]] = {}
    rest = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # the module's docstring, or a note
        names = _defined_names(node)
        if names is None:
            rest.append(_segment(lines, node))
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                text = f"import {alias.name}" if isinstance(node, ast.Import) else \
                    f"from {'.' * node.level}{node.module or ''} import {alias.name}"
                defs.setdefault(name, []).append(text + (f" as {alias.asname}" if alias.asname else ""))
            continue
        text = _segment(lines, node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = text
        found = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        for name in names:
            defs.setdefault(name, []).append(text)
            uses.setdefault(name, set()).update(found)
    sources = {name: "\n".join(texts) for name, texts in defs.items()}
    used = {name: frozenset(n for n in names if n in sources and n != name) for name, names in uses.items()}
    return functions, sources, used, repr(rest)


class StepCache:
    """Steps remembered by their code and inputs, on this machine (`<store>/cache/steps/`; safe to delete).

    A step's key is made of: the cache's format and the Python and library versions; the toolbox's code; how the run
    records; the method file's module-level code that is no definition (rarely any); the step's own source and the
    sources of the method's definitions it uses, directly or through others (functions, constants, classes, and
    each imported name as its own `from … import name`: a block added to the import line changes no other step's
    key); and a fingerprint of the content of every input (a
    table by its cells, other values by `lab.values.fingerprint`; a text that names a file also by the file's size
    and time). An input that can't be fingerprinted means the step isn't remembered. A step that changes a value it
    was given in place is never remembered (skipping it would change what the next steps see).

    The calculation process keeps what it can between runs (`WARM`): fingerprints of the objects it already
    fingerprinted, and remembered steps it already read (checked against their file on disk). That is only safe
    while nothing but steps can change a value, and every step that ran was checked: see `remember_fingerprints`.
    """

    def __init__(self, folder: str | Path, source: str, read: bool = True, max_bytes: int = CACHE_MAX,
                 entry_max: int = ENTRY_MAX, mode: str = "", later: Any = None):
        self.folder = Path(folder)
        self.later = later  # lab.values.Later: the files are written by it (the same one as the ValueStore's)
        self.to_keep: list[tuple[str, dict]] = []
        self.written: dict[str, tuple] = {}
        self.broken: set[str] = set()  # entries that couldn't be read in this run (written again when it can)
        self.read = read
        self.max_bytes = max_bytes
        self.entry_max = entry_max
        self.hits = self.misses = self.stored = 0
        self.trusted = False  # see `remember_fingerprints`
        self.sources, self.definitions, self.uses, rest = _parse_for_cache(source)
        # `mode`: how the run records (values inside or not, calls kept): a step remembered one way has another
        # record than the other way.
        self.base = hashlib.sha256(repr((CACHE_FORMAT, _versions(), toolbox_fingerprint(), rest, mode))
                                   .encode("utf-8")).hexdigest()

    def closure(self, name: str) -> list[str]:
        """The method's definitions a function uses (functions, constants, classes, imported names), directly or
        through others (itself included)."""
        seen, todo = {name}, [name]
        while todo:
            for n in self.uses.get(todo.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return sorted(seen)

    # --------------------------------------------------------------- fingerprints kept in memory

    def remember_fingerprints(self) -> None:
        """Fingerprint each object once (a sequence's injections are big), and keep fingerprints and remembered
        steps in memory for the next run. Safe only when nothing but the steps can change a value between two
        steps: the worker asks for it when the recipe is plain calls. As soon as a step is found changing a value
        it was given, or a step ran that nobody checked, the memory is forgotten (`distrust`)."""
        self.trusted = True
        WARM.used.clear()

    def distrust(self) -> None:
        """Something may have changed a value nobody checked: forget every fingerprint and step kept in memory."""
        if self.trusted or WARM.fps or WARM.hits:
            WARM.forget()
        self.trusted = False

    def known(self, v: Any) -> str | None:
        """The fingerprint of this very object, when it is known (and nothing can have changed it)."""
        if not self.trusted:
            return None
        got = WARM.fps.get(id(v))
        if got is not None and got[0] is v:
            WARM.used.add(id(v))
            return got[1]
        return None

    def learn(self, v: Any, fp: str) -> None:
        if self.trusted and not (v is None or isinstance(v, (bool, int, float, str))):
            WARM.fps[id(v)] = (v, fp)
            WARM.used.add(id(v))

    def finish(self) -> None:
        """After a run: keep only the fingerprints of objects this run used (the others' objects can go)."""
        if not self.trusted:
            WARM.forget()
            return
        WARM.fps = {k: v for k, v in WARM.fps.items() if k in WARM.used}
        WARM.used.clear()

    def _fingerprint(self, v: Any, use_memo: bool) -> str | None:
        from .values import fingerprint

        if not self.trusted or v is None or isinstance(v, (bool, int, float, str)):
            return fingerprint(v)
        if use_memo:
            fp = self.known(v)
            if fp is not None:
                return fp
        fp = fingerprint(v)
        if fp is not None:
            self.learn(v, fp)
        return fp

    def inputs(self, bound: dict[str, Any], use_memo: bool = True) -> dict[str, str] | None:
        out = {}
        for k, v in bound.items():
            fp = self._fingerprint(v, use_memo)
            if fp is None:
                return None
            if isinstance(v, str) and len(v) < 1000:
                try:
                    if os.path.isfile(v):
                        st = os.stat(v)
                        fp += f":{st.st_size}:{st.st_mtime_ns}"
                except (OSError, ValueError):
                    pass
            out[k] = fp
        return out

    def key(self, name: str, kind: str, bound: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
        """(key, the inputs' fingerprints), or None when the step can't be remembered."""
        fps = self.inputs(bound)
        if fps is None:
            return None
        code = [self.definitions[n] for n in self.closure(name)] if name in self.sources and kind != "toolbox" \
            else [f"toolbox {name}"]
        # A function of the method given as an input (a formula for add_column): its fingerprint is its own code
        # only; the method's definitions it uses belong in the key too.
        for v in bound.values():
            for x in (v if isinstance(v, (list, tuple)) else v.values() if isinstance(v, dict) else (v,)):
                fn = getattr(x, "__layr_original__", x)
                if inspect.isfunction(fn) and fn.__name__ in self.sources:
                    code += [self.definitions[n] for n in self.closure(fn.__name__)]
        key =hashlib.sha256(repr((self.base, name, code, sorted(fps.items()))).encode("utf-8")).hexdigest()[:32]
        return key, fps

    def changed_inputs(self, bound: dict[str, Any], before: dict[str, str]) -> bool:
        """Whether a step changed a value it was given (its inputs fingerprinted again, from their content)."""
        if not bound:
            return False
        after = self.inputs(bound, use_memo=False)
        changed = after is None or {k: v.split(":")[0] for k, v in after.items()} != \
            {k: v.split(":")[0] for k, v in before.items()}
        if changed:
            self.distrust()  # it may have changed something the memory holds, through a shared part
        return changed

    # --------------------------------------------------------------- entries on disk

    def _dir(self, key: str) -> Path:
        return self.folder / key[:2] / key

    def folder_of(self, key: str) -> Path:
        return self._dir(key)

    def get(self, key: str) -> dict[str, Any] | None:
        d = self._dir(key)
        p = d / "entry.pkl"
        try:
            st = os.stat(p)
        except OSError:
            self.misses += 1
            return None
        sig = (st.st_size, st.st_mtime_ns)
        if self.trusted:
            warm = WARM.hits.get(key)
            if warm is not None and warm[1] == sig:
                self.hits += 1
                return {**warm[0], "folder": d}
        try:
            with open(p, "rb") as f:
                hit = pickle.load(f)
        except Exception:  # cut short, or from another version: a miss
            self.misses += 1
            self.broken.add(key)
            return None
        if "result" in hit:
            self.hits += 1
            hit["folder"] = d
            if time.time() - WARM.touched.get(key, 0) > TOUCH_EVERY:
                try:
                    os.utime(p)
                    WARM.touched[key] = time.time()
                    st = os.stat(p)
                    sig = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
            if self.trusted:
                WARM.keep(key, hit, sig)
        return hit

    def refuse(self, key: str, why: str) -> None:
        """Remember that this step mustn't be remembered."""
        self._write(key, {"why": why}, None, set())

    def put(self, key: str, entry: dict[str, Any], values: Any, files: set[str] | None = None) -> str:
        """Remember a step (`files`: the stored tables its record refers to; found in the record when not given).
        A step remembered already under this key is kept as it is: the same key means the same code and inputs,
        so the same result and record."""
        from .values import approx_size, files_in

        if key not in self.broken and (self._dir(key) / "entry.pkl").exists():
            return "stored"
        if approx_size(entry["result"]) > self.entry_max:
            return "not remembered: its result is too big"
        if values is None:
            files = set()
        elif files is None:
            files = files_in(entry["inside"]) | files_in(entry["entry"])
        try:
            return self._write(key, entry, values, files)
        except Exception as e:
            print(f"couldn't remember a step: {e}", file=sys.stderr)
            return "not remembered: its result can't be kept"

    def _write(self, key: str, entry: dict[str, Any], values: Any, files: set[str]) -> str:
        WARM.hits.pop(key, None)
        from .values import do

        d = self._dir(key)
        entry = {**entry, "files": values.export(files, d) if values is not None and files else {}}
        blob = pickle.dumps(entry, protocol=pickle.HIGHEST_PROTOCOL)  # now: the result as it is now
        if len(blob) > self.entry_max:
            return "not remembered: its result is too big"
        do(self.later, self._store, key, d, blob)  # the files: by the Later, while the run goes on
        self.broken.discard(key)
        if self.trusted and "result" in entry:
            # The next run finds it in memory (see `wait`). The run adds file names to the step's record afterwards:
            # the memory keeps a copy without them, as on disk.
            self.to_keep.append((key, {**entry, "entry": _copy(entry["entry"])}))
        self.stored += 1
        return "stored"

    def _store(self, key: str, d: Path, blob: bytes) -> None:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"entry.pkl.{os.getpid()}.tmp"
        tmp.write_bytes(blob)
        os.replace(tmp, d / "entry.pkl")
        st = os.stat(d / "entry.pkl")
        self.written[key] = (st.st_size, st.st_mtime_ns)

    def wait(self) -> None:
        """After a run: wait for its files to be written, then keep the steps it remembered in memory."""
        if self.later is not None:
            self.later.wait()
        for key, hit in self.to_keep:
            sig = self.written.get(key)
            if sig is not None and self.trusted:
                WARM.keep(key, hit, sig)
                WARM.touched[key] = time.time()
        self.to_keep.clear()

    def prune(self) -> None:
        """Drop the least recently used steps while the cache is bigger than its limit."""
        if not self.folder.exists():
            return
        entries = []
        total = 0
        for top in os.scandir(self.folder):
            if not top.is_dir():
                continue
            for e in os.scandir(top.path):
                if not e.is_dir():
                    continue
                if e.name.endswith(".tmp"):  # left by a run that was stopped while it wrote
                    try:
                        if time.time() - e.stat().st_mtime > 3600:
                            shutil.rmtree(e.path, ignore_errors=True)
                    except OSError:
                        pass
                    continue
                size = 0
                used = 0.0
                for root, _, names in os.walk(e.path):
                    for n in names:
                        try:
                            st = os.stat(os.path.join(root, n))
                        except OSError:
                            continue
                        if n.endswith(".tmp") and time.time() - st.st_mtime > 3600:
                            try:
                                os.unlink(os.path.join(root, n))
                            except OSError:
                                pass
                            continue
                        size += st.st_size
                        if n == "entry.pkl":
                            used = st.st_mtime
                entries.append((used, size, e.path))
                total += size
        if total <= self.max_bytes:
            return
        for used, size, path in sorted(entries):
            shutil.rmtree(path, ignore_errors=True)
            total -= size
            if total <= self.max_bytes * 0.8:
                break


@functools.lru_cache(maxsize=8)
def plain_recipe(source: str, recipe: str) -> bool:
    """Whether the recipe only calls functions and keeps their results (`a = f(...)`, `a, b = f(...)`, `return`):
    then nothing but the steps can change a value between two steps."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == recipe), None)
    if fn is None:
        return False
    for i, stmt in enumerate(fn.body):
        if i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Return):
            continue
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and all(
                isinstance(t, ast.Name) or (isinstance(t, ast.Tuple) and all(isinstance(e, ast.Name) for e in t.elts))
                for t in stmt.targets):
            continue
        return False
    return True

"""Tools for what happened inside a run's functions: a stored value's rows, and a function's code with its values.

`register(mcp, store, api)` adds `value_get` and `function_inside`. From `api` it uses `pick_run(run, experiment)`
(a run's dict) and, when there, `parsed(source)` (a method read with lab/methodfile.py).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import explain, methodfile, values
from .errors import LabError
from .recorder import docstring_lines, guards


def inside_view(store, run: dict[str, Any], function: str, call: int | None = None,
                    parse=methodfile.parse) -> dict[str, Any]:
    """One function of a run's method with what happened inside it in one call (see the tool's docstring)."""
    source = explain.run_source(store, run)
    if source is None:
        raise LabError(f"Run {run['id']} has no copy of its method (it is from before Research 0.3).")
    method = parse(source)
    fn = method.function(function)
    if fn is None:
        raise LabError(f"`{function}` is not a function of this run's method (a toolbox function? explain_function "
                       f"shows those). Its functions: {', '.join(f.name for f in method.functions)}.")
    lines = source.splitlines()
    doc = docstring_lines(fn.node)
    body = fn.node.body
    first_code = next((s for s in body if not (doc and s.lineno == doc[0])), None)
    out: dict[str, Any] = {
        "run": run["id"], "function": fn.name, "kind": fn.kind, "sentence": fn.doc.sentence, "params": fn.params,
        "line": fn.line, "end_line": fn.end_line, "code": fn.source,
        "docstring": {"line": doc[0], "end_line": doc[1]} if doc else None,
        "body_line": first_code.lineno if first_code is not None else fn.end_line,
        "guards": guards(fn.node), "formulas": explain.function_formulas(fn),
        "count": 0, "kept": 0, "calls": [], "call": None, "statements": [], "uses": [],
    }
    calls = explain.calls_of(store, run["id"])
    if calls is None:
        out["note"] = "This run has no record of its calls (it is from before Research 0.3): run it again."
        return out
    mine = [c for c in calls.get("calls") or [] if c.get("function") == fn.name]
    out["count"] = (calls.get("counts") or {}).get(fn.name, 0)
    out["kept"] = len(mine)
    record = values.inside_of(store, run["id"])
    inside = (record or {}).get("calls") or {}
    out["calls"] = [c["id"] for c in mine if str(c["id"]) in inside][:100]
    if not mine:
        out["note"] = "This function wasn't called in this run."
        return out
    if call is not None:
        chosen = next((c for c in mine if c["id"] == call), None)
        if chosen is None:
            raise LabError(f"Call {call} isn't a call of `{fn.name}` in run {run['id']}. Its calls with values: "
                           f"{', '.join(map(str, out['calls'][:20])) or 'none'}.")
    else:
        chosen = next((c for c in mine if str(c["id"]) in inside), mine[0])
    r = inside.get(str(chosen["id"]))
    out["call"] = {"id": chosen["id"], "n": chosen.get("n"), "ms": chosen.get("ms"), "error": chosen.get("error"),
                   "cached": bool(chosen.get("cached")) or _in_cached_step(calls, chosen),
                   "args": (r or {}).get("args") or chosen.get("inputs") or {}}
    if r is None:
        if record is None:
            out["note"] = ("This run kept no values inside its functions: it is from before Research 0.4, or they "
                           "were cleared from this older run to save space. Run the experiment again to see them.")
        else:
            limit = record.get("calls_limit")
            out["note"] = (f"No values were kept inside this call: only the first {limit} calls of a function keep "
                           "them." if limit else "No values were kept inside this call (only the first calls of a "
                           "function keep them).")
    else:
        for st in r.get("lines") or []:
            item = dict(st)
            item["code"] = "\n".join(lines[st["line"] - 1: st["end_line"]])
            out["statements"].append(item)
        if "returned" in r:
            out["returned"] = r["returned"]
        if r.get("error"):
            out["error"] = r["error"]
    out["uses"] = _uses(method, calls, chosen["id"])
    if record is not None and (record.get("values") or {}).get("sampled"):
        out["values_note"] = ("Some tables of this run were kept only in part (their first rows): the run's values "
                              "passed their size limit.")
    return out


def _in_cached_step(calls: dict[str, Any], call: dict[str, Any]) -> bool:
    c = call
    while c is not None:
        if c.get("cached"):
            return True
        c = calls["by_id"].get(c.get("parent"))
    return False


def _uses(method, calls: dict[str, Any], call_id: int) -> list[dict[str, Any]]:
    """The functions called inside this call (at any depth): how often, their sentence and formulas, one example."""
    found: dict[str, dict[str, Any]] = {}
    for c in explain._descendants(calls, call_id):
        name = c["function"]
        item = found.get(name)
        if item is None:
            inner = method.function(name)
            item = found[name] = {"function": name, "kind": inner.kind if inner else c.get("kind"), "count": 0,
                                  "direct": c.get("parent") == call_id}
            if inner is not None:
                item.update(line=inner.line, sentence=inner.doc.sentence, formulas=explain.function_formulas(inner))
            elif c.get("kind") == "toolbox":
                try:
                    t = explain.toolbox_view(name)
                    item.update(sentence=t["sentence"], formulas=t["formulas"])
                except LabError:
                    pass
            item["example"] = explain.call_view(inner, c)
        item["count"] += 1
        item["direct"] = item["direct"] or c.get("parent") == call_id
    return list(found.values())


def register(mcp, store, api) -> None:
    def parse(source: str):
        return api.parsed(source) if hasattr(api, "parsed") else methodfile.parse(source)

    @mcp.tool()
    def value_get(
        run: Annotated[str, Field(description="Run id.")],
        file: Annotated[str, Field(description="The `file` of a value summary (values/…, steps/…, outputs/…).")],
        offset: Annotated[int, Field(description="Rows to skip.")] = 0,
        limit: Annotated[int, Field(description="Rows to return (max 2000).")] = 50,
    ) -> dict[str, Any]:
        """The rows of a table a run kept: a wire between steps, or a value inside a step (any summary with a
        `file`). Gives {run, file, kind: table, total, offset, columns, rows, stored} (stored: full, or sampled when
        the run's values passed their size limit); a figure comes back as PNG data."""
        return values.read(store, run, file, offset, limit)

    @mcp.tool()
    def function_inside(
        function: Annotated[str, Field(description="A function of the method (a step, formula or helper).")],
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
        call: Annotated[int | None, Field(description="Which recorded call (an id from `calls`); default the "
                                                      "first.")] = None,
    ) -> dict[str, Any]:
        """Inside one function in a run: its code (lines numbered as in the method file), where its docstring
        ends, its guards (an `if` that only stops with a message), one call's arguments, and per statement the
        values it gave its names (loops: the first 3 and last 3 rounds, `n` counts them all; a failed round is
        marked), what it returned, how often it was called, and the formulas and functions used inside. Values are
        summaries; one with a `file` is a table: read its rows with value_get."""
        r = api.pick_run(run, experiment)
        return inside_view(store, r, function, call, parse)

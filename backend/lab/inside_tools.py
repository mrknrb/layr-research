"""Tools for what happened inside a run's functions: a stored value's rows, a function's code with its values, and
every round of a loop as a table.

`register(mcp, store, api)` adds `value_get`, `function_inside` and `loop_rounds`. From `api` it uses
`pick_run(run, experiment)`
(a run's dict) and, when there, `parsed(source)` (a method read with lab/methodfile.py).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import explain, methodfile, values
from .errors import LabError
from .recorder import docstring_lines, guards


def _method_function(store, run: dict[str, Any], function: str, parse) -> tuple[str, Any, Any]:
    """(the run's source, its method read, the function), or a plain error."""
    source = explain.run_source(store, run)
    if source is None:
        raise LabError(f"Run {run['id']} has no copy of its method (it is from before Research 0.3).")
    method = parse(source)
    fn = method.function(function)
    if fn is None:
        raise LabError(f"`{function}` is not a function of this run's method (a toolbox function? explain_function "
                       f"shows those). Its functions: {', '.join(f.name for f in method.functions)}.")
    return source, method, fn


def _chosen(run: dict[str, Any], fn, mine: list[dict], inside: dict, call: int | None,
            with_values: list[int]) -> dict[str, Any]:
    """The call asked for (an error when it isn't one of this function's), else the first one with values inside."""
    if call is not None:
        chosen = next((c for c in mine if c["id"] == call), None)
        if chosen is None:
            raise LabError(f"Call {call} isn't a call of `{fn.name}` in run {run['id']}. Its calls with values: "
                           f"{', '.join(map(str, with_values[:20])) or 'none'}.")
        return chosen
    return next((c for c in mine if str(c["id"]) in inside), mine[0])


def _no_values_note(record: dict[str, Any] | None) -> str:
    if record is None:
        return ("This run kept no values inside its functions: it is from before Research 0.4, or they were cleared "
                "from this older run to save space. Run the experiment again to see them.")
    limit = record.get("calls_limit")
    return (f"No values were kept inside this call: only the first {limit} calls of a function keep them." if limit
            else "No values were kept inside this call (only the first calls of a function keep them).")


def inside_view(store, run: dict[str, Any], function: str, call: int | None = None,
                    parse=methodfile.parse) -> dict[str, Any]:
    """One function of a run's method with what happened inside it in one call (see the tool's docstring)."""
    source, method, fn = _method_function(store, run, function, parse)
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
    chosen = _chosen(run, fn, mine, inside, call, out["calls"])
    r = inside.get(str(chosen["id"]))
    out["call"] = {"id": chosen["id"], "n": chosen.get("n"), "ms": chosen.get("ms"), "error": chosen.get("error"),
                   "cached": bool(chosen.get("cached")) or _in_cached_step(calls, chosen),
                   "args": (r or {}).get("args") or chosen.get("inputs") or {}}
    if r is None:
        out["note"] = _no_values_note(record)
    else:
        for st in r.get("lines") or []:
            item = dict(st)
            item["code"] = "\n".join(lines[st["line"] - 1: st["end_line"]])
            item["loop_line"] = st.get("loop_line")
            item["all"] = st.get("all", 0)
            item["cut"] = bool(st.get("cut"))
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


MAX_ROWS = 10_000  # rows of a loop's table per answer


def _kind(values: list) -> str:
    """number, text (booleans too), shape (a table's or list's size) or mixed; blanks don't count."""
    found = None
    for v in values:
        if v is None:
            continue
        if isinstance(v, (bool, str)):
            k = "text"
        elif isinstance(v, (int, float)) or (isinstance(v, dict) and v.get("kind") == "number"):
            k = "number"
        else:
            k = "shape"
        if found is None:
            found = k
        elif k != found:
            return "mixed"
    return found or "text"


def _by_round(light: dict[str, Any], name: str) -> dict[int, Any]:
    """A statement's light values of one name by the round of its loop (the last one when it ran more than once in
    a round)."""
    vals = (light.get("values") or {}).get(name) or []
    at = light.get("at")
    return dict(zip(at, vals)) if at is not None else {i + 1: v for i, v in enumerate(vals)}


def loop_view(store, run: dict[str, Any], function: str, line: int, call: int | None = None, offset: int = 0,
              limit: int = 1000, parse=methodfile.parse) -> dict[str, Any]:
    """One loop of a function in one call, as a table: a row per round, a column per name (see the tool's
    docstring)."""
    from .recorder import statement_map

    source, _, fn = _method_function(store, run, function, parse)
    lines = source.splitlines()
    calls = explain.calls_of(store, run["id"])
    if calls is None:
        raise LabError("This run has no record of its calls (it is from before Research 0.3): run it again.")
    mine = [c for c in calls.get("calls") or [] if c.get("function") == fn.name]
    if not mine:
        raise LabError(f"`{fn.name}` wasn't called in run {run['id']}.")
    record = values.inside_of(store, run["id"])
    inside = (record or {}).get("calls") or {}
    chosen = _chosen(run, fn, mine, inside, call, [c["id"] for c in mine if str(c["id"]) in inside])
    r = inside.get(str(chosen["id"]))
    if r is None:
        raise LabError(_no_values_note(record))
    items = r.get("lines") or []
    loops = sorted({s.start: s for s in statement_map(fn.node).values() if s.body is not None}.values(),
                   key=lambda s: s.start)
    loop = next((s for s in loops if s.start <= line <= s.end), None)
    if loop is None:
        where = ", ".join(str(s.start) for s in loops)
        raise LabError(f"Line {line} isn't the first line of a loop (`for …:`) of `{fn.name}`. "
                       + (f"Its loops start at line {where}." if where else "It has no loop."))
    out: dict[str, Any] = {"run": run["id"], "function": fn.name, "call": chosen["id"], "line": loop.start,
                           "end_line": loop.body[1] if loop.body else loop.end,
                           "code": lines[loop.start - 1].strip() if loop.start <= len(lines) else "",
                           "total": 0, "kept": 0, "cut": False, "offset": max(0, int(offset or 0)),
                           "columns": [], "rows": []}
    head = next((s for s in items if s.get("loop") and s["line"] == loop.start), None)
    if head is None:
        out["note"] = "This loop made no round in this call."
        return out
    every = r.get("every") or {}

    def light(item: dict) -> dict:
        return every.get(str(item["line"])) or {}

    out["total"] = head["n"]
    kept = out["kept"] = head.get("all", 0)
    if head.get("cut"):
        out["cut"] = True
        out["why"] = (f"Only the first {kept} rounds keep their values (the rest are counted)."
                      if head["cut"] == "rounds" else
                      f"The run kept as many values of every round as it may: only the first {kept} rounds of this "
                      "loop keep theirs (the rest are counted).")
    # the columns: the loop's own names, then every name given directly in its body, an inner loop as one column
    columns: list[dict[str, Any]] = []
    cells: list[dict[int, Any]] = []
    hl = light(head)
    for name in head["names"]:
        columns.append({"name": name, "line": head["line"]})
        cells.append(_by_round({"values": hl.get("values")}, name))
    for s in items:
        if s.get("loop_line") != loop.start:
            continue
        e = light(s)
        if s.get("loop"):
            at = e.get("at") or range(1, s.get("all", 0) + 1)
            counts: dict[int, int] = {}
            for k in at:
                counts[k] = counts.get(k, 0) + 1
            col = {"name": lines[s["line"] - 1].strip().rstrip(":")[:60], "line": s["line"], "kind": "loop"}
            if s.get("cut"):
                col["cut"] = True
            columns.append(col)
            cells.append({k: {"kind": "loop", "line": s["line"], "rounds": c} for k, c in counts.items()})
            continue
        for name in s["names"]:
            col = {"name": name, "line": s["line"]}
            if s.get("cut"):
                col["cut"] = True
            columns.append(col)
            cells.append(_by_round(e, name))
    for col, got in zip(columns, cells):
        if "kind" not in col:
            col["kind"] = _kind(list(got.values()))
    out["columns"] = columns
    # an inner loop: each row also says the round of the loop around it, and its round counts from 1 again there
    outer_of = None
    if head.get("loop_line") is not None:
        p = head["loop_line"]
        around = next((s for s in items if s.get("loop") and s["line"] == p), None)
        out["outer"] = {"line": p, "names": around["names"] if around else [], "total": around["n"] if around else 0,
                        "code": lines[p - 1].strip() if p <= len(lines) else ""}
        outer_of = hl.get("at") or list(range(1, kept + 1))
        starts: dict[Any, int] = {}
        for i, o in enumerate(outer_of):
            starts.setdefault(o, i + 1)
    lo = out["offset"] + 1
    hi = min(kept, out["offset"] + max(1, min(int(limit or 1000), MAX_ROWS)))
    rows = []
    for k in range(lo, hi + 1):
        row: list[Any] = [k]
        if outer_of is not None:
            o = outer_of[k - 1]
            row = [o, k - starts[o] + 1]
        row += [got.get(k) for got in cells]
        rows.append(row)
    out["rows"] = rows
    failed = next((s for s in items if s.get("loop_line") == loop.start and s.get("failed_at")), None)
    if failed is not None:
        k = failed["failed_at"]
        out["failed"] = {"round": k, "line": failed["line"], "error": r.get("error")}
        if outer_of is not None and 0 < k <= len(outer_of):
            o = outer_of[k - 1]
            out["failed"].update(outer_round=o, round=k - starts[o] + 1)
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
        summaries; one with a `file` is a table: read its rows with value_get. Each statement also says
        `loop_line` (the first line of the loop it is in, or null), `all` (how many rounds kept a value for every
        round: see loop_rounds) and `cut` (whether later rounds were only counted)."""
        r = api.pick_run(run, experiment)
        return inside_view(store, r, function, call, parse)

    @mcp.tool()
    def loop_rounds(
        function: Annotated[str, Field(description="A function of the method (a step, formula or helper).")],
        line: Annotated[int, Field(description="The first line of one of its loops (`for …:`), numbered as in the "
                                               "method file (function_inside's `line` of that statement).")],
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
        call: Annotated[int | None, Field(description="Which recorded call of the function (an id from "
                                                      "function_inside's `calls`); default the first.")] = None,
        offset: Annotated[int, Field(description="Rounds to skip.")] = 0,
        limit: Annotated[int, Field(description="Rounds to return (max 10000).")] = 1000,
    ) -> dict[str, Any]:
        """Every round of one loop in a run, as a table: one row per round, one column per name. The columns are
        the loop's own names (`for culture, group in …` gives culture and group), then every name given directly
        in its body, in the order of the code; a loop inside it is one column saying how many rounds it made in
        that round ({kind: loop, line, rounds}). A blank (null) is a round where that statement didn't run.
        Numbers, true/false and short texts are shown as they are; tables, lists and other big values only by
        their size ({kind: table, rows, columns}, {kind: list, length}...).
        Gives {run, function, call, line, end_line, code, total (rounds made), kept (rounds with values), cut,
        why?, offset, columns: [{name, line, kind: number|text|shape|mixed|loop, cut?}], rows: [[round, value,
        ...]], failed?: {round, line, error}}. For a loop inside another loop, `outer` is {line, names, total, code}
        of the loop around it, and every row starts with the outer loop's round: [outer round, round, value, ...]
        (round counts from 1 again in each outer round)."""
        r = api.pick_run(run, experiment)
        return loop_view(store, r, function, line, call, offset, limit, parse)

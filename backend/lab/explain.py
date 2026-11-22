"""How every number was calculated, from the code itself and the run's record of calls.

- A function: its sentence (the docstring's first line), its code, its formula drawn from its body (formulas) and
  from its docstring's Formula block, and the last run's calls of it with the numbers put in.
- A run: each step in the recipe's order with its input and output tables and the formulas used inside it.
- A number (provenance): the recorded call that returned exactly that value (by value; for a formula used on a
  whole column, by value and row), with its inputs, and in turn the calls that gave those inputs.
"""

from __future__ import annotations

import ast
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import latex, methodfile
from .errors import LabError
from .store import Store, jsonable, step_output

SAMPLE = 5  # calls shown per function
SKIP_VALUES = {0.0, 1.0, 100.0, 1000.0}


# ---------------------------------------------------------------------------------------------------- the record


@lru_cache(maxsize=8)
def _load_calls(path: str, mtime: float) -> dict[str, Any]:
    data = json.loads(Path(path).read_text("utf-8"))
    data["by_id"] = {c["id"]: c for c in data.get("calls") or []}
    children: dict[Any, list[int]] = {}
    for c in data.get("calls") or []:
        children.setdefault(c.get("parent"), []).append(c["id"])
    data["children"] = children
    return data


def calls_of(store: Store, run_id: str) -> dict[str, Any] | None:
    p = store.run_dir(run_id) / "calls.json"
    if not p.exists():
        return None
    return _load_calls(str(p), p.stat().st_mtime)


def run_source(store: Store, run: dict[str, Any]) -> str | None:
    p = store.run_dir(run["id"]) / "method.py"
    return p.read_text("utf-8") if p.exists() else None


# ---------------------------------------------------------------------------------------------------- values


def scalar(v: Any) -> float | None:
    """A recorded value as a number, when it is one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict) and v.get("kind") == "number":
        return float("nan")
    return None


def at(v: Any, pos: int) -> Any:
    """A recorded value at a row position: a column's value there, or the value itself."""
    if isinstance(v, dict) and v.get("kind") in ("column", "array"):
        vals = v.get("values") or []
        return vals[pos] if 0 <= pos < len(vals) else None
    return v


def is_column(v: Any) -> bool:
    return isinstance(v, dict) and v.get("kind") in ("column", "array")


def show(v: Any) -> Any:
    """A recorded value for people: numbers and text as they are, others as a short description."""
    if isinstance(v, dict):
        kind = v.get("kind")
        if kind == "number":
            return v.get("text")
        if kind == "table":
            return f"table: {v.get('rows')} rows × {len(v.get('columns') or [])} columns"
        if kind in ("column", "array"):
            n = v.get("length") or len(v.get("values") or [])
            return f"column of {n} values" + (f" ({v.get('name')})" if v.get("name") else "")
        if kind == "record":
            return {k: show(x) for k, x in (v.get("fields") or {}).items()}
        if kind == "dict":
            items = v.get("items")
            return {k: show(x) for k, x in items.items()} if items else f"{v.get('length')} entries"
        if kind == "list":
            return f"list of {v.get('length')}"
        if kind in ("object", "figure", "bytes"):
            return v.get("text") or kind
    return v


def _example_position(call: dict[str, Any]) -> int | None:
    """For a call on whole columns, the first row where the result is a number."""
    res = call.get("result")
    if not is_column(res):
        return None
    for i, v in enumerate(res.get("values") or []):
        if isinstance(v, (int, float)):
            return i
    return 0


# ---------------------------------------------------------------------------------------------------- functions


def function_formulas(fn: methodfile.Function) -> list[dict[str, str]]:
    """The function's formulas: its body drawn (for a formula) and its docstring's Formula block."""
    out = []
    if fn.formula_shaped:
        try:
            for line in latex.draw_function(fn.node):
                out.append({"latex": line, "from": "code"})
        except latex.DrawError:
            pass
    for f in latex.draw_formula_lines(fn.doc.formula):
        out.append({"latex": f["latex"], "from": "docstring", "code": f["code"]})
    return out


def numbers_latex(fn: methodfile.Function, call: dict[str, Any], pos: int | None = None) -> list[str]:
    """A formula's body with one call's numbers put in (`pos`: the row, for a call on whole columns)."""
    if not fn.formula_shaped:
        return []
    if pos is None:
        pos = _example_position(call)
    values: dict[str, Any] = {}
    for k, v in (call.get("inputs") or {}).items():
        x = at(v, pos) if pos is not None else v
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            values[k] = x
        elif isinstance(x, dict) and x.get("kind") == "number":
            values[k] = float("nan")
    result = call.get("result")
    r = at(result, pos) if pos is not None else result
    if isinstance(r, dict) and r.get("kind") == "number":
        r = float("nan")
    if not isinstance(r, (int, float)):
        return []
    try:
        return latex.draw_function(fn.node, values, r, with_result=True)
    except latex.DrawError:
        return []


def call_view(fn: methodfile.Function | None, call: dict[str, Any], pos: int | None = None) -> dict[str, Any]:
    if pos is None:
        pos = _example_position(call)
    inputs = {k: show(at(v, pos) if pos is not None and is_column(v) else v)
              for k, v in (call.get("inputs") or {}).items()}
    result = call.get("result")
    out = {"call": call["id"], "n": call.get("n"), "ms": call.get("ms"), "inputs": inputs,
           "result": show(at(result, pos) if pos is not None and is_column(result) else result),
           "error": call.get("error")}
    if pos is not None:
        out["row"] = pos
        out["column_length"] = (result or {}).get("length")
    if fn is not None:
        out["latex"] = numbers_latex(fn, call, pos)
    return out


def function_view(method: methodfile.Method, name: str, calls: dict[str, Any] | None = None,
                  run: dict[str, Any] | None = None, call_id: int | None = None) -> dict[str, Any]:
    """Everything about one function of a method, with the run's numbers when there is a run."""
    fn = method.function(name)
    if fn is None:
        raise LabError(f"The method has no function `{name}`. Functions: "
                       f"{', '.join(f.name for f in method.functions)}.")
    view: dict[str, Any] = {"name": fn.name, "kind": fn.kind, "line": fn.line, "end_line": fn.end_line,
                            "sentence": fn.doc.sentence, "text": fn.doc.text, "code": fn.source, "params": fn.params,
                            "formulas": function_formulas(fn), "calls_functions": fn.calls}
    if calls:
        mine = [c for c in calls.get("calls") or [] if c.get("function") == name]
        view["count"] = (calls.get("counts") or {}).get(name, 0)
        view["kept"] = len(mine)
        chosen = next((c for c in mine if c["id"] == call_id), None) if call_id is not None else None
        sample = mine[:SAMPLE]
        if chosen and chosen not in sample:
            sample = [chosen] + sample[:SAMPLE - 1]
        view["sample"] = [call_view(fn, c) for c in sample]
        if chosen or sample:
            view["example"] = call_view(fn, chosen or sample[0])
    if run:
        step = next((s for s in run.get("steps") or [] if s.get("function") == name), None)
        if step:
            view["step"] = step
    return view


def plain_signature(name: str, obj: Any) -> str:
    """`name(a, b: Line = 1) -> Table` as written in the code: annotations without the quotes Python keeps for
    them (`-> 'Line'`) when the module uses `from __future__ import annotations`."""
    import inspect

    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return name

    def plain(a: Any) -> Any:
        return inspect.Parameter.empty if a is inspect.Parameter.empty else _Plain(a)

    params = [p.replace(annotation=plain(p.annotation)) for p in sig.parameters.values()]
    return f"{name}{sig.replace(parameters=params, return_annotation=plain(sig.return_annotation))}"


class _Plain:
    """An annotation shown as its text (inspect writes a string annotation with quotes)."""

    def __init__(self, a: Any):
        self.text = a if isinstance(a, str) else getattr(a, "__forward_arg__", None) or inspect_name(a)

    def __repr__(self) -> str:
        return self.text


def inspect_name(a: Any) -> str:
    import inspect

    return inspect.formatannotation(a)


def toolbox_view(name: str, calls: dict[str, Any] | None = None) -> dict[str, Any]:
    """A toolbox function's sentence, formula block, code and recorded calls."""
    import importlib
    import inspect

    from toolbox import registry

    found = registry.find(name)
    if found is None:
        raise LabError(f"The toolbox has no `{name}`. It has: {', '.join(registry.everything())}.")
    imported_from, obj = found
    doc = methodfile.parse_doc(inspect.getdoc(obj) or "")
    try:
        source = inspect.getsource(obj)
    except (OSError, TypeError):
        source = ""
    module = getattr(obj, "__module__", "toolbox")
    view = {"name": name, "kind": "toolbox", "module": module, "import": imported_from, "sentence": doc.sentence,
            "text": doc.text, "code": source, "signature": plain_signature(name, obj) if callable(obj) else name,
            "formulas": [{"latex": f["latex"], "from": "docstring", "code": f["code"]}
                         for f in latex.draw_formula_lines(doc.formula)],
            "is_function": inspect.isfunction(obj)}
    importlib.import_module(module)
    if calls:
        mine = [c for c in calls.get("calls") or [] if c.get("function") == name]
        view["count"] = (calls.get("counts") or {}).get(name, 0)
        view["kept"] = len(mine)
        view["sample"] = [call_view(None, c) for c in mine[:SAMPLE]]
    return view


def toolbox_docs() -> list[dict[str, Any]]:
    """Every function of the toolbox and its add-ons (see toolbox_view); `import` says where a method imports it."""
    from toolbox import registry

    return [toolbox_view(n) for n in registry.everything()]


# ---------------------------------------------------------------------------------------------------- runs


def _descendants(calls: dict[str, Any], call_id: int) -> list[dict[str, Any]]:
    out = []
    todo = list(calls["children"].get(call_id, []))
    while todo:
        cid = todo.pop(0)
        c = calls["by_id"].get(cid)
        if c is None:
            continue
        out.append(c)
        todo.extend(calls["children"].get(cid, []))
    return sorted(out, key=lambda c: c["id"])


def explain_run(store: Store, run: dict[str, Any]) -> dict[str, Any]:
    """Each step of a run in order: what it does, its code, its tables, the formulas used inside with numbers."""
    source = run_source(store, run)
    if source is None:
        raise LabError(f"Run {run['id']} has no copy of its method (it is from before Research 0.3).")
    method = methodfile.parse(source)
    calls = calls_of(store, run["id"]) or {"calls": [], "children": {}, "by_id": {}, "counts": {}}
    steps = []
    for s in run.get("steps") or []:
        fn = method.function(s["function"])
        item = {"function": s["function"], "kind": s.get("kind"), "status": s.get("status"), "ms": s.get("ms"),
                "error": s.get("error"), "inputs": s.get("inputs"), "output": s.get("output")}
        if fn is not None:
            item.update(line=fn.line, sentence=fn.doc.sentence, text=fn.doc.text, code=fn.source,
                        formulas=function_formulas(fn))
        else:
            item.update(toolbox_view(s["function"]) if s.get("kind") == "toolbox" else {})
            item["kind"] = s.get("kind")
        used = []
        seen = set()
        for c in _descendants(calls, s["call"]):
            name = c["function"]
            if name in seen:
                continue
            seen.add(name)
            inner = method.function(name)
            entry = {"function": name, "kind": inner.kind if inner else c.get("kind"),
                     "count": sum(1 for x in _descendants(calls, s["call"]) if x["function"] == name)}
            if inner:
                entry.update(sentence=inner.doc.sentence, formulas=function_formulas(inner), line=inner.line)
            elif c.get("kind") == "toolbox":
                try:
                    t = toolbox_view(name)
                    entry.update(sentence=t["sentence"], formulas=t["formulas"])
                except LabError:
                    pass
            entry["example"] = call_view(inner, c)
            used.append(entry)
        item["used"] = used
        steps.append(item)
    return {"run": run["id"], "method": run.get("method"), "name": method.name, "description": method.description,
            "steps": steps, "source": source}


def explain_method(method: methodfile.Method) -> dict[str, Any]:
    """A method without a run: the recipe's steps in order, each with the functions it uses."""
    recipe = method.recipe_function
    order = []
    if recipe:
        for node in ast.walk(recipe.node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                order.append((node.lineno, node.col_offset, node.func.id))
    steps = []
    seen = set()
    for _, _, name in sorted(order):
        if name in seen:
            continue
        seen.add(name)
        fn = method.function(name)
        if fn is None:
            continue
        used, todo = [], list(fn.calls)
        while todo:
            n = todo.pop(0)
            inner = method.function(n)
            if inner is None or n in [u["function"] for u in used] or n == name:
                continue
            used.append({"function": n, "kind": inner.kind, "sentence": inner.doc.sentence,
                         "formulas": function_formulas(inner), "line": inner.line})
            todo.extend(inner.calls)
        steps.append({"function": name, "kind": fn.kind, "line": fn.line, "sentence": fn.doc.sentence,
                      "text": fn.doc.text, "code": fn.source, "formulas": function_formulas(fn), "used": used})
    return {"name": method.name, "description": method.description, "steps": steps}


# ---------------------------------------------------------------------------------------------------- provenance


def _close(a: float, b: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return False
    return math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-14)


def _key(v: float) -> str:
    return f"{v:.10g}"


@lru_cache(maxsize=8)
def _value_index(path: str, mtime: float) -> dict[str, list[tuple[int, Any]]]:
    """Every number any recorded call returned: value → [(call id, where: None | row | field)]."""
    calls = _load_calls(path, mtime)
    index: dict[str, list[tuple[int, Any]]] = {}

    def add(v: Any, cid: int, where: Any) -> None:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v)):
            index.setdefault(_key(float(v)), []).append((cid, where))

    for c in calls.get("calls") or []:
        r = c.get("result")
        if isinstance(r, dict):
            kind = r.get("kind")
            if kind in ("column", "array"):
                for i, v in enumerate(r.get("values") or []):
                    add(v, c["id"], ("row", i))
            elif kind == "record":
                for k, v in (r.get("fields") or {}).items():
                    add(v, c["id"], ("field", k))
            elif kind == "dict":
                for k, v in (r.get("items") or {}).items():
                    add(v, c["id"], ("field", k))
        else:
            add(r, c["id"], None)
    return index


def _candidates(store: Store, run_id: str, value: float) -> list[tuple[dict[str, Any], Any]]:
    p = store.run_dir(run_id) / "calls.json"
    calls = _load_calls(str(p), p.stat().st_mtime)
    index = _value_index(str(p), p.stat().st_mtime)
    out = []
    for cid, where in index.get(_key(value), []):
        c = calls["by_id"][cid]
        r = c.get("result")
        v = r
        if where and where[0] == "row":
            v = at(r, where[1])
        elif where and where[0] == "field":
            v = (r.get("fields") or r.get("items") or {}).get(where[1])
        if isinstance(v, (int, float)) and _close(float(v), value):
            out.append((c, where))
    return out


_KIND_RANK = {"formula": 0, "helper": 1, "toolbox": 2, "step": 3, "recipe": 4}


def _score(call: dict[str, Any], where: Any, row: dict[str, Any] | None, method: methodfile.Method) -> tuple:
    """Prefer formulas over helpers over steps; for a row of a column-wise call, the row whose inputs match."""
    fn = method.function(call["function"])
    kind = fn.kind if fn else call.get("kind", "toolbox")
    matches = 0
    if row and where and where[0] == "row":
        for k, v in (call.get("inputs") or {}).items():
            x = at(v, where[1])
            rv = row.get(k)
            if isinstance(x, (int, float)) and isinstance(rv, (int, float)) and _close(float(x), float(rv)):
                matches += 1
            elif x is not None and rv is not None and str(x) == str(rv):
                matches += 1
    return (-matches, _KIND_RANK.get(kind, 5), call.get("depth", 9), call["id"])


def _node(method: methodfile.Method, call: dict[str, Any], where: Any, value: float) -> dict[str, Any]:
    fn = method.function(call["function"])
    pos = where[1] if where and where[0] == "row" else None
    view = call_view(fn, call, pos)
    node = {"function": call["function"], "kind": fn.kind if fn else call.get("kind", "toolbox"),
            "value": value, "call": call["id"], "inputs": view["inputs"], "result": view["result"],
            "latex_numbers": view.get("latex") or []}
    if where and where[0] == "field":
        node["field"] = where[1]
    if pos is not None:
        node["row"] = pos
    if fn is not None:
        node.update(line=fn.line, sentence=fn.doc.sentence, formulas=function_formulas(fn))
    else:
        try:
            t = toolbox_view(call["function"])
            node.update(sentence=t["sentence"], formulas=t["formulas"])
        except LabError:
            pass
    return node


def equals(column, v: Any):
    """Which values of a column equal `v`: numbers as numbers (0 equals 0.0), anything else as text."""
    import pandas as pd

    if isinstance(v, (int, float)) and not isinstance(v, bool):
        nums = pd.to_numeric(column, errors="coerce")
        return (nums - float(v)).abs() <= 1e-9 * max(1.0, abs(float(v)))
    return column.astype(str) == str(v)


def table_maker(store: Store, run: dict[str, Any], table: str) -> str | None:
    """The step that made a table of the run (the last step whose result has the same rows and columns)."""
    if table.startswith("step:"):
        return table[5:]
    out = (run.get("outputs") or {}).get(table) or {}
    for s in reversed(run.get("steps") or []):
        o = step_output(s)
        if o.get("kind") == "table" and o.get("rows") == out.get("rows") and o.get("columns") == out.get("columns"):
            return s["function"]
    return None


def column_creator(run: dict[str, Any], column: str) -> dict[str, Any] | None:
    """The step that first gave a table with this column (where the column was made)."""
    for s in run.get("steps") or []:
        outputs = [step_output(s)]
        outputs += [o for o in step_output(s).get("items") or [] if isinstance(o, dict)]
        if any(o.get("kind") == "table" and column in (o.get("columns") or []) for o in outputs):
            return s
    return None


def provenance(store: Store, run: dict[str, Any], table: str, where: dict[str, Any], column: str | None = None,
               value: float | None = None, depth: int = 4, max_nodes: int = 10) -> dict[str, Any]:
    """Where one number of a run's table came from: the chain of recorded calls that produced it."""
    df = store.run_table(run["id"], table)
    picked = df
    for c, v in (where or {}).items():
        if c not in picked.columns:
            raise LabError(f"The table {table} has no column '{c}'. Columns: {', '.join(map(str, df.columns))}.")
        picked = picked[equals(picked[c], v)]
    if len(picked) != 1:
        raise LabError(f"{len(picked)} rows of {table} match {where}: give columns that pick exactly one row.")
    row = jsonable(picked.iloc[0].to_dict())
    numeric = [c for c, v in row.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if column is None and value is None:
        column = next((c for c in reversed(numeric) if c not in (where or {})), None)
    if value is None:
        if column not in row:
            raise LabError(f"The table {table} has no column '{column}'.")
        value = row[column]
    out: dict[str, Any] = {"run": run["id"], "table": table, "row": row, "column": column, "value": value,
                           "chain": []}
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        out["note"] = "This is not a number: it was written by the step that made the table (see its code)."
        return out
    source = run_source(store, run)
    if source is None or calls_of(store, run["id"]) is None:
        out["note"] = "This run has no record of its calls (it is from before Research 0.3): run it again."
        return out
    method = methodfile.parse(source)
    calls = calls_of(store, run["id"])
    creator = column_creator(run, column) if column else None
    inside = {c["id"] for c in _descendants(calls, creator["call"])} if creator else None
    seen_calls: set[int] = set()
    settings = set(method.recipe_function.params if method.recipe_function else [])

    def follow(v: float, ctx_row: dict[str, Any] | None, level: int) -> None:
        if level > depth or len(out["chain"]) >= max_nodes:
            return
        found = _candidates(store, run["id"], float(v))
        found = [(c, w) for c, w in found if c["id"] not in seen_calls and not c.get("step")
                 and c["function"] != (method.recipe or "")]
        if level == 0 and inside is not None:  # the number's column was made by this step: look only inside it
            found = [(c, w) for c, w in found if c["id"] in inside]
        if not found:
            return
        found.sort(key=lambda cw: _score(cw[0], cw[1], ctx_row, method))
        call, where = found[0]
        seen_calls.add(call["id"])
        node = _node(method, call, where, float(v))
        node["level"] = level
        out["chain"].append(node)
        pos = where[1] if where and where[0] == "row" else None
        for k, x in (call.get("inputs") or {}).items():
            xv = at(x, pos) if pos is not None else x
            if not isinstance(xv, (int, float)) or isinstance(xv, bool) or isinstance(xv, int):
                continue
            if float(xv) in SKIP_VALUES or k in settings or _close(float(xv), float(v)):
                continue
            follow(float(xv), ctx_row, level + 1)

    follow(float(value), row, 0)
    if not out["chain"]:
        maker = creator["function"] if creator else table_maker(store, run, table)
        out["note"] = ("No separate formula returned this number: it was worked out inside the step "
                       f"`{maker}` (see its code)." if maker else
                       "No recorded formula returned this number: see the code of the step that made the table.")
    return out

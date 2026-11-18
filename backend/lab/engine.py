"""Methods: checking them, editing them in validated batches, running them, and explaining them.

A method is a named list of steps, read top to bottom:

    {"id": "growth-rate", "name": "...", "description": "...",
     "inputs":  [{"id": "data", "kind": "file", "label": "Plate reader export"}],
     "params":  [{"id": "blank_wells", "type": "list", "label": "Blank wells", "default": ["A1"]}],
     "steps":   [{"id": "read", "type": "table.read", "inputs": {"file": "$data"}},
                 {"id": "norm", "type": "table.compute", "inputs": {"table": "read.table"},
                  "params": {"column": "od", "formula": "raw - mean(raw[well == \\"A1\\"])"}}],
     "outputs": {"normalized": {"from": "norm.table", "database": true}}}

References: "$input" is one of the method's inputs ("$corrections": the experiment's approved corrections),
"step.port" an earlier step's output, and a parameter value "@name" one of the method's parameters (so an
experiment can set it). A step of type "method.call" runs another method: its params are {"method": id, "version"?,
...that method's parameters}, its inputs that method's inputs, and its outputs that method's outputs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import platform
import time
from typing import Any
from collections.abc import Callable

import pandas as pd

from . import steps as _steps  # noqa: F401  (registers the step types)
from .core import REGISTRY, Ctx, StepError, StepOut, step_type
from .store import Store, jsonable, new_id, now, records

CALL = "method.call"
MAX_DEPTH = 4


# ---------------------------------------------------------------------------------------------------- params


def param_values(method: dict, given: dict | None) -> dict[str, Any]:
    vals = {p["id"]: copy.deepcopy(p.get("default")) for p in method.get("params") or []}
    for k, v in (given or {}).items():
        if k in vals:
            vals[k] = v
    return vals


def resolve_params(step: dict, values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in (step.get("params") or {}).items():
        if isinstance(v, str) and v.startswith("@") and v[1:] in values:
            out[k] = copy.deepcopy(values[v[1:]])
        else:
            out[k] = copy.deepcopy(v)
    return out


def step_params(step: dict, values: dict[str, Any]) -> dict[str, Any]:
    """A step's parameters as it runs: the step type's defaults, then the step's own (with @refs resolved)."""
    if step["type"] == CALL:
        return resolve_params(step, values)
    st = step_type(step["type"])
    return {**st.defaults(), **resolve_params(step, values)}


# ---------------------------------------------------------------------------------------------------- validation


def validate(method: dict, store: Store | None = None, depth: int = 0) -> list[dict[str, str]]:
    """Problems as {level: error|warning, step, message}; a method with errors can't be saved or run."""
    probs: list[dict[str, str]] = []

    def err(step: str, msg: str, level: str = "error") -> None:
        probs.append({"level": level, "step": step, "message": msg})

    if not method.get("name"):
        err("", "The method needs a name.")
    inputs = {i["id"]: i.get("kind", "file") for i in method.get("inputs") or [] if i.get("id")}
    params = {p["id"] for p in method.get("params") or [] if p.get("id")}
    available: dict[str, str] = {f"${k}": v for k, v in inputs.items()}
    available["$corrections"] = "corrections"
    seen: set[str] = set()
    steps = method.get("steps") or []
    if not steps:
        err("", "The method has no steps yet.", "warning")
    for s in steps:
        sid = s.get("id") or ""
        if not sid or not sid.replace("_", "").replace("-", "").isalnum():
            err(sid, f"Step id '{sid}' must be letters, digits, - or _.")
            continue
        if sid in seen:
            err(sid, f"Two steps are called '{sid}'.")
        seen.add(sid)
        typ = s.get("type")
        if typ == CALL:
            sub_id = (s.get("params") or {}).get("method")
            if not store:
                continue
            if depth >= MAX_DEPTH or sub_id == method.get("id"):
                err(sid, "A method can't call itself (or nest deeper than 4).")
                continue
            try:
                sub = store.get_method(sub_id, (s.get("params") or {}).get("version"))
            except StepError as e:
                err(sid, str(e))
                continue
            sub_inputs = {i["id"]: i.get("kind", "file") for i in sub.get("inputs") or []}
            for port, kind in sub_inputs.items():
                ref = (s.get("inputs") or {}).get(port)
                if ref is None:
                    err(sid, f"Input '{port}' of method {sub_id} is not connected.")
                elif ref not in available:
                    err(sid, f"Input '{port}' takes '{ref}', which no earlier step or method input gives.")
            for name in (sub.get("outputs") or {}):
                available[f"{sid}.{name}"] = "table"
            continue
        if typ not in REGISTRY:
            err(sid, f"Unknown step type '{typ}'. Step types: step_catalog lists them.")
            continue
        st = REGISTRY[typ]
        given = s.get("inputs") or {}
        for port in st.inputs:
            ref = given.get(port.id)
            if ref is None:
                if not port.optional:
                    err(sid, f"Input '{port.id}' ({port.label}) is not connected.")
                continue
            if ref not in available:
                err(sid, f"Input '{port.id}' takes '{ref}', which no earlier step or method input gives. "
                         f"Available: {', '.join(sorted(available)) or 'nothing yet'}.")
            elif available[ref] != port.kind and not (available[ref] == "file" and port.kind == "file"):
                err(sid, f"Input '{port.id}' wants {port.kind} but '{ref}' gives {available[ref]}.", "warning")
        for extra in set(given) - {p.id for p in st.inputs}:
            err(sid, f"{typ} has no input '{extra}'.")
        known = {p.id for p in st.params}
        for k, v in (s.get("params") or {}).items():
            if k not in known:
                err(sid, f"{typ} has no parameter '{k}'. Parameters: {', '.join(sorted(known)) or 'none'}.")
            if isinstance(v, str) and v.startswith("@") and v[1:] not in params:
                err(sid, f"Parameter '{k}' refers to method parameter '{v[1:]}', which the method doesn't have.")
        for port in st.outputs:
            available[f"{sid}.{port.id}"] = port.kind
    for name, out in (method.get("outputs") or {}).items():
        src = out.get("from") if isinstance(out, dict) else out
        if src not in available:
            err("", f"Output '{name}' comes from '{src}', which no step gives.")
    return probs


def errors(probs: list[dict]) -> list[dict]:
    return [p for p in probs if p["level"] == "error"]


# ---------------------------------------------------------------------------------------------------- patches


def apply_patch(method: dict, ops: list[dict]) -> dict:
    """A new method from a batch of operations (nothing is saved here). Operations:
    add {step, after?|before?} · remove {step} · move {step, after?|before?} · set {step, params?, inputs?, label?,
    note?, type?} (params/inputs are merged; a null value removes the key) · method {name?, description?, inputs?,
    params?, outputs?} (replaces those fields) · add_param {param} · remove_param {param} · output {name, from?,
    database?} (from null removes it)."""
    m = copy.deepcopy(method)
    m.setdefault("steps", [])
    m.setdefault("params", [])
    m.setdefault("inputs", [])
    m.setdefault("outputs", {})

    def index(sid: str) -> int:
        for i, s in enumerate(m["steps"]):
            if s.get("id") == sid:
                return i
        raise StepError(f"No step '{sid}' in the method. Steps: {', '.join(s['id'] for s in m['steps'])}.")

    def place(step: dict, op: dict) -> None:
        if op.get("after"):
            m["steps"].insert(index(op["after"]) + 1, step)
        elif op.get("before"):
            m["steps"].insert(index(op["before"]), step)
        elif op.get("first"):
            m["steps"].insert(0, step)
        else:
            m["steps"].append(step)

    for n, op in enumerate(ops, 1):
        kind = op.get("op")
        if kind == "add":
            step = copy.deepcopy(op.get("step") or {})
            if not step.get("id") or not step.get("type"):
                raise StepError(f"Operation {n} (add): the step needs an id and a type.")
            if any(s.get("id") == step["id"] for s in m["steps"]):
                raise StepError(f"Operation {n} (add): a step '{step['id']}' exists already.")
            step.setdefault("inputs", {})
            step.setdefault("params", {})
            place(step, op)
        elif kind == "remove":
            m["steps"].pop(index(op.get("step", "")))
        elif kind == "move":
            step = m["steps"].pop(index(op.get("step", "")))
            place(step, op)
        elif kind == "set":
            s = m["steps"][index(op.get("step", ""))]
            for field in ("params", "inputs"):
                for k, v in (op.get(field) or {}).items():
                    s.setdefault(field, {})
                    if v is None:
                        s[field].pop(k, None)
                    else:
                        s[field][k] = v
            for field in ("label", "note", "type"):
                if field in op:
                    if op[field] is None:
                        s.pop(field, None)
                    else:
                        s[field] = op[field]
        elif kind == "method":
            for field in ("name", "description", "inputs", "params", "outputs"):
                if field in op:
                    m[field] = copy.deepcopy(op[field])
        elif kind == "add_param":
            p = op.get("param") or {}
            if not p.get("id"):
                raise StepError(f"Operation {n} (add_param): the parameter needs an id.")
            m["params"] = [x for x in m["params"] if x.get("id") != p["id"]] + [p]
        elif kind == "remove_param":
            m["params"] = [x for x in m["params"] if x.get("id") != op.get("param")]
        elif kind == "output":
            if op.get("from") is None:
                m["outputs"].pop(op.get("name"), None)
            else:
                m["outputs"][op["name"]] = {"from": op["from"], "database": bool(op.get("database"))}
        else:
            raise StepError(f"Operation {n}: unknown op '{kind}'. Ops: add, remove, move, set, method, add_param, "
                            "remove_param, output.")
    return m


def diff(old: dict, new: dict) -> list[str]:
    """What changed between two versions of a method, in plain lines."""
    lines = []
    for f in ("name", "description"):
        if old.get(f) != new.get(f):
            lines.append(f"{f.capitalize()}: '{old.get(f) or ''}' → '{new.get(f) or ''}'")
    os_ = {s["id"]: s for s in old.get("steps") or []}
    ns = {s["id"]: s for s in new.get("steps") or []}

    def title(s: dict) -> str:
        st = REGISTRY.get(s["type"])
        return f"'{s.get('label') or (st.title if st else s['type'])}' ({s['id']})"

    for sid in os_.keys() - ns.keys():
        lines.append(f"Removed step {title(os_[sid])}")
    for sid in ns.keys() - os_.keys():
        lines.append(f"Added step {title(ns[sid])} of type {ns[sid]['type']}")
    order_old = [s for s in os_ if s in ns]
    order_new = [s for s in ns if s in os_]
    if order_old != order_new:
        lines.append("New step order: " + ", ".join(order_new))
    for sid in ns.keys() & os_.keys():
        a, b = os_[sid], ns[sid]
        for field in ("params", "inputs"):
            pa, pb = a.get(field) or {}, b.get(field) or {}
            for k in sorted(pa.keys() | pb.keys()):
                if pa.get(k) != pb.get(k):
                    lines.append(f"Step {title(b)}: {field[:-1]} {k}: {json.dumps(pa.get(k), ensure_ascii=False)} → "
                                 f"{json.dumps(pb.get(k), ensure_ascii=False)}")
        for field in ("label", "note", "type"):
            if a.get(field) != b.get(field):
                lines.append(f"Step {title(b)}: {field} '{a.get(field) or ''}' → '{b.get(field) or ''}'")
    pa = {p["id"]: p for p in old.get("params") or []}
    pb = {p["id"]: p for p in new.get("params") or []}
    for k in sorted(pa.keys() | pb.keys()):
        if pa.get(k) != pb.get(k):
            if k not in pa:
                lines.append(f"New parameter {k} (default {json.dumps(pb[k].get('default'), ensure_ascii=False)})")
            elif k not in pb:
                lines.append(f"Removed parameter {k}")
            else:
                lines.append(f"Parameter {k}: default {json.dumps(pa[k].get('default'), ensure_ascii=False)} → "
                             f"{json.dumps(pb[k].get('default'), ensure_ascii=False)}")
    if (old.get("inputs") or []) != (new.get("inputs") or []):
        lines.append("Inputs: " + ", ".join(i["id"] for i in new.get("inputs") or []))
    if (old.get("outputs") or {}) != (new.get("outputs") or {}):
        lines.append("Outputs: " + ", ".join(new.get("outputs") or {}))
    return lines


# ---------------------------------------------------------------------------------------------------- running


def _fp(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(jsonable(parts), sort_keys=True, default=str).encode()).hexdigest()[:24]


class Runner:
    """Runs methods; keeps a cache of step results (keyed by step type, parameters and input fingerprints) so a
    rerun after a correction recomputes only what changed."""

    def __init__(self, store: Store, cache: dict | None = None):
        self.store = store
        self.cache: dict[Any, Any] = cache if cache is not None else {}

    def _remember(self, key: Any, value: Any) -> None:
        steps = [k for k in self.cache if isinstance(k, tuple) and k and k[0] == "step"]
        if len(steps) > 300:
            for k in steps[:100]:
                del self.cache[k]
        self.cache[key] = value

    def execute(self, method: dict, scope: dict[str, Any], fps: dict[str, str], values: dict[str, Any],
                progress: Callable[[str], None], on_step: Callable[[dict], None] | None = None, depth: int = 0,
                prefix: str = "") -> tuple[list[dict], dict[str, Any], dict[str, str]]:
        """Runs the steps in order. Returns (step records, scope, fingerprints); raises StepError at the first
        failing step after recording it."""
        records_: list[dict] = []
        ctx = Ctx(cache=self.cache, store_path=self.store.path, progress=progress)
        for s in method.get("steps") or []:
            sid = s["id"]
            rec: dict[str, Any] = {"id": prefix + sid, "type": s["type"], "status": "running", "started": now()}
            records_.append(rec)
            if on_step:
                on_step(rec)
            t0 = time.perf_counter()
            try:
                params = step_params(s, values)
                inputs = {}
                in_fps = {}
                for port, ref in (s.get("inputs") or {}).items():
                    if ref not in scope:
                        raise StepError(f"Input '{port}' takes '{ref}', which isn't there.")
                    inputs[port] = scope[ref]
                    in_fps[port] = fps.get(ref, "")
                if s["type"] == CALL:
                    out = self._call(s, params, inputs, in_fps, progress, depth)
                else:
                    st = step_type(s["type"])
                    key = ("step", st.id, _fp(params, in_fps))
                    out = self.cache.get(key)
                    rec["cached"] = out is not None
                    if out is None:
                        progress(f"{st.title}…")
                        out = st.run(ctx, inputs, params)
                        self._remember(key, out)
                step_fp = _fp(s["type"], params, in_fps)
                for port, value in out.outputs.items():
                    scope[f"{sid}.{port}"] = value
                    fps[f"{sid}.{port}"] = _fp(step_fp, port)
                rec.update(status="done", ms=int((time.perf_counter() - t0) * 1000), facts=jsonable(out.facts),
                           flags=len(out.flags), params=jsonable(params), _out=out)
            except StepError as e:
                rec.update(status="failed", error=str(e), ms=int((time.perf_counter() - t0) * 1000))
                raise
            except Exception as e:  # a bug or unexpected data: say where
                rec.update(status="failed", error=f"{type(e).__name__}: {e}", ms=int((time.perf_counter() - t0) * 1000))
                raise StepError(f"Step '{sid}' failed: {type(e).__name__}: {e}") from e
            finally:
                if on_step:
                    on_step(rec)
        return records_, scope, fps

    def _call(self, s: dict, params: dict, inputs: dict, in_fps: dict, progress, depth: int) -> StepOut:
        if depth >= MAX_DEPTH:
            raise StepError("Methods nest too deep (more than 4).")
        sub = self.store.get_method(params.get("method"), params.get("version"))
        sub_values = param_values(sub, {k: v for k, v in params.items() if k not in ("method", "version")})
        scope = {f"${k}": v for k, v in inputs.items()}
        fps = {f"${k}": v for k, v in in_fps.items()}
        recs, scope, fps = self.execute(sub, scope, fps, sub_values, progress, None, depth + 1)
        outputs = {}
        for name, o in (sub.get("outputs") or {}).items():
            src = o.get("from") if isinstance(o, dict) else o
            outputs[name] = scope.get(src)
        return StepOut(outputs, {"method": sub["id"], "version": sub["version"], "name": sub.get("name"),
                                 "steps": [{"id": r["id"], "type": r["type"], "ms": r.get("ms")} for r in recs]})

    def run_experiment(self, exp: dict, progress: Callable[[str], None] = lambda _m: None,
                       on_change: Callable[[dict], None] | None = None) -> dict[str, Any]:
        store = self.store
        method = store.get_method(exp["method"], exp.get("method_version"))
        probs = errors(validate(method, store))
        if probs:
            raise StepError("The method has problems: " + "; ".join(f"{p['step']}: {p['message']}" for p in probs))
        values = param_values(method, exp.get("params"))
        approved = [c for c in exp.get("corrections") or [] if c.get("status") == "approved"]
        scope: dict[str, Any] = {"$corrections": approved}
        fps: dict[str, str] = {"$corrections": _fp(approved)}
        for i in method.get("inputs") or []:
            f = (exp.get("inputs") or {}).get(i["id"])
            if f is None and not i.get("optional"):
                raise StepError(f"The experiment has no file for input '{i['id']}' ({i.get('label', '')}).")
            scope[f"${i['id']}"] = f
            fps[f"${i['id']}"] = (f or {}).get("sha256", "") if isinstance(f, dict) else _fp(f)
        run = {"id": new_id("r"), "experiment": exp["id"], "title": exp.get("title"), "status": "running",
               "started": now(), "finished": None,
               "method": {"id": method["id"], "version": method["version"], "name": method.get("name")},
               "params": values, "inputs": {k: {kk: v.get(kk) for kk in ("name", "sha256", "size")}
                                            for k, v in (exp.get("inputs") or {}).items() if isinstance(v, dict)},
               "corrections": approved, "steps": [], "outputs": method.get("outputs") or {},
               "versions": versions()}
        store.save_run(run)
        live: list[dict] = run["steps"]

        def on_step(rec: dict) -> None:
            if rec not in live:
                live.append(rec)
            if on_change:
                on_change({"run": run["id"], "experiment": exp["id"], "step": rec["id"], "status": rec["status"]})

        rdir = store.run_dir(run["id"])
        try:
            self.execute(method, scope, fps, values, progress, on_step)
            run["status"] = "done"
        except StepError as e:
            run["status"], run["error"] = "failed", str(e)
        # save outputs
        for rec in live:
            out: StepOut | None = rec.pop("_out", None)
            if out is None:
                continue
            rec["outputs"] = {}
            for port, value in out.outputs.items():
                name = f"{rec['id']}.{port}"
                if isinstance(value, pd.DataFrame):
                    value.to_csv(rdir / f"{name}.csv", index=False)
                    rec["outputs"][port] = {"kind": "table", "file": f"{name}.csv", "rows": len(value),
                                            "columns": [str(c) for c in value.columns]}
                elif isinstance(value, (bytes, bytearray)):
                    (rdir / f"{name}.png").write_bytes(value)
                    rec["outputs"][port] = {"kind": "figure", "file": f"{name}.png"}
                elif isinstance(value, (dict, list, str, int, float)) and port != "injections":
                    rec["outputs"][port] = {"kind": "value", "value": jsonable(value)}
                else:
                    rec["outputs"][port] = {"kind": "memory"}
        run["finished"] = now()
        flags_ref = next((o["from"] for n, o in run["outputs"].items() if n == "flags"), None)
        summary: dict[str, Any] = {"steps": len(live), "failed_step": next((r["id"] for r in live
                                                                             if r["status"] == "failed"), None)}
        if flags_ref and run["status"] == "done":
            try:
                fl = pd.read_csv(rdir / f"{flags_ref}.csv")
                summary["flags"] = fl.level.value_counts().to_dict() if len(fl) else {}
            except (OSError, pd.errors.EmptyDataError):
                summary["flags"] = {}
        run["summary"] = summary
        store.save_run(run)
        if run["status"] == "done":
            for name, o in run["outputs"].items():
                if isinstance(o, dict) and o.get("database"):
                    f = store.output_file(run, name)
                    if f and f.get("kind") == "table":
                        try:
                            df = pd.read_csv(rdir / f["file"])
                        except pd.errors.EmptyDataError:
                            continue
                        store.db_write(name, exp, run, df)
        exp = store.get_experiment(exp["id"])
        exp["last_run"] = {"id": run["id"], "status": run["status"], "started": run["started"],
                           "finished": run["finished"],
                           "summary": summary, "error": run.get("error")}
        store.save_experiment(exp)
        return run


def versions() -> dict[str, str]:
    import matplotlib
    import numpy
    import scipy

    return {"python": platform.python_version(), "pandas": pd.__version__, "numpy": numpy.__version__,
            "scipy": scipy.__version__, "matplotlib": matplotlib.__version__}


# ---------------------------------------------------------------------------------------------------- explaining


def _title(s: dict) -> str:
    if s["type"] == CALL:
        return s.get("label") or f"Run method {(s.get('params') or {}).get('method')}"
    st = REGISTRY.get(s["type"])
    return s.get("label") or (st.title if st else s["type"])


def explain_step(store: Store, method: dict, s: dict, values: dict, run_step: dict | None = None) -> dict[str, Any]:
    params = step_params(s, values)
    base: dict[str, Any] = {"step": s["id"], "type": s["type"], "title": _title(s), "note": s.get("note", ""),
                            "inputs": s.get("inputs") or {}}
    if s["type"] == CALL:
        sub = store.get_method(params.get("method"), params.get("version"))
        sub_values = param_values(sub, {k: v for k, v in params.items() if k not in ("method", "version")})
        base.update(sentence=f"Runs the method '{sub.get('name')}' (version {sub['version']}) as one step: "
                             f"{sub.get('description') or ''}",
                    formulas=[], symbols=[], assumptions=[], code="", reference="",
                    rules=[f"{i + 1}. {_title(x)}: {explain_step(store, sub, x, sub_values)['sentence']}"
                           for i, x in enumerate(sub.get("steps") or [])],
                    params=[{"id": k, "value": v} for k, v in params.items()])
    else:
        st = step_type(s["type"])
        base.update(sentence=st.sentence, **st.explain(params))
        base["params"] = [{"id": p.id, "label": p.label, "unit": p.unit, "value": jsonable(params.get(p.id)),
                           "from": (s.get("params") or {}).get(p.id) if isinstance((s.get("params") or {}).get(p.id),
                                                                                  str)
                           and str((s.get("params") or {}).get(p.id)).startswith("@") else None}
                          for p in st.params]
        if run_step and run_step.get("status") == "done" and st.example:
            try:
                ex = st.example(params, run_step.get("facts") or {})
            except Exception as e:  # an explanation must never break the page
                ex = [{"label": f"(couldn't fill in this run's numbers: {e})", "latex": ""}]
        else:
            ex = []
        if run_step:
            base["run"] = {"status": run_step.get("status"), "ms": run_step.get("ms"), "error": run_step.get("error"),
                           "examples": ex, "facts": run_step.get("facts") or {}, "outputs": run_step.get("outputs")}
    return base


def explain_method(store: Store, method: dict, values: dict | None = None, run: dict | None = None) -> list[dict]:
    values = param_values(method, values)
    by_id = {r["id"]: r for r in (run or {}).get("steps") or []}
    return [explain_step(store, method, s, values, by_id.get(s["id"])) for s in method.get("steps") or []]


def methods_text(store: Store, method: dict, values: dict | None = None, run: dict | None = None) -> str:
    """The Methods paragraph, one sentence per step, from the steps themselves (so text and computation agree)."""
    values = param_values(method, values)
    by_id = {r["id"]: r for r in (run or {}).get("steps") or []}
    parts = []
    for s in method.get("steps") or []:
        if s["type"] == CALL:
            sub = store.get_method((s.get("params") or {}).get("method"))
            parts.append(methods_text(store, sub, {k: v for k, v in step_params(s, values).items()
                                                   if k not in ("method", "version")}))
            continue
        st = REGISTRY.get(s["type"])
        if not st or not st.methods:
            continue
        try:
            parts.append(st.methods(step_params(s, values), (by_id.get(s["id"]) or {}).get("facts") or {}))
        except Exception:  # a sentence must not break the paragraph
            continue
    if run and run.get("versions"):
        v = run["versions"]
        parts.append(f"Calculations were done in Python {v.get('python')} (pandas {v.get('pandas')}, NumPy "
                     f"{v.get('numpy')}, SciPy {v.get('scipy')}) with the layr Research plugin, method "
                     f"'{method.get('name')}' version {method.get('version')}.")
    return " ".join(p.strip() for p in parts if p and p.strip())


def provenance(store: Store, run: dict, table: str, where: dict[str, Any]) -> list[dict[str, Any]]:
    """Where a result row came from: for every step (in order) whose table has one row matching `where`, that
    step's formula with that row's numbers in it."""
    method = store.get_method(run["method"]["id"], run["method"]["version"])
    values = param_values(method, run.get("params"))
    target = store.output_file(run, table)
    if not target:
        raise StepError(f"No table '{table}' in run {run['id']}.")
    target_step = (run["outputs"].get(table) or {}).get("from", table).split(".")[0] if table in run["outputs"] \
        else table.split(".")[0]
    rdir = store.run_dir(run["id"])
    chain = []
    for rec in run.get("steps") or []:
        st = REGISTRY.get(rec["type"])
        for port, o in (rec.get("outputs") or {}).items():
            if o.get("kind") != "table" or not all(c in o.get("columns", []) for c in where):
                continue
            df = pd.read_csv(rdir / o["file"])
            m = df
            for c, v in where.items():
                m = m[m[c].astype(str) == str(v)]
            if len(m) != 1:
                continue
            row = records(m)[0]
            step = next((s for s in method["steps"] if s["id"] == rec["id"]), {"id": rec["id"], "type": rec["type"]})
            ex = []
            if st and st.example:
                try:
                    ex = st.example(step_params(step, values), {**(rec.get("facts") or {}), "example": row})
                except Exception:
                    ex = []
            chain.append({"step": rec["id"], "title": _title(step), "table": f"{rec['id']}.{port}", "row": row,
                          "examples": ex})
            break
        if rec["id"] == target_step:
            break
    return chain

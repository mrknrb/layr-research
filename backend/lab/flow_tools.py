"""MCP tools for the flow: the recipe drawn as a graph, changes made in the flow as edits of the code, a new method.

All three are pure: they read methods, experiments and runs but save nothing. The panel saves an edited source
through `method_write` (her saves count at once; an agent's are drafts), so versions and drafts work as before.

What `register` needs from `api` (filled by `tools.py`):
- `api.pick_run(run, experiment) -> dict`: a run by id, or the experiment's latest run (raises LabError when there
  is none);
- `api.method_of(experiment_dict) -> dict`: the method (with `source`) an experiment uses.
"""

from __future__ import annotations

import difflib
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import checker, explain, flow, methodfile, values
from .errors import LabError
from .store import Store


def _diff(old: str, new: str) -> list[str]:
    return [line.rstrip("\n") for line in difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after",
                                                                lineterm="")]


def run_lines(store: Store, run: dict, source: str | None) -> list[dict[str, Any]]:
    """The recipe's own statements as the run recorded them inside the recipe call (its inside.json): [{line,
    end_line, names, n, rounds: [{round, values}]}], lines as in the run's method.py. Empty for a run from before
    0.4 or one whose values inside were cleared."""
    record = values.inside_of(store, run["id"])
    if not record:
        return []
    inside = record.get("calls") or {}
    calls = explain.calls_of(store, run["id"]) or {}
    rc = calls.get("recipe_call")
    entry = inside.get(str(rc)) if rc is not None else None
    if entry is None and source:
        try:
            recipe = methodfile.parse(source).recipe_function
        except SyntaxError:
            recipe = None
        if recipe is not None:
            entry = next((c for c in inside.values() if c.get("function") == recipe.name), None)
    return (entry or {}).get("lines") or []


def register(mcp: FastMCP, store: Store, api: Any) -> None:
    def run_with_source(run: dict) -> dict:
        p = store.run_dir(run["id"]) / "method.py"
        source = p.read_text("utf-8") if p.exists() else None
        return {**run, "source": source, "lines": run_lines(store, run, source)}

    def context(method: str | None, version: int | None, source: str | None, experiment: str | None,
                run: str | None) -> tuple[str, dict | None, dict | None, dict | None]:
        """The source to draw, the experiment, the run (with the code it ran) and the stored method."""
        exp = store.get_experiment(experiment) if experiment else None
        r = None
        if run:
            r = run_with_source(api.pick_run(run, None))
        elif exp is not None and exp.get("last_run"):
            try:
                r = run_with_source(api.pick_run(None, experiment))
            except LabError:
                r = None
        stored = None
        if source is None:
            if method:
                stored = store.get_method(method, version)
            elif exp is not None:
                stored = api.method_of(exp)
            elif r is not None and r.get("source"):
                source = r["source"]
            else:
                raise LabError("Give a method (or its source), an experiment or a run.")
            if stored is not None:
                source = stored["source"]
        return source, exp, r, stored

    def described(src: str, exp: dict | None, r: dict | None, stored: dict | None) -> dict[str, Any]:
        g = flow.graph(src, r, exp)
        if stored is not None:
            g["method"].update(id=stored.get("id"), version=stored.get("version"))
        if exp is not None:
            g["experiment"] = {"id": exp["id"], "title": exp.get("title")}
        return g

    @mcp.tool()
    def flow_get(
        method: Annotated[str | None, Field(description="Method id, e.g. survey-scores.")] = None,
        version: Annotated[int | None, Field(description="Its version; default the latest.")] = None,
        source: Annotated[str | None, Field(description="Or the method's source (e.g. unsaved in the editor).")]
        = None,
        experiment: Annotated[str | None, Field(description="Show this experiment's settings and its latest run "
                                                            "(and draw its method when no method is given).")] = None,
        run: Annotated[str | None, Field(description="Or this run's values.")] = None,
    ) -> dict[str, Any]:
        """The method's recipe as a flow: nodes (one per recipe line: a step call, or a code box for anything
        else) with their ports (wire, setting, input or a value written in place), types, the columns each step
        reads and those missing from the incoming table; the inputs, settings (with the experiment's values and
        where each is used), results, groups and the quality lane. With a run: each node's status, time, error and
        the value summaries on its wires; `stale` when the method changed since that run."""
        src, exp, r, stored = context(method, version, source, experiment, run)
        return described(src, exp, r, stored)

    @mcp.tool()
    def flow_edit(
        ops: Annotated[list[dict[str, Any]], Field(description="The changes, applied in order. Each is "
                                                               "{op, ...}; op is one of " + ", ".join(flow.OPS)
                                                               + ". See the flow module for each op's fields.")],
        source: Annotated[str | None, Field(description="The method's source to change (e.g. unsaved in the "
                                                        "editor).")] = None,
        method: Annotated[str | None, Field(description="Or a method id (its latest version, or `version`).")]
        = None,
        version: Annotated[int | None, Field(description="The version to start from.")] = None,
        experiment: Annotated[str | None, Field(description="Draw the result with this experiment's settings and "
                                                            "latest run.")] = None,
        run: Annotated[str | None, Field(description="Or with this run's values.")] = None,
    ) -> dict[str, Any]:
        """Change the method as a change in the flow would (add a step, connect, rename a wire, add a setting or a
        result, group steps...): exact edits of the recipe's lines, comments kept, lines wrapped at 120 columns.
        Saves nothing: returns {source, flow, check, ok, diff}; save the source with method_write (from an AI agent
        that is a draft the user accepts). Several ops in one call apply in order. A change that would break the
        method is refused in plain words. Ops and their fields: add_step {function, args: {param: wire name |
        {setting|input|value|code: ...}}, out?, after?}, remove_step {node}, connect {node, port, wire|setting|
        input|value|code}, disconnect {node, port} (back to its default), rename_wire {wire, to, node?},
        add_step also takes outs: [names] for a step that gives several,
        add_setting {name, default?|code?, label?, help?, unit?, type?, group?}, remove_setting {setting},
        set_default {setting, value|code}, add_input {name (ends in _file), label?, types?, optional?},
        remove_input {input}, add_result {wire, name?, label?}, remove_result {name}, new_function {name, params?,
        sentence?, body?}, group {nodes, title}, ungroup {group}, add_code {code | lines, after?}."""
        if source is None and method is None and experiment is None:
            raise LabError("Give the source to change, a method id, or an experiment (its method).")
        src, exp, r, stored = context(method, version, source, experiment, run)
        if not isinstance(ops, list) or not ops:
            raise LabError("Give at least one change in `ops`.")
        new = flow.edit_all(src, ops)
        result = checker.check(new)
        return {"source": new, "flow": described(new, exp, r, stored), "check": result["messages"],
                "ok": result["ok"], "diff": _diff(src, new)}

    @mcp.tool()
    def flow_new(
        name: Annotated[str, Field(description="The method's name people read, e.g. 'Growth rate from OD600'.")],
        description: Annotated[str, Field(description="One or two sentences: what it measures and how.")] = "",
    ) -> dict[str, Any]:
        """A starting method: its name, description and an empty recipe, ready to be built in the flow (add a raw
        file, then steps). Saves nothing: returns {source, flow, check}; create it with method_create(source=...)."""
        src = flow.new_method(name, description)
        result = checker.check(src)
        return {"source": src, "flow": flow.graph(src), "check": result["messages"], "ok": result["ok"]}

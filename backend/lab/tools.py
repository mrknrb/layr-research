"""MCP tools for methods (Python files), experiments, runs, corrections, explanations and the results database.

Rules the tools enforce (also in the server's instructions for agents):
- Numbers come only from runs: an agent reads results with result_table / provenance and cites the run id.
- A method is a Python file. The user's own saves in the panel count at once (a new version); a change written by
  an AI agent is a draft until the user accepts it in the panel (draft_decide answers only for the user).
- Corrections (renames, exclusions, manual peak boundaries) always carry a reason; one made by an agent is only
  proposed until the user approves it in the panel.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from mcp.server.fastmcp import Context, FastMCP, Image
from pydantic import Field

from . import checker, compare, methodfile, standalone
from . import explain as explainer
from .errors import LabError
from .gcassay_import import corrections_from, params_from_config, parse_yaml
from .runner import Pool, Runner, experiment_inputs
from .store import Store, method_name, new_id, now, records

LIVE: dict[str, dict[str, Any]] = {}  # experiment id -> the run in progress
TASKS: set[asyncio.Future] = set()
GAML_LOCK = threading.Lock()


def _caller(ctx: Context | None) -> dict[str, Any]:
    try:
        meta = ctx.request_context.meta if ctx else None
    except (ValueError, AttributeError):
        return {}
    if meta is None:
        return {}
    extra = getattr(meta, "model_extra", None) or {}
    c = extra.get("layr/caller") or getattr(meta, "layr/caller", None)
    return c if isinstance(c, dict) else {}


def _who(ctx: Context | None) -> str:
    c = _caller(ctx)
    if c.get("kind") == "agent":
        return f"AI agent ({c.get('label') or c.get('plugin') or 'agent'})"
    if c.get("kind") == "panel" and c.get("plugin") == os.environ.get("LAYR_PLUGIN_ID", "research"):
        return "you (Research panel)"
    if c.get("kind"):
        return f"{c.get('kind')} {c.get('plugin') or ''}".strip()
    return "unknown caller"


def _is_user(ctx: Context | None) -> bool:
    c = _caller(ctx)
    return c.get("kind") in ("panel", "shell") and c.get("plugin", os.environ.get("LAYR_PLUGIN_ID", "research")) \
        == os.environ.get("LAYR_PLUGIN_ID", "research")


def unified_diff(old: str, new: str, old_label: str = "before", new_label: str = "after") -> list[str]:
    return [line.rstrip("\n") for line in difflib.unified_diff(old.splitlines(), new.splitlines(), old_label,
                                                                new_label, lineterm="")]


def replace_function(source: str, name: str, code: str) -> str:
    """The source with one function replaced by `code` (or added before the recipe when it is new)."""
    try:
        new_tree = methodfile.parse(code)
    except SyntaxError as e:
        raise LabError(f"The new code of `{name}` can't be read (line {e.lineno}): {e.msg}.") from e
    defined = [f.name for f in new_tree.functions]
    if defined != [name]:
        raise LabError(f"`code` must define exactly the function `{name}` "
                       f"(it defines: {', '.join(defined) or 'none'}).")
    method = methodfile.parse(source)
    lines = source.splitlines()
    code_lines = code.strip("\n").splitlines()
    fn = method.function(name)
    if fn is not None:
        lines[fn.line - 1:fn.end_line] = code_lines
    else:
        recipe = method.recipe_function
        at = (recipe.line - 1) if recipe else len(lines)
        lines[at:at] = code_lines + ["", ""]
    return "\n".join(lines) + "\n"


def copy_from_toolbox(source: str, name: str) -> str:
    """The method source with a toolbox function copied in (after the imports), with the imports it needs."""
    import ast
    import builtins
    import importlib
    import inspect

    import toolbox

    if name not in toolbox.__all__:
        raise LabError(f"The toolbox has no `{name}`. It has: {', '.join(toolbox.__all__)}.")
    obj = getattr(toolbox, name)
    if not inspect.isfunction(obj):
        raise LabError(f"`{name}` is not a function: only functions can be copied (import it as it is).")
    module = importlib.import_module(obj.__module__)
    code = inspect.getsource(obj)
    free = {n.id for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    needs = []
    for n in sorted(free):
        if n == name or not hasattr(module, n):
            continue
        v = getattr(module, n)
        if inspect.ismodule(v):
            alias = {"numpy": "np", "pandas": "pd"}.get(v.__name__)
            needs.append(f"import {v.__name__} as {n}" if alias == n else f"import {v.__name__}"
                         if v.__name__ == n else f"import {v.__name__} as {n}")
        elif not hasattr(builtins, n):
            needs.append(f"from {obj.__module__} import {n}")
    lines = source.splitlines()
    have = set(line.strip() for line in lines)
    tree = ast.parse(source)
    last_import = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node.end_lineno or node.lineno
    add = [n for n in needs if n not in have]
    block = add + ["", "", f"# Copied from the toolbox ({obj.__module__}); this copy is used instead of it.",
                   *code.rstrip("\n").splitlines()]
    lines[last_import:last_import] = block if last_import else block[1:]
    return "\n".join(lines) + "\n"


NEW_METHOD = '''"""{name}

{description}
"""

from toolbox import read_table


def relative_value(value, control_mean):
    """A value divided by the mean of the control group."""
    return value / control_mean


def relative_to_control(table, value_column, group_column, control):
    """Divide every value by the mean value of the control group."""
    control_mean = table[table[group_column] == control][value_column].mean()
    result = table.copy()
    result["relative"] = relative_value(result[value_column], control_mean)
    return result


def analysis(table_file, value_column="value", group_column="group", control="control"):
    """{name}.

    Inputs:
        table_file: The measurements (CSV, TSV or Excel) [.csv, .tsv, .xlsx]

    Settings:
        value_column: Value column. The column with the numbers.
        group_column: Group column. The column that says which group a row is in.
        control: Control group. The group the others are compared with.

    Returns:
        relative: Values relative to the control
    """
    table = read_table(table_file)
    relative = relative_to_control(table, value_column, group_column, control)
    return {{"relative": relative}}
'''


def register(mcp: FastMCP, data_dir: Path, emit) -> Store:
    store = Store(data_dir / "lab")
    pool = Pool()
    runner = Runner(store, pool)
    threading.Thread(target=pool.warm, daemon=True).start()
    gaml_cache: dict[str, Any] = {}

    def changed(what: str, **data: Any) -> None:
        emit("research:lab", {"what": what, **data})

    def parsed(source: str) -> methodfile.Method:
        try:
            return methodfile.parse(source)
        except SyntaxError as e:
            raise LabError(f"The method can't be read (line {e.lineno}): {e.msg}.") from e

    def method_of(e: dict) -> dict:
        return store.get_method(e["method"], e.get("method_version"))

    def experiment_summary(e: dict) -> dict:
        try:
            mname = method_of(e)["name"]
        except LabError:
            mname = e["method"] + " (missing)"
        pending = sum(1 for c in e.get("corrections") or [] if c.get("status") == "proposed")
        return {"experiment": e["id"], "title": e.get("title"), "method": e["method"], "method_name": mname,
                "files": {k: v.get("name") for k, v in (e.get("inputs") or {}).items() if isinstance(v, dict)},
                "last_run": e.get("last_run"), "running": e["id"] in LIVE, "proposed_corrections": pending,
                "updated": e.get("updated")}

    def latest_run(exp_id: str) -> dict:
        e = store.get_experiment(exp_id)
        if not e.get("last_run"):
            raise LabError(f"Experiment {exp_id} has not been run yet: call run first.")
        return store.get_run(e["last_run"]["id"])

    def pick_run(run: str | None, experiment: str | None) -> dict:
        if run:
            return store.get_run(run)
        if experiment:
            return latest_run(experiment)
        raise LabError("Give a run id, or an experiment id for its latest run.")

    # ------------------------------------------------------------------ comparisons on the reference experiment

    def start_comparison(method_id: str, after_method: dict, base_version: int | None, where: str,
                         version: int | None = None) -> dict | None:
        """Rerun the method's reference experiment with a change and compare with the version before. The result
        goes into the draft (where="draft") or the new version's facts (where="version")."""
        ref = store.method_settings(method_id).get("reference")
        if not ref:
            return None
        try:
            exp = store.get_experiment(ref)
        except LabError:
            return None
        state = {"status": "running", "experiment": ref, "started": now()}

        def save(result: dict) -> None:
            if where == "draft":
                store.update_draft(method_id, comparison=result)
            elif version is not None:
                store.set_version_meta(method_id, version, comparison=result)
            changed("comparison", method=method_id, version=version, draft=where == "draft")

        def work() -> None:
            try:
                before = None
                for r in store.list_runs(ref, trials=True):
                    if r.get("status") == "done" and (r.get("method") or {}).get("version") == base_version \
                            and not (r.get("method") or {}).get("draft"):
                        before = store.get_run(r["id"])
                        break
                if before is None:
                    base = store.get_method(method_id, base_version)
                    before = runner.run(exp, base, trial=True, reason=f"comparison base (version {base_version})")
                after = runner.run(exp, after_method, trial=True, reason="comparison after a change")
                result = compare.compare_runs(store, before, after)
                result.update(status="done", experiment=ref, experiment_title=exp.get("title"), finished=now())
                save(result)
            except Exception as e:  # a comparison must never break a save
                save({"status": "failed", "experiment": ref, "error": str(e), "finished": now()})

        save(state)
        threading.Thread(target=work, daemon=True).start()
        return state

    # ------------------------------------------------------------------ methods

    @mcp.tool()
    def methods() -> list[dict[str, Any]]:
        """The analysis methods (Python files): id, name, latest version, number of steps, raw file inputs, built in
        or not, whether an AI draft waits for the user, the reference experiment."""
        return store.list_methods()

    def method_view(method: str, version: int | None = None) -> dict[str, Any]:
        draft = store.get_draft(method)
        if draft and store.draft_only(method):
            name, description = method_name(draft["source"], method)
            m = {"id": method, "version": 0, "builtin": False, "name": name, "description": description,
                 "source": draft["source"], "draft_only": True}
        else:
            m = store.get_method(method, version)
        result = checker.check(m["source"])
        return {**m, "check": result["messages"], "summary": result["summary"],
                "versions": store.method_versions(method), "reference": store.method_settings(method).get("reference"),
                "draft": None if not draft else {k: v for k, v in draft.items() if k != "source"}}

    @mcp.tool()
    def method_get(method: Annotated[str, Field(description="Method id, e.g. gc-activity-assay.")],
                   version: Annotated[int | None, Field(description="A version; default the latest.")] = None
                   ) -> dict[str, Any]:
        """A method in full: its Python source, the outline of its functions (recipe, steps, formulas, helpers),
        its settings and inputs (from the recipe's signature and docstring), the style checker's messages, its
        versions, its reference experiment and a pending AI draft (if any)."""
        return method_view(method, version)

    @mcp.tool()
    def method_check(source: Annotated[str, Field(description="A method file's full Python source.")]
                     ) -> dict[str, Any]:
        """Check a method's source without saving it: errors (it can't be read, an import can't work, no recipe)
        and style warnings, each with its line; plus the outline, settings and inputs when it can be read."""
        return checker.check(source)

    @mcp.tool()
    def method_versions(method: Annotated[str, Field(description="Method id.")]) -> list[dict[str, Any]]:
        """Every saved version of a method: number, when, by whom, why, and how the reference experiment's numbers
        moved with it (when it has a reference)."""
        return store.method_versions(method)

    @mcp.tool()
    def method_diff(method: Annotated[str, Field(description="Method id.")],
                    from_version: Annotated[int, Field(description="Older version.")],
                    to_version: Annotated[int | None, Field(description="Newer version; default latest.")] = None,
                    draft: Annotated[bool, Field(description="true: compare with the pending AI draft.")] = False
                    ) -> list[str]:
        """The changed lines between two versions of a method (unified diff: - removed, + added)."""
        old = store.get_method(method, from_version)["source"]
        if draft:
            d = store.get_draft(method)
            if d is None:
                raise LabError(f"Method {method} has no draft.")
            return unified_diff(old, d["source"], f"version {from_version}", "draft")
        new = store.get_method(method, to_version)
        return unified_diff(old, new["source"], f"version {from_version}", f"version {new['version']}")

    @mcp.tool()
    def method_create(
        method: Annotated[str, Field(description="New id: lowercase letters, digits and dashes, e.g. growth-rate.")],
        name: Annotated[str, Field(description="Name people read, e.g. 'Growth rate from OD600'.")],
        description: Annotated[str, Field(description="One or two sentences: what it measures and how.")] = "",
        copy_of: Annotated[str | None, Field(description="Start as a copy of this method id.")] = None,
        source: Annotated[str | None, Field(description="Or the whole Python source of the new method.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a new method: a copy of another, the given source, or a small starting template (a table, a value
        relative to a control group). From the panel it is version 1 at once; from an AI agent it is a draft the
        user accepts first. Returns the checker's messages."""
        if any(m["id"] == method for m in store.list_methods()):
            raise LabError(f"A method '{method}' exists already; change it with method_write.")
        if source is None:
            if copy_of:
                source = store.get_method(copy_of)["source"]
                first, _, rest = source.partition("\n")
                if source.startswith('"""'):
                    source = f'"""{name}\n' + rest
            else:
                source = NEW_METHOD.format(name=name, description=description or name)
        result = checker.check(source)
        if not result["ok"]:
            return {"saved": False, "messages": result["messages"]}
        change = f"Created{f' as a copy of {copy_of}' if copy_of else ''}"
        if _is_user(ctx):
            saved = store.save_method(method, source, _who(ctx), change)
            changed("method", method=method)
            return {"saved": True, "method": method, "version": saved["version"], "messages": result["messages"]}
        store.save_draft(method, source, {"by": _who(ctx), "at": now(), "change": change, "base_version": None,
                                          "messages": result["messages"], "new_method": True})
        changed("method", method=method, draft=True)
        return {"saved": False, "draft": True, "method": method, "messages": result["messages"],
                "note": "Saved as a draft: the user accepts it in the Research panel (Methods)."
                        + (" Fix the warnings first." if result["messages"] else "")}

    @mcp.tool()
    def method_write(
        method: Annotated[str, Field(description="Method id.")],
        change: Annotated[str, Field(description="One sentence: what changed and why, for the version history.")],
        source: Annotated[str | None, Field(description="The whole new Python source of the method.")] = None,
        function: Annotated[str | None, Field(description="Or: the one function to replace (or add, before the "
                                                          "recipe, when it is new)...")] = None,
        code: Annotated[str | None, Field(description="...and its complete new code (def line to the end).")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Change a method: the whole file, or one function. It is checked first: with errors (code Python can't
        read, an import that can't work) nothing is saved. From the user's panel it becomes a new version at once;
        from an AI agent it is saved as a draft that counts only when the user accepts it, and the style warnings
        come back: fix them with another method_write. Returns the messages and the changed lines; with a reference
        experiment, it is rerun with the change and the moved numbers appear in draft_get / method_versions."""
        user = _is_user(ctx)
        new_method = store.draft_only(method)
        current = {"id": method, "version": 0, "source": ""} if new_method else store.get_method(method)
        draft = store.get_draft(method)
        base_source = draft["source"] if (draft and not user) else current["source"]
        if source is None:
            if not function or code is None:
                raise LabError("Give the whole `source`, or `function` and its `code`.")
            new_source = replace_function(base_source, function, code)
        else:
            new_source = source if source.endswith("\n") else source + "\n"
        result = checker.check(new_source)
        lines = unified_diff(current["source"], new_source, f"version {current['version']}",
                             "new version" if user else "draft")
        if not result["ok"]:
            return {"saved": False, "messages": result["messages"], "diff": lines,
                    "note": "Not saved: fix the errors first."}
        if new_source == current["source"] and not (draft and not user):
            return {"saved": False, "messages": result["messages"], "diff": [], "note": "Nothing changed."}
        if user:
            saved = store.save_method(method, new_source, _who(ctx), change.strip() or "Changed in the editor")
            if new_method:
                store.drop_draft(method)
            reference = None
            if current["version"]:
                reference = start_comparison(method, saved, current["version"], "version", saved["version"])
            changed("method", method=method, version=saved["version"])
            return {"saved": True, "version": saved["version"], "messages": result["messages"], "diff": lines,
                    "reference": reference}
        meta = {"by": _who(ctx), "at": now(), "change": change.strip(), "base_version": current["version"],
                "messages": result["messages"]}
        store.save_draft(method, new_source, meta)
        draft_method = {**current, "source": new_source, "draft": True}
        reference = start_comparison(method, draft_method, current["version"], "draft") if current["version"] else None
        changed("method", method=method, draft=True)
        return {"saved": False, "draft": True, "messages": result["messages"], "diff": lines, "reference": reference,
                "note": "Saved as a draft: it counts once the user accepts it in the Research panel."
                        + (" Fix the warnings above first (method_write again)." if result["messages"] else "")}

    @mcp.tool()
    def method_revert(method: Annotated[str, Field(description="Method id.")],
                      version: Annotated[int, Field(description="The version to go back to.")],
                      ctx: Context | None = None) -> dict[str, Any]:
        """Go back to an earlier version of a method (saved as a new version, so nothing is lost; from an AI agent
        it is a draft)."""
        old = store.get_method(method, version)
        return method_write(method, f"Back to version {version}", source=old["source"], ctx=ctx)

    @mcp.tool()
    def method_reference(method: Annotated[str, Field(description="Method id.")],
                         experiment: Annotated[str | None, Field(description="The experiment to rerun after every "
                                                                             "change (null: none).")]) -> dict:
        """Pin an experiment as the method's reference: after every change it is rerun with the new code and the
        numbers that moved are listed (in method_versions, and in draft_get for an AI draft)."""
        store.get_method(method)
        if experiment:
            e = store.get_experiment(experiment)
            if e["method"] != method:
                raise LabError(f"Experiment {experiment} uses the method {e['method']}, not {method}.")
        s = store.set_method_settings(method, reference=experiment)
        changed("method", method=method)
        return {"method": method, "reference": s.get("reference")}

    @mcp.tool()
    def draft_get(method: Annotated[str, Field(description="Method id.")]) -> dict[str, Any]:
        """A method's pending AI draft: who wrote it and why, the checker's messages, the changed lines against the
        current version, and how the reference experiment's numbers moved (status running until it is done)."""
        d = store.get_draft(method)
        if d is None:
            return {"draft": None}
        try:
            current = store.get_method(method)
            lines = unified_diff(current["source"], d["source"], f"version {current['version']}", "draft")
        except LabError:
            current, lines = None, unified_diff("", d["source"], "nothing", "draft")
        return {"draft": {**d, "diff": lines, "current_version": current["version"] if current else None}}

    @mcp.tool()
    def draft_decide(method: Annotated[str, Field(description="Method id.")],
                     decision: Annotated[Literal["accept", "reject"], Field(description="accept: it becomes the "
                                                                                         "next version")],
                     ctx: Context | None = None) -> dict[str, Any]:
        """The user's decision on an AI draft (only the user decides, in the Research panel)."""
        if not _is_user(ctx):
            raise LabError("Only the user accepts or rejects a draft, in the Research panel (Methods).")
        d = store.get_draft(method)
        if d is None:
            raise LabError(f"Method {method} has no draft.")
        store.drop_draft(method)
        if decision == "reject":
            changed("method", method=method)
            return {"rejected": True}
        saved = store.save_method(method, d["source"], f"{d.get('by')}, accepted by {_who(ctx)}",
                                  d.get("change") or "AI change", {"comparison": d.get("comparison")})
        changed("method", method=method, version=saved["version"])
        return {"accepted": True, "version": saved["version"]}

    @mcp.tool()
    def method_delete(method: Annotated[str, Field(description="Method id.")]) -> dict[str, Any]:
        """Delete a method made here and all its versions (built-in methods return to their shipped version)."""
        used = [e["id"] for e in store.list_experiments() if e.get("method") == method]
        builtin = method in {m["id"] for m in store.list_methods() if m.get("builtin")}
        if used and not builtin:
            raise LabError(f"Experiments use this method: {', '.join(used)}.")
        store.delete_method(method)
        changed("method", method=method, deleted=True)
        return {"deleted": method}

    # ------------------------------------------------------------------ explaining code

    def source_for(method: str | None, version: int | None, source: str | None, run: dict | None) -> str:
        if source:
            return source
        if run is not None and not method:
            s = explainer.run_source(store, run)
            if s:
                return s
        if method:
            return store.get_method(method, version)["source"]
        raise LabError("Give a method (or its source), or a run.")

    @mcp.tool()
    def explain_function(
        function: Annotated[str, Field(description="A function of the method, or a toolbox function.")],
        method: Annotated[str | None, Field(description="Method id.")] = None,
        version: Annotated[int | None, Field(description="Its version; default the latest.")] = None,
        source: Annotated[str | None, Field(description="Or the method's source (e.g. unsaved in the editor).")]
        = None,
        experiment: Annotated[str | None, Field(description="Show this experiment's latest run's numbers.")] = None,
        run: Annotated[str | None, Field(description="Or this run's numbers.")] = None,
        call: Annotated[int | None, Field(description="A recorded call's id to show (default the first).")] = None,
    ) -> dict[str, Any]:
        """One function explained from its code: its sentence, its formula drawn as math (LaTeX), its code, and
        with a run the recorded calls (inputs and result, the formula with those numbers put in, how many times it
        ran) and for a step its input and output tables."""
        r = None
        if run or experiment:
            try:
                r = pick_run(run, experiment)
            except LabError:
                r = None
        calls = explainer.calls_of(store, r["id"]) if r else None
        src = source_for(method, version, source, r)
        m = parsed(src)
        if m.function(function) is None:
            return explainer.toolbox_view(function, calls)
        return explainer.function_view(m, function, calls, r, call)

    @mcp.tool()
    def explain(
        experiment: Annotated[str | None, Field(description="Explain this experiment's latest run (or its method "
                                                            "when it hasn't run).")] = None,
        run: Annotated[str | None, Field(description="Or this run.")] = None,
        method: Annotated[str | None, Field(description="Or a method in general (no numbers).")] = None,
    ) -> dict[str, Any]:
        """How every number is calculated: each step in the recipe's order with its sentence, its code, its input
        and output tables (with a run) and the formulas used inside it, drawn as math with this run's numbers.
        Use it to explain an analysis to the user in their words."""
        if run or experiment:
            try:
                r = pick_run(run, experiment)
                return explainer.explain_run(store, r)
            except LabError:
                if not experiment:
                    raise
                method = store.get_experiment(experiment)["method"]
        if not method:
            raise LabError("Give an experiment, a run or a method.")
        m = store.get_method(method)
        return {"method": {"id": m["id"], "version": m["version"]}, "run": None,
                **explainer.explain_method(parsed(m["source"])), "source": m["source"]}

    @mcp.tool()
    def toolbox_docs(name: Annotated[str | None, Field(description="One toolbox function; default all.")] = None
                     ) -> dict[str, Any]:
        """The toolbox's ready-made functions (reading GAML and tables, peaks, straight lines, flags, plots):
        {functions: [{name, signature, sentence, docstring text, formulas, code}]}. Methods import them:
        `from toolbox import fit_line`."""
        return {"functions": [explainer.toolbox_view(name)] if name else explainer.toolbox_docs()}

    @mcp.tool()
    def toolbox_copy(name: Annotated[str, Field(description="The toolbox function to copy.")],
                     source: Annotated[str, Field(description="The method's current source.")]) -> dict[str, Any]:
        """The method's source with a toolbox function copied in (with the imports it needs), to change it: the
        copy is used instead of the toolbox's. Nothing is saved; save it with method_write."""
        new = copy_from_toolbox(source, name)
        return {"source": new, "messages": checker.check(new)["messages"]}

    @mcp.tool()
    def ai_prompt(
        kind: Annotated[Literal["explain", "methods"], Field(description="explain: a function or selected lines in "
                                                                         "plain words · methods: a Methods paragraph "
                                                                         "for a paper")],
        method: Annotated[str | None, Field(description="Method id.")] = None,
        source: Annotated[str | None, Field(description="Or the method's source.")] = None,
        function: Annotated[str | None, Field(description="explain: the function.")] = None,
        code: Annotated[str | None, Field(description="explain: or the selected lines.")] = None,
        experiment: Annotated[str | None, Field(description="methods: the experiment (its settings, files).")]
        = None,
    ) -> dict[str, Any]:
        """For the panel: the prompt it sends to Claude (the claude plugin's ask tool) for an AI explanation or a
        Methods paragraph, built from the code and settings only."""
        e = store.get_experiment(experiment) if experiment else None
        if source is None:
            mid = method or (e["method"] if e else None)
            if not mid:
                raise LabError("Give a method, its source, or an experiment.")
            source = store.get_method(mid, e.get("method_version") if e else None)["source"]
        if kind == "explain":
            target = code or ""
            if function and not code:
                fn = parsed(source).function(function)
                target = fn.source if fn else function
            system = ("You explain Python code to a microbiology PhD student who is learning Python. Use plain "
                      "words, short sentences and her lab's terms; say what each part does and why, in the order it "
                      "runs. Name pandas and Python patterns simply when they appear (picking rows, for each group, "
                      "making a table). Do not invent behaviour the code doesn't have. No more than 250 words.")
            prompt = (f"Explain this part of an analysis method:\n\n```python\n{target}\n```\n\nThe whole method, for "
                      f"context:\n\n```python\n{source}\n```")
            return {"prompt": prompt, "system": system}
        settings = {}
        if e:
            settings = {**{s["id"]: s["default"] for s in methodfile.settings(parsed(source))}, **(e.get("params")
                                                                                                    or {})}
        last = e.get("last_run") if e else None
        versions = ""
        if last:
            try:
                v = store.get_run(last["id"]).get("versions") or {}
                versions = ", ".join(f"{k} {x}" for k, x in v.items())
            except LabError:
                pass
        system = ("You write the Methods section of a scientific paper in microbiology. Write one paragraph in the "
                  "past tense, precise and concise, stating only what the code and settings below actually do "
                  "(no results). Mention the software (Python and the listed package versions; 'the layr Research "
                  "plugin'). Where something can't be known from the code (instrument, sample preparation), leave "
                  "a placeholder in [brackets].")
        prompt = (f"Method source:\n\n```python\n{source}\n```\n\nSettings used: {settings}\n\nSoftware: {versions}"
                  "\n\nWrite the Methods paragraph.")
        return {"prompt": prompt, "system": system}

    # ------------------------------------------------------------------ files

    def import_path(path: str) -> dict:
        p = Path(path).expanduser()
        if not p.is_file():
            raise LabError(f"No file at {p}.")
        return store.add_file(p)

    @mcp.tool()
    async def file_import(path: Annotated[str, Field(description="Absolute path of a file on this PC (GAML, CSV, "
                                                                 "TSV, Excel).")]) -> dict[str, Any]:
        """Copy a raw data file into the plugin's store (by checksum; the original is never changed). Returns a file
        reference to give experiment_create / experiment_update as an input."""
        return await asyncio.to_thread(import_path, path)

    @mcp.tool()
    async def file_fetch(url: Annotated[str, Field(description="A layr file address from the Research panel "
                                                               "(layr.files.url).")],
                         name: Annotated[str, Field(description="The file's name, e.g. run5.gaml.")]
                         ) -> dict[str, Any]:
        """For the Research panel: store a file the user picked, read through layr's file route. Returns its file
        reference."""
        parsed_url = urllib.parse.urlsplit(url)
        hub = urllib.parse.urlsplit(os.environ.get("LAYR_HUB_URL") or "http://127.0.0.1:4300/mcp")
        if not parsed_url.path.startswith("/_layr/files/"):
            raise LabError("That is not a layr file address.")
        target = f"{hub.scheme}://{hub.netloc}{parsed_url.path}?{parsed_url.query}"
        tmp = store.root / "uploads" / new_id("u")

        def fetch() -> dict:
            with urllib.request.urlopen(target, timeout=600) as resp, open(tmp, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
            return store.add_file(tmp, name, move=True)

        return await asyncio.to_thread(fetch)

    @mcp.tool()
    def upload_chunk(upload: Annotated[str | None, Field(description="Upload id from the first call; empty to "
                                                                     "start.")],
                     data: Annotated[str, Field(description="The next piece of the file, base64.")]) -> dict[str, Any]:
        """For the Research panel: send a file in pieces (start with upload empty, then pass the id back);
        finish with upload_finish."""
        uid = upload or new_id("u")
        if not uid.startswith("u") or not uid[1:].isalnum():
            raise LabError("Bad upload id.")
        with open(store.root / "uploads" / uid, "ab") as f:
            f.write(base64.b64decode(data))
        return {"upload": uid, "size": (store.root / "uploads" / uid).stat().st_size}

    @mcp.tool()
    async def upload_finish(upload: Annotated[str, Field(description="Upload id.")],
                            name: Annotated[str, Field(description="The file's name, e.g. run5.gaml.")]
                            ) -> dict[str, Any]:
        """For the Research panel: put a sent file into the store; returns its file reference."""
        if not upload.startswith("u") or not upload[1:].isalnum():
            raise LabError("Bad upload id.")
        p = store.root / "uploads" / upload
        if not p.exists():
            raise LabError("No such upload.")
        return await asyncio.to_thread(store.add_file, p, name, True)

    # ------------------------------------------------------------------ experiments

    def form(method: dict) -> tuple[list[dict], list[dict]]:
        try:
            m = methodfile.parse(method["source"])
        except SyntaxError:
            return [], []
        return methodfile.settings(m), methodfile.inputs(m)

    @mcp.tool()
    def experiments() -> list[dict[str, Any]]:
        """All experiments (a method applied to raw files): id, title, method, files, last run (status, QC flag
        counts), proposed corrections waiting for the user."""
        return [experiment_summary(e) for e in store.list_experiments()]

    @mcp.tool()
    def experiment_get(experiment: Annotated[str, Field(description="Experiment id, like e1a2b3c4d.")]
                       ) -> dict[str, Any]:
        """One experiment in full: method, files (with checksums), settings (with the method's defaults for the
        rest), corrections with reasons and status, last run."""
        e = store.get_experiment(experiment)
        try:
            m = method_of(e)
            settings, inputs = form(m)
            problem = None
        except LabError as err:
            settings, inputs, problem = [], [], str(err)
        resolved = {s["id"]: s["default"] for s in settings}
        resolved.update({k: v for k, v in (e.get("params") or {}).items() if k in resolved})
        files = experiment_inputs(e, [i["id"] for i in inputs])
        return {**e, "inputs": {**(e.get("inputs") or {}), **files}, "resolved_params": resolved,
                "method_params": settings, "method_inputs": inputs, "running": experiment in LIVE,
                "method_problem": problem}

    def _apply_files(e: dict, inputs: list[dict], files: dict[str, Any] | None) -> None:
        ids = [i["id"] for i in inputs]
        for k, v in (files or {}).items():
            if k not in ids:
                if k == "raw" and len(ids) == 1:  # the 0.2 name of the GC method's file
                    k = ids[0]
                else:
                    raise LabError(f"The method has no input '{k}'. Inputs: {', '.join(ids) or 'none'}.")
            if isinstance(v, str):
                v = import_path(v)
            if not isinstance(v, dict) or not v.get("path"):
                raise LabError(f"Input {k}: give a path on this PC or a file reference from file_import.")
            e.setdefault("inputs", {})[k] = v

    @mcp.tool()
    async def experiment_create(
        title: Annotated[str, Field(description="Title, e.g. 'Activity assay 06 - VLB120 ΔalkB'.")],
        method: Annotated[str, Field(description="Method id (methods lists them).")] = "gc-activity-assay",
        files: Annotated[dict[str, Any] | None, Field(
            description="Raw files per method input: {input id (e.g. gaml_file): absolute path on this PC, or a file "
                        "reference}.")] = None,
        params: Annotated[dict[str, Any] | None, Field(description="Settings (the rest use the method's "
                                                                   "defaults).")] = None,
        gcassay_config: Annotated[str | None, Field(
            description="Instead: path of a gcassay experiment config (YAML); its settings, raw file and "
                        "corrections file are taken over.")] = None,
        corrections: Annotated[list[dict[str, Any]] | None, Field(
            description="Corrections to start with (as gcassay_settings returns them); from an agent they are "
                        "only proposed.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create an experiment: a method plus its raw files and settings. Then run it with run."""
        m = store.get_method(method)
        _, inputs = form(m)
        e = {"id": new_id("e"), "title": title.strip() or "Experiment", "method": method, "inputs": {},
             "params": {}, "corrections": [], "created": now(), "created_by": _who(ctx)}
        if gcassay_config:
            cfgp = Path(gcassay_config).expanduser()
            cfg = parse_yaml(cfgp.read_text("utf-8"), "gcassay config")
            p, info = params_from_config(cfg)
            e["params"].update(p)
            if info.get("raw_file") and not files:
                files = {"raw": str((cfgp.parent / info["raw_file"]).resolve())}
            if info.get("corrections"):
                cp = (cfgp.parent / info["corrections"]).resolve()
                if cp.exists():
                    e["corrections"] = corrections_from(parse_yaml(cp.read_text("utf-8"), "corrections file"))
        e["params"].update(params or {})
        user = _is_user(ctx)
        for c in corrections or []:
            if not str(c.get("reason", "")).strip() or c.get("kind") not in ("rename", "exclude", "manual_integration"):
                raise LabError(f"Correction {c} needs a kind (rename, exclude, manual_integration) and a reason.")
            e["corrections"].append({**c, "id": new_id("c"), "status": "approved" if user else "proposed",
                                     "by": c.get("by") or _who(ctx), "at": c.get("at") or now()})
        e["changed"] = now()
        await asyncio.to_thread(_apply_files, e, inputs, files)
        store.save_experiment(e)
        changed("experiment", experiment=e["id"])
        return experiment_summary(e)

    @mcp.tool()
    def experiment_update(
        experiment: Annotated[str, Field(description="Experiment id.")],
        title: Annotated[str | None, Field(description="New title.")] = None,
        params: Annotated[dict[str, Any] | None, Field(description="Settings to set (merged; null resets one to the "
                                                                   "method's default).")] = None,
        files: Annotated[dict[str, Any] | None, Field(description="Replace raw files: {input id: path or file "
                                                                  "reference}.")] = None,
        method: Annotated[str | None, Field(description="Use another method (its id).")] = None,
        method_version: Annotated[int | None, Field(description="Pin a method version (0: always the latest).")]
        = None,
        notes: Annotated[str | None, Field(description="Free notes about the experiment.")] = None,
        methods_text: Annotated[str | None, Field(description="A Methods paragraph to keep with the experiment "
                                                              "(goes into its reports).")] = None,
    ) -> dict[str, Any]:
        """Change an experiment's title, settings, raw files, method (or pinned version), notes or Methods
        paragraph. Run again to see the effect."""
        e = store.get_experiment(experiment)
        if method is not None:
            store.get_method(method)
            e["method"] = method
            e["method_version"] = None
        if method_version is not None:
            e["method_version"] = method_version or None
        m = method_of(e)
        settings, inputs = form(m)
        if title is not None:
            e["title"] = title.strip() or e["title"]
        known = {s["id"] for s in settings}
        for k, v in (params or {}).items():
            if k not in known:
                raise LabError(f"The method has no setting '{k}'. Settings: {', '.join(sorted(known))}.")
            if v is None:
                e.setdefault("params", {}).pop(k, None)
            else:
                e.setdefault("params", {})[k] = v
        _apply_files(e, inputs, files)
        if notes is not None:
            e["notes"] = notes
        if methods_text is not None:
            e["methods_text"] = methods_text
        e["changed"] = now()
        store.save_experiment(e)
        changed("experiment", experiment=experiment)
        return experiment_get(experiment)

    @mcp.tool()
    def gcassay_settings(config: Annotated[str, Field(description="The text of a gcassay experiment config "
                                                                  "(YAML).")],
                         corrections: Annotated[str | None, Field(description="The text of its corrections file "
                                                                              "(YAML), if any.")] = None
                         ) -> dict[str, Any]:
        """Read a gcassay config (and corrections file) into this plugin's settings and corrections, without
        saving anything (the panel uses it to fill an experiment)."""
        p, info = params_from_config(parse_yaml(config, "gcassay config"))
        corr = corrections_from(parse_yaml(corrections, "corrections file")) if corrections else []
        return {"params": p, "title": info["title"], "raw_file": info.get("raw_file"), "corrections": corr}

    @mcp.tool()
    def experiment_delete(experiment: Annotated[str, Field(description="Experiment id.")]) -> dict[str, Any]:
        """Delete an experiment, its runs and its rows in the results database (raw files stay in the store)."""
        store.get_experiment(experiment)
        store.delete_experiment(experiment)
        for m in store.list_methods():
            if m.get("reference") == experiment:
                store.set_method_settings(m["id"], reference=None)
        changed("experiment", experiment=experiment, deleted=True)
        return {"deleted": experiment}

    # ------------------------------------------------------------------ running

    def start_run(exp_id: str, time_limit_s: float | None) -> asyncio.Future:
        e = store.get_experiment(exp_id)
        if exp_id in LIVE:
            return LIVE[exp_id]["future"]
        method = method_of(e)
        state: dict[str, Any] = {"run": None, "status": "running", "step": None, "message": "", "started": now()}
        loop = asyncio.get_running_loop()

        def on_change(ev: dict) -> None:
            state["run"], state["step"], state["message"] = ev["run"], ev.get("step"), ev.get("message") or ""
            loop.call_soon_threadsafe(lambda: changed("run", experiment=exp_id, run=ev["run"], step=ev.get("step"),
                                                      status="running"))

        def work() -> dict:
            return runner.run(e, method, on_change=on_change, time_limit_s=time_limit_s)

        fut = asyncio.ensure_future(asyncio.to_thread(work))
        state["future"] = fut
        LIVE[exp_id] = state

        def done(f: asyncio.Future) -> None:
            LIVE.pop(exp_id, None)
            run = f.result() if not f.exception() else None
            changed("run", experiment=exp_id, run=(run or {}).get("id"), status=(run or {}).get("status", "failed"),
                    finished=True)

        fut.add_done_callback(done)
        TASKS.add(fut)
        fut.add_done_callback(TASKS.discard)
        return fut

    def run_brief(run: dict) -> dict:
        return {"run": run["id"], "experiment": run["experiment"], "status": run["status"], "error": run.get("error"),
                "started": run["started"], "finished": run.get("finished"), "method": run["method"],
                "summary": run.get("summary"), "trial": run.get("trial", False), "ms": run.get("ms"),
                "notes": run.get("notes") or [],
                "legacy": bool(run.get("steps")) and not any(s.get("function") for s in run.get("steps") or []),
                "steps": [{"function": s.get("function") or s.get("id"), "kind": s.get("kind"),
                           "status": s.get("status"),
                           "ms": s.get("ms"), "error": s.get("error"),
                           "table": {"rows": (s.get("output") or {}).get("rows"),
                                     "columns": (s.get("output") or {}).get("columns")}
                           if (s.get("output") or {}).get("kind") == "table" else None}
                          for s in run.get("steps") or []],
                "outputs": {n: {k: v for k, v in o.items() if k != "file"}
                            for n, o in (run.get("outputs") or {}).items()}}

    @mcp.tool()
    async def run(experiment: Annotated[str, Field(description="Experiment id.")],
                  wait_s: Annotated[float, Field(description="How long to wait for it to finish (seconds, max 55); "
                                                             "if it is still running, ask run_get later.")] = 50,
                  time_limit_s: Annotated[float | None, Field(description="Stop it after this many seconds "
                                                                          "(default 900).")] = None
                  ) -> dict[str, Any]:
        """Run an experiment's method on its raw files with its settings and approved corrections, in a separate
        calculation process (slow parts like reading GAML and integrating peaks are remembered, so a rerun after a
        correction is fast). Returns the run: status, each step's status and table, QC flag counts, and for a failed
        run the method's line and what went wrong. Read numbers with result_table, explanations with explain."""
        fut = start_run(experiment, time_limit_s)
        try:
            r = await asyncio.wait_for(asyncio.shield(fut), timeout=max(0.0, min(wait_s, 55)))
            return run_brief(r)
        except TimeoutError:
            st = LIVE.get(experiment) or {}
            return {"run": st.get("run"), "status": "running", "step": st.get("step"), "message": st.get("message")}

    @mcp.tool()
    def run_stop(experiment: Annotated[str | None, Field(description="The experiment whose run to stop (default: "
                                                                     "whatever runs).")] = None) -> dict[str, Any]:
        """Stop the calculation that runs now (an endless loop, or too slow): its process is ended and a fresh one
        started; the run is recorded as stopped."""
        if experiment and experiment not in LIVE:
            return {"stopped": False, "note": "That experiment isn't running."}
        return {"stopped": pool.stop("you")}

    @mcp.tool()
    def run_get(run: Annotated[str | None, Field(description="Run id.")] = None,
                experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run (or the one "
                                                                    "in progress).")] = None) -> dict[str, Any]:
        """A run's status, steps, tables and QC summary (or the progress of one still running)."""
        if experiment and experiment in LIVE and not run:
            st = LIVE[experiment]
            return {"run": st.get("run"), "status": "running", "step": st.get("step"), "message": st.get("message")}
        return run_brief(pick_run(run, experiment))

    @mcp.tool()
    def runs(experiment: Annotated[str | None, Field(description="Only this experiment's runs.")] = None,
             trials: Annotated[bool, Field(description="Also the comparison runs made after method changes.")] = False
             ) -> list[dict[str, Any]]:
        """Runs, newest first: id, experiment, status, times, method version, QC summary."""
        return store.list_runs(experiment, trials)

    @mcp.tool()
    def compare_runs(before: Annotated[str, Field(description="The earlier run id.")],
                     after: Annotated[str, Field(description="The later run id.")],
                     limit: Annotated[int, Field(description="Changes listed per table (max 500).")] = 50
                     ) -> dict[str, Any]:
        """Which numbers moved between two runs: per output table the rows added or removed and every changed value
        (before, after, change in %), paired by the identifying columns."""
        return compare.compare_runs(store, store.get_run(before), store.get_run(after), max(1, min(limit, 500)))

    # ------------------------------------------------------------------ results

    @mcp.tool()
    def result_table(
        table: Annotated[str, Field(description="An output name (activities, concentrations, time_course, peaks, "
                                                "calibration, flags ...) or 'step:<function>' for a step's table.")],
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
        where: Annotated[dict[str, Any] | None, Field(description="Keep rows whose column equals the value (a list: "
                                                                  "any of them), e.g. {\"compound\": \"HAME\"}.")]
        = None,
        columns: Annotated[list[str] | None, Field(description="Only these columns.")] = None,
        limit: Annotated[int, Field(description="Rows to return (max 2000).")] = 100,
        offset: Annotated[int, Field(description="Rows to skip.")] = 0,
    ) -> dict[str, Any]:
        """Rows of a table from a run, computed by the method. Quote numbers from here (with the run id), never
        compute or round them yourself in a way that changes them."""
        r = pick_run(run, experiment)
        df = store.run_table(r["id"], table)
        for c, v in (where or {}).items():
            if c not in df.columns:
                raise LabError(f"No column '{c}'. Columns: {', '.join(map(str, df.columns))}.")
            vals = v if isinstance(v, list) else [v]
            keep = explainer.equals(df[c], vals[0])
            for x in vals[1:]:
                keep = keep | explainer.equals(df[c], x)
            df = df[keep]
        if columns:
            missing = [c for c in columns if c not in df.columns]
            if missing:
                raise LabError(f"No column {missing}. Columns: {', '.join(map(str, df.columns))}.")
            df = df[columns]
        total = len(df)
        part = df.iloc[max(0, offset): max(0, offset) + max(1, min(limit, 2000))]
        return {"run": r["id"], "table": table, "total": total, "columns": [str(c) for c in df.columns],
                "rows": records(part)}

    @mcp.tool(structured_output=False)
    def figure(run: Annotated[str | None, Field(description="Run id.")] = None,
               experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
               output: Annotated[str, Field(description="The figure's output name.")] = "figure") -> Image:
        """A figure a run drew (PNG)."""
        r = pick_run(run, experiment)
        f = (r.get("outputs") or {}).get(output)
        if not f or f.get("kind") != "figure":
            raise LabError(f"Run {r['id']} has no figure '{output}'.")
        return Image(data=(store.run_dir(r["id"]) / f["file"]).read_bytes(), format="png")

    @mcp.tool()
    def provenance(
        table: Annotated[str, Field(description="The table the number is in (e.g. concentrations).")],
        where: Annotated[dict[str, Any], Field(description="Columns that pick one row, e.g. {\"injection\": 12, "
                                                           "\"compound\": \"HHAME\"}.")],
        column: Annotated[str | None, Field(description="The number's column, e.g. conc_mM (default: the last "
                                                        "number column).")] = None,
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
    ) -> dict[str, Any]:
        """Where one number came from: the recorded call of the method's function that returned exactly that value
        (for a formula used on a whole column, that row), its inputs and formula with the numbers put in, and in
        turn the calls that gave those inputs."""
        r = pick_run(run, experiment)
        return explainer.provenance(store, r, table, where, column)

    # ------------------------------------------------------------------ chromatograms and corrections

    def sequence_of(e: dict):
        import toolbox

        try:
            m = methodfile.parse(method_of(e)["source"])
            file_params = [p for p in (m.recipe_function.params if m.recipe_function else []) if p.endswith("_file")]
            channel = (e.get("params") or {}).get("channel", (m.recipe_function.defaults if m.recipe_function else
                                                               {}).get("channel"))
        except (LabError, SyntaxError):
            file_params, channel = [], (e.get("params") or {}).get("channel")
        files = experiment_inputs(e, file_params or list((e.get("inputs") or {}).keys())[:1])
        gaml = next((f for f in files.values() if str(f.get("name", f.get("path", ""))).lower().endswith(".gaml")),
                    None) or next(iter(files.values()), None)
        if not gaml:
            raise LabError("This experiment has no GC sequence file.")
        toolbox.cache.use(store.root / "cache")
        with GAML_LOCK:
            key = f"{gaml['path']}:{channel}"
            if key not in gaml_cache:
                gaml_cache.clear()
                gaml_cache[key] = toolbox.read_gaml(store.path(gaml["path"]), channel if isinstance(channel, str)
                                                    else None)
            return gaml_cache[key]

    @mcp.tool()
    async def chromatogram(
        experiment: Annotated[str, Field(description="Experiment id (a GC experiment).")],
        injection: Annotated[int, Field(description="Injection number (as in Chromeleon).")],
        start_min: Annotated[float | None, Field(description="From this time (min); default the start.")] = None,
        end_min: Annotated[float | None, Field(description="To this time (min); default the end.")] = None,
        points: Annotated[int, Field(description="At most this many points (min/max kept per bucket).")] = 1200,
    ) -> dict[str, Any]:
        """A stretch of one injection's raw trace (time in min, signal in pA), thinned to `points`, with the peaks
        the latest run found in that stretch (start, end, baseline, area, status) and this injection's corrections."""
        e = store.get_experiment(experiment)
        seq = await asyncio.to_thread(sequence_of, e)
        inj = next((i for i in seq if i.number == injection), None)
        if inj is None:
            raise LabError(f"No injection {injection} (1-{len(seq)}).")
        t, y = inj.time_min, inj.signal_pa
        lo = t[0] if start_min is None else start_min
        hi = t[-1] if end_min is None else end_min
        idx = np.flatnonzero((t >= lo) & (t <= hi))
        if len(idx) > points > 10:
            buckets = np.array_split(idx, points // 2)
            keep = []
            for b in buckets:
                if len(b):
                    seg = y[b]
                    keep += sorted({int(b[np.argmin(seg)]), int(b[np.argmax(seg)])})
            idx = np.array(keep)
        peaks = []
        if e.get("last_run"):
            try:
                pk = store.run_table(e["last_run"]["id"], "peaks")
                pk = pk[pk.injection == injection]
                cols = [c for c in ["compound", "status", "rt_expected", "rt_tolerance", "rt", "start", "end", "area",
                                    "height", "snr", "baseline_start_y", "baseline_end_y", "note", "vendor_rt",
                                    "vendor_area"] if c in pk.columns]
                peaks = records(pk[cols])
            except (LabError, KeyError, AttributeError):
                peaks = []
        return {"injection": injection, "name": inj.name, "time": [round(float(v), 5) for v in t[idx]],
                "signal": [round(float(v), 5) for v in y[idx]], "peaks": peaks,
                "corrections": [c for c in e.get("corrections") or [] if int(c.get("injection", -1)) == injection]}

    @mcp.tool()
    def correction_propose(
        experiment: Annotated[str, Field(description="Experiment id.")],
        kind: Annotated[Literal["rename", "exclude", "manual_integration"], Field(
            description="rename: fix a wrong injection name · exclude: leave out an injection (or one compound in "
                        "it) · manual_integration: set a peak's start and end by hand")],
        injection: Annotated[int, Field(description="Injection number.")],
        reason: Annotated[str, Field(description="Why, in a sentence a reviewer understands. Required.")],
        compound: Annotated[str | None, Field(description="Compound (exclude: empty = the whole injection; "
                                                          "manual_integration: required).")] = None,
        start_min: Annotated[float | None, Field(description="manual_integration: peak start (min).")] = None,
        end_min: Annotated[float | None, Field(description="manual_integration: peak end (min).")] = None,
        new_name: Annotated[str | None, Field(description="rename: the correct injection name.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Record a correction with its reason. From the user's own panel it applies at the next run; from an AI
        agent it is only proposed: the user approves or rejects it in the panel (Corrections). Raw data is never
        changed; every correction is listed in the report."""
        if not reason.strip():
            raise LabError("Every correction needs a reason.")
        if kind == "manual_integration":
            if not compound or start_min is None or end_min is None or end_min <= start_min:
                raise LabError("A manual integration needs the compound and a start before the end (min).")
        if kind == "rename" and not (new_name or "").strip():
            raise LabError("A rename needs the new name.")
        e = store.get_experiment(experiment)
        user = _is_user(ctx)
        c: dict[str, Any] = {"id": new_id("c"), "kind": kind, "injection": int(injection), "reason": reason.strip(),
                             "status": "approved" if user else "proposed", "by": _who(ctx), "at": now()}
        if compound:
            c["compound"] = compound
        if kind == "manual_integration":
            c["start_min"], c["end_min"] = float(start_min), float(end_min)
        if kind == "rename":
            c["new_name"] = new_name.strip()
        corr = e.setdefault("corrections", [])
        if user:  # a newer decision on the same peak replaces the older one
            for old in corr:
                if old.get("status") == "approved" and old["kind"] == kind and old["injection"] == c["injection"] \
                        and old.get("compound") == c.get("compound"):
                    old["status"], old["replaced_by"] = "replaced", c["id"]
        corr.append(c)
        if user:
            e["changed"] = now()
        store.save_experiment(e)
        changed("corrections", experiment=experiment)
        return {"correction": c, "note": "Applies at the next run." if user else
                "Proposed: the user approves or rejects it in the Research panel."}

    @mcp.tool()
    def correction_decide(
        experiment: Annotated[str, Field(description="Experiment id.")],
        correction: Annotated[str, Field(description="Correction id.")],
        decision: Annotated[Literal["approve", "reject", "remove"], Field(
            description="approve a proposal, reject it, or remove (undo) an approved correction")],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """The user's decision on a correction (proposals from agents need it before they apply)."""
        e = store.get_experiment(experiment)
        c = next((x for x in e.get("corrections") or [] if x["id"] == correction), None)
        if c is None:
            raise LabError(f"No correction {correction}.")
        c["status"] = {"approve": "approved", "reject": "rejected", "remove": "removed"}[decision]
        c["decided_by"], c["decided_at"] = _who(ctx), now()
        if decision == "approve":
            for old in e["corrections"]:
                if old is not c and old.get("status") == "approved" and old["kind"] == c["kind"] \
                        and old["injection"] == c["injection"] and old.get("compound") == c.get("compound"):
                    old["status"], old["replaced_by"] = "replaced", c["id"]
        e["changed"] = now()
        store.save_experiment(e)
        changed("corrections", experiment=experiment)
        return {"correction": c}

    # ------------------------------------------------------------------ database and export

    @mcp.tool()
    def database_tables() -> list[dict[str, Any]]:
        """The PhD-wide results database: one table per kept method output (activities, time_course,
        concentrations ...), each with the latest run of every experiment; rows, experiments, columns."""
        return store.db_tables()

    @mcp.tool()
    def database_query(
        table: Annotated[str, Field(description="Table name (database_tables lists them).")],
        where: Annotated[dict[str, Any] | None, Field(description="Column equals value (or any of a list), e.g. "
                                                                  "{\"compound\": \"HAME\", \"strain\": \"WT\"}.")]
        = None,
        limit: Annotated[int, Field(description="Max rows.")] = 500,
    ) -> dict[str, Any]:
        """Rows across all experiments, to compare strains, conditions and assays over the PhD. Each row says which
        experiment and run it came from."""
        df = store.db_query(table, where, limit)
        return {"table": table, "rows": records(df), "columns": [str(c) for c in df.columns]}

    @mcp.tool()
    async def export(
        format: Annotated[Literal["html", "xlsx", "docx"], Field(
            description="html: full report with explanations and figures · xlsx: every table · docx: Word document "
                        "with the explanations and results")],
        experiment: Annotated[str | None, Field(description="Experiment id (its latest run).")] = None,
        run: Annotated[str | None, Field(description="Or a run id.")] = None,
        inline: Annotated[bool, Field(description="Also return the file's bytes (base64), for the panel.")] = False,
    ) -> dict[str, Any]:
        """Write a report of a run: how every number was calculated (each step's sentence, its formulas drawn from
        the code, this run's numbers), the results, figures, QC flags, every correction with its reason, checksums,
        versions, and the method's source as an appendix. Returns where it was saved."""
        from . import export as exp_mod

        r = pick_run(run, experiment)
        e = store.get_experiment(r["experiment"])
        path = await asyncio.to_thread(exp_mod.write, store, e, r, format)
        out: dict[str, Any] = {"path": str(path), "name": path.name, "size": path.stat().st_size}
        if inline:
            out["data"] = base64.b64encode(path.read_bytes()).decode()
        return out

    @mcp.tool()
    async def method_export(
        method: Annotated[str | None, Field(description="Method id (with no experiment: no settings or files).")]
        = None,
        experiment: Annotated[str | None, Field(description="Or an experiment: its method, settings, corrections "
                                                            "and raw file checksums (its latest run's code).")] = None,
        include_raw: Annotated[bool, Field(description="Also copy the raw files into the folder.")] = False,
        inline: Annotated[bool, Field(description="Also return the zip's bytes (base64), for the panel.")] = False,
    ) -> dict[str, Any]:
        """Export a method as a folder that runs without layr: the method file, the toolbox, the settings, the raw
        files' checksums, a small runner (python run.py) and the requirements; zipped as well."""
        e = store.get_experiment(experiment) if experiment else None
        mid = method or (e["method"] if e else None)
        if not mid:
            raise LabError("Give a method or an experiment.")
        m = store.get_method(mid, e.get("method_version") if e else None)
        r = store.get_run(e["last_run"]["id"]) if e and e.get("last_run") else None
        name = standalone.slug(f"{mid}-v{(r or {}).get('method', {}).get('version') or m['version']}"
                               + (f"-{e.get('title')}" if e else ""))
        dest = store.root / "exports" / name

        def work() -> dict:
            folder = standalone.export_folder(store, m, dest, e, r, include_raw)
            archive = standalone.zip_folder(folder)
            out = {"folder": str(folder), "zip": str(archive), "name": archive.name, "size": archive.stat().st_size}
            if inline:
                out["data"] = base64.b64encode(archive.read_bytes()).decode()
            return out

        return await asyncio.to_thread(work)

    @mcp.tool()
    async def notebook(experiment: Annotated[str | None, Field(description="Experiment id (its latest run).")] = None,
                       run: Annotated[str | None, Field(description="Or a run id.")] = None) -> dict[str, Any]:
        """Open a run in a notebook: writes a folder into JupyterLab's notebooks (the jupyter plugin) with the run's
        tables, its exact method file and the toolbox, and explore.ipynb that loads them and imports the method's
        functions. Returns where it is."""
        r = pick_run(run, experiment)
        e = store.get_experiment(r["experiment"])
        m = {"id": r["method"]["id"], "name": r["method"].get("name"), "version": r["method"].get("version"),
             "source": explainer.run_source(store, r) or ""}
        return await asyncio.to_thread(standalone.make_notebook, store, m, e, r)

    @mcp.tool()
    def lab_sample(ctx: Context | None = None) -> dict[str, Any]:
        """Create the example GC experiment (a made-up activity assay: substrate SUB turned into product PROD, internal
        standard octanol), to try the methods without real data."""
        from .sample import make_sample

        f = make_sample(store)
        e = {"id": new_id("e"), "title": "Example: made-up GC activity assay", "method": "gc-activity-assay",
             "inputs": {"gaml_file": f}, "params": dict(SAMPLE_PARAMS), "corrections": [], "created": now(),
             "created_by": _who(ctx)}
        store.save_experiment(e)
        if not store.method_settings("gc-activity-assay").get("reference"):
            store.set_method_settings("gc-activity-assay", reference=e["id"])
        changed("experiment", experiment=e["id"])
        return experiment_summary(e)

    return store


SAMPLE_PARAMS = {
    "compounds": {"SUB": {"rt": 3.62}, "octanol": {"rt": 5.13}, "PROD": {"rt": 6.47}},
    "internal_standard": "octanol",
    "sample_patterns": [
        {"role": "sample", "regex": r"^Act\.Assay_(?P<strain>[A-Za-z0-9]+)_(?P<time_min>\d+)min_R(?P<replicate>\d+)_"
                                    r"(?P<substrate>[A-Z]+)$"},
        {"role": "standard", "regex": r"^GC_std_(?P<std_conc_mM>[\d.]+)mM (?P<std_series>[A-Z])$"},
        {"role": "blank", "regex": r"^ether$"},
    ],
    "cal_max_conc_mM": 2.0,
    "concentration_factor": 1.0,
    "rate_window_min": [0, 30],
    "biomass_gcdw_per_l": 0.5,
}

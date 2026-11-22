"""MCP tools for methods (Python files), experiments, runs, corrections and explanations.

Rules the tools enforce (also in the server's instructions for agents):
- Numbers come only from runs: an agent reads results with result_table / provenance and cites the run id.
- A method is a Python file. The user's own saves in the panel count at once (a new version); a change written by
  an AI agent is a draft until the user accepts it in the panel (draft_decide answers only for the user).
- Corrections (of the kinds the method declares in CORRECTIONS: renames, exclusions, boundaries set by hand, a
  respondent left out...) always carry a reason; one made by an agent is only proposed until the user approves it.
- A method's pages, kinds of correction and extra packages are plain values in its file (lab/pages.py), changed with
  method_declare like any other change of the method.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from mcp.server.fastmcp import Context, FastMCP, Image
from pydantic import Field

from . import checker, compare, examples, methodfile, packages, standalone
from . import explain as explainer
from . import sources as sources_mod
from . import pages as declarations
from .errors import LabError
from .runner import Pool, Runner, experiment_inputs
from .store import Store, jsonable, method_name, new_id, now, records, step_output

LIVE: dict[str, dict[str, Any]] = {}  # experiment id -> the run in progress
TRIALS: dict[str, dict[str, Any]] = {}  # experiment id -> the trial run in progress (an unsaved source or settings)
TRIAL_ASKED: dict[str, int] = {}  # experiment id -> how many trial runs were asked for (only the newest waits)
TASKS: set[asyncio.Future] = set()
CURVES: dict[str, Any] = {}  # a raw file's curves (or table) by file and settings, so dragging a region stays fast
CURVES_LOCK = threading.Lock()
CURVES_KEPT = 3
SOURCES_HELP = ("Results of other experiments, for the method's inputs that read them (their Inputs: line ends in "
                "[result]): {input id: [{\"experiment\": id, \"result\": a table it returns, e.g. \"scores\", or "
                "\"step:<function>\"}, ...]}, one or several experiments; `result` may be left out when the input "
                "names the one it expects ([result: scores]). The experiments lists each one's tables.")


def thin(t: np.ndarray, y: np.ndarray, start: float | None, end: float | None, points: int) -> np.ndarray:
    """The indexes of a curve's points between start and end, at most about `points` of them (each bucket keeps its
    lowest and highest point, so sharp rises and falls survive)."""
    lo = t[0] if start is None else start
    hi = t[-1] if end is None else end
    idx = np.flatnonzero((t >= lo) & (t <= hi))
    if len(idx) > points > 10:
        keep = []
        for b in np.array_split(idx, points // 2):
            if len(b):
                seg = y[b]
                keep += sorted({int(b[np.argmin(seg)]), int(b[np.argmax(seg)])})
        idx = np.array(keep)
    return idx


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

    from toolbox import registry

    found = registry.find(name)
    if found is None:
        raise LabError(f"The toolbox has no `{name}`. It has: {', '.join(registry.everything())}.")
    obj = found[1]
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


def register(mcp: FastMCP, data_dir: Path, emit, pool: Pool | None = None) -> Store:
    """Adds the lab's tools to `mcp`. Starts no calculation process: the first run starts one, or the server warms
    `pool` when it runs (server.py)."""
    store = Store(data_dir / "lab")
    if store.started_fresh:
        logging.getLogger("research").warning("the data of an earlier version was cleared: %s starts fresh", store.root)
    packages.configure(store.root)
    pool = pool or Pool()
    runner = Runner(store, pool)

    def changed(what: str, **data: Any) -> None:
        emit("research:lab", {"what": what, **data})

    def parsed(source: str) -> methodfile.Method:
        try:
            return methodfile.parse(source)
        except SyntaxError as e:
            raise LabError(f"The method can't be read (line {e.lineno}): {e.msg}.") from e

    def method_of(e: dict) -> dict:
        return store.get_method(e["method"], e.get("method_version"))

    def kinds_of(e: dict) -> dict[str, dict]:
        """The kinds of correction the experiment's method declares (lab/pages.py)."""
        try:
            m = methodfile.parse(method_of(e)["source"])
        except (LabError, SyntaxError):
            return {}
        return declarations.correction_kinds(m.constants)

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
                    before = runner.run(exp, base, trial=True, reason=f"comparison base (version {base_version})",
                                        fresh=True)
                # every step calculated again: the comparison proves the numbers, it doesn't trust remembered ones
                after = runner.run(exp, after_method, trial=True, reason="comparison after a change", fresh=True)
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
        """The analysis methods (Python files): id, name, latest version, number of steps, inputs (raw files, kind
        file, and results of other experiments, kind result), built in or not, whether an AI draft waits for the
        user, the reference experiment."""
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
    def method_get(method: Annotated[str, Field(description="Method id, e.g. survey-scores.")],
                   version: Annotated[int | None, Field(description="A version; default the latest.")] = None
                   ) -> dict[str, Any]:
        """A method in full: its Python source, the outline of its functions (recipe, steps, formulas, helpers),
        its settings and inputs (from the recipe's signature and docstring; summary.inputs: [{id, label, kind: file
        (types: extensions) | result (result: the table name it expects, or null), optional}]), the style checker's
        messages, its versions, its reference experiment and a pending AI draft (if any)."""
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
            packages.install_later((result.get("summary") or {}).get("requirements") or [])
            reference = None
            if current["version"] and not declarations.same_calculation(current["source"], new_source):
                reference = start_comparison(method, saved, current["version"], "version", saved["version"])
            changed("method", method=method, version=saved["version"])
            return {"saved": True, "version": saved["version"], "messages": result["messages"], "diff": lines,
                    "reference": reference}
        meta = {"by": _who(ctx), "at": now(), "change": change.strip(), "base_version": current["version"],
                "messages": result["messages"]}
        store.save_draft(method, new_source, meta)
        draft_method = {**current, "source": new_source, "draft": True}
        reference = start_comparison(method, draft_method, current["version"], "draft") \
            if current["version"] and not declarations.same_calculation(current["source"], new_source) else None
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
    def method_declare(
        method: Annotated[str, Field(description="Method id.")],
        what: Annotated[Literal["pages", "corrections", "requirements"], Field(
            description="pages: the experiment's own pages made of cards (PAGES) · corrections: the kinds of "
                        "correction an experiment records (CORRECTIONS) · requirements: extra Python packages "
                        "(REQUIREMENTS)")],
        value: Annotated[Any, Field(description="The whole new value: pages [{title, cards: [...]}]; corrections "
                                                "{kind id: {label, help?, fields: {name: 'text'|'number'|'integer'|"
                                                "'boolean' (add 'optional')}, key?: [fields]}}; requirements "
                                                "['pingouin>=0.5']. Empty removes it. Card kinds and their fields: "
                                                "see lab/pages.py (text, number, table, figure, chart, review).")],
        change: Annotated[str | None, Field(description="One sentence for the version history.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Set a method's pages, kinds of correction or extra packages: only that one assignment at the top of the
        file is rewritten. Like method_write: from the user's panel a new version at once, from an AI agent a draft
        the user accepts. Pages and kinds of correction don't change any number, so the reference experiment isn't
        compared again for them."""
        name = what.upper()
        if what == "pages" and value not in (None, []) and not (
                isinstance(value, list) and all(isinstance(p, dict) and str(p.get("title") or "").strip()
                                                for p in value)):
            raise LabError("Pages are a list of {\"title\": ..., \"cards\": [...]}, each with a title.")
        if what == "corrections" and value not in (None, {}):
            if not isinstance(value, dict):
                raise LabError("Kinds of correction are a dict: {kind id: {\"label\", \"fields\": {...}}}.")
            for kid in value:
                if not declarations._ID.match(str(kid)):
                    raise LabError(f"'{kid}' can't be a kind's id: lowercase letters, digits and _, starting with a "
                                   "letter (e.g. drop_respondent).")
        if what == "requirements" and value:
            try:
                value = declarations.check_requirements(list(value))
            except (TypeError, ValueError) as e:
                raise LabError(str(e)) from None
        user = _is_user(ctx)
        draft = store.get_draft(method)
        base = draft["source"] if (draft and (not user or store.draft_only(method))) else store.get_method(method)[
            "source"]
        try:
            new_source = declarations.write(base, name, value)
        except (ValueError, SyntaxError) as e:
            raise LabError(f"Couldn't write {name}: {e}") from None
        label = {"pages": "the pages", "corrections": "the kinds of correction", "requirements": "the packages"}[what]
        return method_write(method, (change or "").strip() or f"Changed {label}", source=new_source, ctx=ctx)

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
                     source: Annotated[str | None, Field(description="accept: the text to save instead of the "
                                                                     "draft's, when the user took only some of its "
                                                                     "changes (merged in the panel).")] = None,
                     ctx: Context | None = None) -> dict[str, Any]:
        """The user's decision on an AI draft (only the user decides, in the Research panel). Accepting with
        `source` saves that merged text as the next version (the user's, with the AI's change noted) and drops the
        draft; the reference experiment is compared again when the text differs from the draft's."""
        if not _is_user(ctx):
            raise LabError("Only the user accepts or rejects a draft, in the Research panel (Methods).")
        d = store.get_draft(method)
        if d is None:
            raise LabError(f"Method {method} has no draft.")
        if decision == "reject":
            store.drop_draft(method)
            changed("method", method=method)
            return {"rejected": True}
        text = d["source"]
        merged = source is not None and (source if source.endswith("\n") else source + "\n") != d["source"]
        if merged:
            text = source if source.endswith("\n") else source + "\n"
            result = checker.check(text)
            if not result["ok"]:
                return {"accepted": False, "messages": result["messages"], "note": "Not saved: fix the errors first."}
        new_method = store.draft_only(method)
        base = None if new_method else store.get_method(method)
        if base is not None and text == base["source"]:
            store.drop_draft(method)
            changed("method", method=method)
            return {"accepted": False, "rejected": True, "note": "None of the draft's changes were kept: nothing "
                                                                 "changed."}
        store.drop_draft(method)
        if merged:
            saved = store.save_method(method, text, f"{_who(ctx)}, with part of a change by {d.get('by')}",
                                      d.get("change") or "AI change, in part")
            if base is not None:
                start_comparison(method, saved, base["version"], "version", saved["version"])
        else:
            saved = store.save_method(method, text, f"{d.get('by')}, accepted by {_who(ctx)}",
                                      d.get("change") or "AI change", {"comparison": d.get("comparison")})
        changed("method", method=method, version=saved["version"])
        return {"accepted": True, "version": saved["version"], "merged": merged}

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
        """The toolbox's ready-made functions (reading tables, straight lines, flags, ready-made steps and charts)
        and those of its add-ons (sub-packages for one kind of data or instrument): {functions: [{name, module,
        signature, sentence, docstring text, formulas, code}], groups: [{module, title, sentence, functions: [names]}]
        (the toolbox first, then each add-on), readers: [{function, module, label, extensions, out, param}] (the
        functions that read a raw file, for "add a raw file")}. Methods import them from their module:
        `from toolbox import fit_line`, `from toolbox.<add-on> import ...`."""
        from toolbox import registry

        if name:
            return {"functions": [explainer.toolbox_view(name)]}
        return {"functions": explainer.toolbox_docs(), "groups": registry.groups(), "readers": registry.readers()}

    @mcp.tool()
    def toolbox_copy(name: Annotated[str, Field(description="The toolbox function to copy.")],
                     source: Annotated[str, Field(description="The method's current source.")]) -> dict[str, Any]:
        """The method's source with a toolbox function copied in (with the imports it needs), to change it: the
        copy is used instead of the toolbox's. Nothing is saved; save it with method_write."""
        new = copy_from_toolbox(source, name)
        return {"source": new, "messages": checker.check(new)["messages"]}

    @mcp.tool()
    def ai_prompt(
        kind: Annotated[Literal["explain", "methods", "step"], Field(
            description="explain: a function or selected lines in plain words · methods: a Methods paragraph for a "
                        "paper · step: write one new step function from the user's sentence")],
        method: Annotated[str | None, Field(description="Method id.")] = None,
        source: Annotated[str | None, Field(description="Or the method's source.")] = None,
        function: Annotated[str | None, Field(description="explain: the function · step: the new step's name "
                                                          "(e.g. remove_outliers).")] = None,
        code: Annotated[str | None, Field(description="explain: or the selected lines.")] = None,
        experiment: Annotated[str | None, Field(description="methods: the experiment (its settings, files).")]
        = None,
        inputs: Annotated[dict[str, list[str] | None] | None, Field(
            description="step: the step's inputs by parameter name, each with the columns its table had in the last "
                        "run (null: not a table), e.g. {\"cultures\": [\"culture\", \"time_h\", \"od600\"], "
                        "\"window_h\": null}.")] = None,
        request: Annotated[str | None, Field(description="step: what the step should do, in the user's words.")]
        = None,
    ) -> dict[str, Any]:
        """For the panel: the prompt it sends to Claude (the claude plugin's ask tool) for an AI explanation, a
        Methods paragraph, or a new step, built from the code and settings only. For `step` the answer is one
        ```python block defining exactly `function`: put it in with method_write(function=..., code=...) or
        flow_edit's new_function / add_step, as a draft she reviews."""
        e = store.get_experiment(experiment) if experiment else None
        if source is None:
            mid = method or (e["method"] if e else None)
            if not mid:
                raise LabError("Give a method, its source, or an experiment.")
            source = store.get_method(mid, e.get("method_version") if e else None)["source"]
        if kind == "step":
            if not function or not function.isidentifier():
                raise LabError("Give the new step's name in `function` (letters, digits and _, e.g. remove_outliers).")
            if not (request or "").strip():
                raise LabError("Say in `request` what the step should do.")
            taking = "\n".join(f"- {n}: " + (f"a table with the columns {', '.join(c)}" if c else "a value")
                               for n, c in (inputs or {}).items()) or "- (no inputs given: choose them from the method)"
            have = parsed(source).function(function)
            system = (
                "You write one step of an analysis method for a microbiology PhD student who is learning Python. The "
                "method is one Python file in a strict, readable style: a step is a function that takes tables and "
                "values and returns a new table; it picks rows, loops over groups ('for each group: calculate, add a "
                "row dict to a list, make a table with pd.DataFrame(rows)'), and calls the method's formulas; it never "
                "changes a table it was given (copy it); a plain loop or a merge rather than clever pandas (no "
                "transform, no set_index().map(), no index tricks); about 25 lines at most. It starts with a docstring "
                "whose first line is one plain sentence; a Formula: block in the docstring for what isn't one "
                "expression. Units go in names (conc_mM, time_min). Imports only from toolbox, pandas, numpy, scipy, "
                "math, statistics, and only those the method already has, unless one is needed. Use only the columns "
                "listed for the inputs. Answer with exactly one ```python code block that defines the function, "
                "nothing else in it (no imports, no example call), then at most three short sentences saying what it "
                "does and anything you had to assume.")
            prompt = (f"Write the step `{function}`" + (" (it exists: rewrite it)" if have else "") +
                      f".\n\nWhat it should do: {request.strip()}\n\nIts inputs:\n{taking}\n\nThe whole method, for "
                      f"its style, its formulas and the names it uses:\n\n```python\n{source}\n```")
            return {"prompt": prompt, "system": system, "function": function,
                    "answer": "One ```python block defining the function; take the code from it."}
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
    async def file_import(path: Annotated[str, Field(description="Absolute path of a file on this PC (a table such "
                                                                 "as CSV or Excel, an instrument's export...).")]
                          ) -> dict[str, Any]:
        """Copy a raw data file into the plugin's store (by checksum; the original is never changed). Returns a file
        reference to give experiment_create / experiment_update as an input."""
        return await asyncio.to_thread(import_path, path)

    @mcp.tool()
    async def file_fetch(url: Annotated[str, Field(description="A layr file address from the Research panel "
                                                               "(layr.files.url).")],
                         name: Annotated[str, Field(description="The file's name, e.g. plate5.csv.")]
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
                            name: Annotated[str, Field(description="The file's name, e.g. plate5.csv.")]
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

    def linked(e: dict, everyone: list[dict], by_id: dict[str, dict], runs: sources_mod.Runs) -> dict[str, Any]:
        """How an experiment is tied to others: the tables its latest run has (to pick from), what it reads, who
        reads it, and why it is out of date."""
        returned, steps = sources_mod.tables(runs.latest(e))
        return {"results": returned, "step_results": steps,
                "reads": [{"input": i, "experiment": s["experiment"], "result": s["result"]}
                          for i, chosen in sources_mod.picks(e).items() for s in chosen],
                "read_by": sources_mod.readers(e["id"], everyone),
                "outdated": sources_mod.outdated(store, e, by_id, runs)}

    @mcp.tool()
    def experiments() -> list[dict[str, Any]]:
        """All experiments (a method applied to raw files, or to results of other experiments): id, title, method,
        files, last run (status, QC flag counts), proposed corrections waiting for the user; `results` (the tables
        its latest run returned) and `step_results` (its steps' tables, `step:<function>`) to pick from for an input
        that reads other experiments; `reads` [{input, experiment, result}] and `read_by` [{experiment, title,
        input}]; `outdated` [{input, experiment, title, reason}]: a source changed since its latest run (run it
        again to see it; nothing reruns by itself)."""
        everyone = store.list_experiments()
        by_id = {e["id"]: e for e in everyone}
        runs = sources_mod.Runs(store)
        return [{**experiment_summary(e), **linked(e, everyone, by_id, runs)} for e in everyone]

    @mcp.tool()
    def experiment_get(experiment: Annotated[str, Field(description="Experiment id, like e1a2b3c4d.")]
                       ) -> dict[str, Any]:
        """One experiment in full: method, files (with checksums), settings (with the method's defaults for the
        rest), corrections with reasons and status, last run. For each input that reads other experiments,
        `sources` {input: [{experiment, result, title, method, missing, last_run: {id, status, finished} | null,
        has_result}]}; `results`, `step_results`, `reads`, `read_by` and `outdated` as in experiments."""
        e = store.get_experiment(experiment)
        everyone = store.list_experiments()
        by_id = {x["id"]: x for x in everyone}
        runs = sources_mod.Runs(store)
        decl: dict[str, Any] = {}
        try:
            m = method_of(e)
            settings, inputs = form(m)
            problem = None
            try:
                s = methodfile.summary(methodfile.parse(m["source"]))
                decl = {"method_pages": s["pages"], "method_corrections": s["corrections"],
                        "method_takes_corrections": s["takes_corrections"],
                        "method_requirements": s["requirements"], "method_outputs": s["outputs"],
                        "method_version_shown": m.get("version")}
            except SyntaxError:
                pass
        except LabError as err:
            settings, inputs, problem = [], [], str(err)
        resolved = {s["id"]: s["default"] for s in settings}
        resolved.update({k: v for k, v in (e.get("params") or {}).items() if k in resolved})
        files = experiment_inputs(e, [i["id"] for i in inputs])
        kinds = decl.get("method_corrections") or {}
        corrections = [{**c, "what": declarations.describe(kinds, c)} for c in e.get("corrections") or []]
        return {**e, "corrections": corrections, "inputs": {**(e.get("inputs") or {}), **files},
                "resolved_params": resolved, "method_params": settings, "method_inputs": inputs,
                "running": experiment in LIVE, "method_problem": problem, **decl,
                "sources": sources_mod.view(store, e, inputs, by_id, runs), **linked(e, everyone, by_id, runs)}

    def _apply_files(e: dict, inputs: list[dict], files: dict[str, Any] | None) -> None:
        ids = [i["id"] for i in inputs]
        for k, v in (files or {}).items():
            if k not in ids:
                raise LabError(f"The method has no input '{k}'. Inputs: {', '.join(ids) or 'none'}.")
            if isinstance(v, str):
                v = import_path(v)
            if not isinstance(v, dict) or not v.get("path"):
                raise LabError(f"Input {k}: give a path on this PC or a file reference from file_import.")
            e.setdefault("inputs", {})[k] = v

    def checked_params(settings: list[dict], given: dict[str, Any] | None, base: dict[str, Any] | None = None
                       ) -> dict[str, Any]:
        """`base` with the given settings put over it (null: back to the method's default), each checked against
        the method's settings (a rows setting row by row)."""
        out = dict(base or {})
        by_id = {s["id"]: s for s in settings}
        for k, v in (given or {}).items():
            if k not in by_id:
                raise LabError(f"The method has no setting '{k}'. Settings: {', '.join(sorted(by_id))}.")
            if v is None:
                out.pop(k, None)
                continue
            try:
                out[k] = methodfile.check_value(by_id[k], v)
            except ValueError as err:
                raise LabError(str(err)) from None
        return out

    def add_corrections(e: dict, corrections: list[dict] | None, user: bool, by: str) -> None:
        """Corrections to start an experiment with, each checked against the method's kinds (from an agent they
        are only proposed)."""
        kinds = kinds_of(e)
        for c in corrections or []:
            if not str(c.get("reason", "")).strip():
                raise LabError(f"Correction {c} needs a reason.")
            try:
                fields = declarations.check_correction(kinds, str(c.get("kind")), {
                    k: v for k, v in c.items() if k not in ("kind", "reason", "by", "at", "id", "status")})
            except ValueError as err:
                raise LabError(str(err)) from None
            e["corrections"].append({"kind": c["kind"], **fields, "reason": str(c["reason"]).strip(),
                                     "id": new_id("c"), "status": "approved" if user else "proposed",
                                     "by": c.get("by") or by, "at": c.get("at") or now()})

    @mcp.tool()
    async def experiment_create(
        title: Annotated[str, Field(description="Title, e.g. 'Growth curves 06 - wild type and mutant'.")],
        method: Annotated[str, Field(description="Method id (methods lists them).")],
        files: Annotated[dict[str, Any] | None, Field(
            description="Raw files per method input: {input id (e.g. plate_file): absolute path on this PC, or a "
                        "file reference}.")] = None,
        params: Annotated[dict[str, Any] | None, Field(description="Settings (the rest use the method's "
                                                                   "defaults).")] = None,
        corrections: Annotated[list[dict[str, Any]] | None, Field(
            description="Corrections to start with, each {kind, its fields..., reason}; from an agent they are only "
                        "proposed.")] = None,
        sources: Annotated[dict[str, list[dict[str, Any]]] | None, Field(description=SOURCES_HELP)] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create an experiment: a method plus its raw files (or the results of other experiments it reads) and
        settings. Then run it with run."""
        m = store.get_method(method)
        settings, inputs = form(m)
        e = {"id": new_id("e"), "title": title.strip() or "Experiment", "method": method, "inputs": {},
             "params": checked_params(settings, params), "corrections": [], "created": now(),
             "created_by": _who(ctx)}
        add_corrections(e, corrections, _is_user(ctx), _who(ctx))
        e["sources"] = sources_mod.check(store, e, inputs, sources)
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
        sources: Annotated[dict[str, list[dict[str, Any]] | None] | None, Field(
            description=SOURCES_HELP + " Replaces the picks of each input given; null or [] clears one.")] = None,
    ) -> dict[str, Any]:
        """Change an experiment's title, settings, raw files, the results of other experiments it reads, method (or
        pinned version), notes or Methods paragraph. Run again to see the effect."""
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
        e["params"] = checked_params(settings, params, e.get("params"))
        _apply_files(e, inputs, files)
        if method is not None or method_version is not None:  # another method: the picks it can't use go
            takes = {i["id"] for i in inputs if i["kind"] == "result"}
            e["sources"] = {k: v for k, v in sources_mod.picks(e).items() if k in takes}
        if sources is not None:
            e["sources"] = sources_mod.check(store, e, inputs, sources)
        if notes is not None:
            e["notes"] = notes
        if methods_text is not None:
            e["methods_text"] = methods_text
        e["changed"] = now()
        store.save_experiment(e)
        changed("experiment", experiment=experiment)
        return experiment_get(experiment)

    @mcp.tool()
    def experiment_delete(experiment: Annotated[str, Field(description="Experiment id.")],
                          force: Annotated[bool, Field(description="Delete it even when other experiments read its "
                                                                   "results (they then show it as missing).")] = False
                          ) -> dict[str, Any]:
        """Delete an experiment and its runs (raw files stay in the store). When other experiments read its results
        it is refused, naming them (remove it from their inputs first), unless `force`."""
        e = store.get_experiment(experiment)
        everyone = store.list_experiments()
        readers = [r for r in sources_mod.readers(experiment, everyone) if r["experiment"] != experiment]
        if readers and not force:
            names = ", ".join(f"'{r['title'] or r['experiment']}' ({r['experiment']})"
                              for r in {r["experiment"]: r for r in readers}.values())
            raise LabError(f"Other experiments read the results of '{e.get('title') or experiment}': {names}. Remove "
                           "it from their inputs first, or delete it anyway (force): they then show it as missing.")
        store.delete_experiment(experiment)
        for m in store.list_methods():
            if m.get("reference") == experiment:
                store.set_method_settings(m["id"], reference=None)
        changed("experiment", experiment=experiment, deleted=True)
        for r in {r["experiment"] for r in readers}:
            changed("sources", experiment=r, source=experiment, deleted=True)
        return {"deleted": experiment, "readers": readers}

    # ------------------------------------------------------------------ running

    def announce_readers(exp_id: str, run: dict) -> None:
        """Tell the panels that the experiments reading this one have a source with a new run."""
        try:
            readers = sources_mod.readers(exp_id, store.list_experiments())
        except (LabError, OSError):
            return
        for r in {r["experiment"] for r in readers if r["experiment"] != exp_id}:
            changed("sources", experiment=r, source=exp_id, run=run.get("id"), status=run.get("status"))

    def start_run(exp_id: str, time_limit_s: float | None, fresh: bool = False, trial: dict | None = None
                  ) -> asyncio.Future:
        """Start a run in the background. `trial`: {method, params} for a trial run (an unsaved source or unsaved
        settings): it reports like a run, with `trial: true` in its events, and is kept apart (see Runner.run)."""
        e = store.get_experiment(exp_id)
        live = TRIALS if trial else LIVE
        if exp_id in live:
            return live[exp_id]["future"]
        method = trial["method"] if trial else method_of(e)
        if trial and trial.get("params") is not None:
            e = {**e, "params": trial["params"]}
        state: dict[str, Any] = {"run": None, "status": "running", "step": None, "message": "", "started": now(),
                                 "trial": bool(trial)}
        loop = asyncio.get_running_loop()
        extra = {"trial": True} if trial else {}

        def on_change(ev: dict) -> None:
            state["run"], state["step"], state["message"] = ev["run"], ev.get("step"), ev.get("message") or ""
            loop.call_soon_threadsafe(lambda: changed("run", experiment=exp_id, run=ev["run"], step=ev.get("step"),
                                                      message=ev.get("message"), status="running", **extra))

        def work() -> dict:
            return runner.run(e, method, on_change=on_change, time_limit_s=time_limit_s, fresh=fresh,
                              trial=bool(trial), reason="trial run while editing" if trial else None)

        fut = asyncio.ensure_future(asyncio.to_thread(work))
        state["future"] = fut
        live[exp_id] = state

        def done(f: asyncio.Future) -> None:
            live.pop(exp_id, None)
            run = f.result() if not f.exception() else None
            changed("run", experiment=exp_id, run=(run or {}).get("id"), status=(run or {}).get("status", "failed"),
                    finished=True, **extra)
            if run and not trial:  # the experiments reading this one may be out of date now: their screens redraw
                announce_readers(exp_id, run)

        fut.add_done_callback(done)
        TASKS.add(fut)
        fut.add_done_callback(TASKS.discard)
        return fut

    def trial_of(e: dict, source: str | None, settings: dict[str, Any] | None) -> dict:
        """What a trial run runs: the unsaved source (or the experiment's method) and the settings with the unsaved
        ones put over the experiment's (null: the method's default)."""
        method = method_of(e)
        if source is not None:
            parsed(source)
            method = {**method, "source": source if source.endswith("\n") else source + "\n", "draft": True,
                      "version": None}
        params = checked_params(form(method)[0], settings, e.get("params"))
        return {"method": method, "params": params}

    def run_brief(run: dict) -> dict:
        return {"run": run["id"], "experiment": run["experiment"], "status": run["status"], "error": run.get("error"),
                "started": run["started"], "finished": run.get("finished"), "method": run["method"],
                "summary": run.get("summary"), "trial": run.get("trial", False), "ms": run.get("ms"),
                "fresh": bool(run.get("fresh")), "cache": run.get("cache"), "reason": run.get("reason"),
                "settings": run.get("settings"), "notes": run.get("notes") or [], "sources": run.get("sources") or {},
                "steps": [{"function": s.get("function"), "kind": s.get("kind"),
                           "status": s.get("status"),
                           "ms": s.get("ms"), "error": s.get("error"),
                           "table": {"rows": step_output(s).get("rows"), "columns": step_output(s).get("columns")}
                           if step_output(s).get("kind") == "table" else None}
                          for s in run.get("steps") or []],
                "outputs": {n: {k: v for k, v in o.items() if k != "file"}
                            for n, o in (run.get("outputs") or {}).items()}}

    @mcp.tool()
    async def run(experiment: Annotated[str, Field(description="Experiment id.")],
                  wait_s: Annotated[float, Field(description="How long to wait for it to finish (seconds, max 55); "
                                                             "if it is still running, ask run_get later.")] = 50,
                  time_limit_s: Annotated[float | None, Field(description="Stop it after this many seconds "
                                                                          "(default 900).")] = None,
                  fresh: Annotated[bool, Field(description="Calculate every step again instead of reusing the ones "
                                                           "remembered from earlier runs (same code, same inputs).")]
                  = False,
                  source: Annotated[str | None, Field(description="A trial run of this method source (e.g. unsaved "
                                                                  "in the editor) instead of the saved method.")]
                  = None,
                  settings: Annotated[dict[str, Any] | None, Field(description="A trial run with these settings put "
                                                                               "over the experiment's (null: the "
                                                                               "method's default); not saved.")]
                  = None,
                  ) -> dict[str, Any]:
        """Run an experiment's method on its raw files with its settings and approved corrections, in a separate
        calculation process. A step whose code and inputs are the same as in an earlier run isn't calculated again:
        its result is reused and its status is `cached` (fresh=true calculates everything). Returns the run: status,
        each step's status and table, QC flag counts, and for a failed run the method's line and what went wrong.
        Read numbers with result_table, explanations with explain, the flow with its values with flow_get.
        With `source` or `settings` it is a trial run: it doesn't become the experiment's latest run and saves
        nothing; give its run id to flow_get / function_inside / result_table to see
        its values. Only the newest trial of an experiment waits: an older one still waiting comes back
        `replaced`."""
        wait = max(0.0, min(wait_s, 55))
        if source is None and settings is None:
            fut = start_run(experiment, time_limit_s, fresh)
            live = LIVE
        else:
            plan = trial_of(store.get_experiment(experiment), source, settings)
            asked = TRIAL_ASKED[experiment] = TRIAL_ASKED.get(experiment, 0) + 1
            while experiment in TRIALS:  # one trial at a time per experiment; a newer request replaces this one
                try:
                    await asyncio.wait_for(asyncio.shield(TRIALS[experiment]["future"]), timeout=wait)
                except TimeoutError:
                    return {"run": None, "status": "waiting", "trial": True,
                            "note": "Another trial run of this experiment is still calculating: ask again."}
                except Exception:  # noqa: S110  (that run's failure is its own; this one starts now)
                    pass
                if TRIAL_ASKED.get(experiment) != asked:
                    return {"run": None, "status": "replaced", "trial": True}
            fut = start_run(experiment, time_limit_s, fresh, plan)
            live = TRIALS
        try:
            r = await asyncio.wait_for(asyncio.shield(fut), timeout=wait)
            return run_brief(r)
        except TimeoutError:
            st = live.get(experiment) or {}
            return {"run": st.get("run"), "status": "running", "step": st.get("step"), "message": st.get("message"),
                    "trial": live is TRIALS}

    @mcp.tool()
    def run_stop(experiment: Annotated[str | None, Field(description="The experiment whose run to stop (default: "
                                                                     "whatever runs).")] = None) -> dict[str, Any]:
        """Stop the calculation that runs now (an endless loop, or too slow): its process is ended and a fresh one
        started; the run is recorded as stopped."""
        if experiment and experiment not in LIVE and experiment not in TRIALS:
            return {"stopped": False, "note": "That experiment isn't running."}
        if pool.stop("you", experiment):
            return {"stopped": True}
        return {"stopped": False, "note": "That experiment's run is waiting for another calculation to finish: it "
                                          "starts after it."} if experiment else {"stopped": False}

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
        table: Annotated[str, Field(description="An output name (scores, rates, flags ...) or 'step:<function>' "
                                                "for a step's table.")],
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
        where: Annotated[dict[str, Any] | None, Field(description="Keep rows whose column equals the value (a list: "
                                                                  "any of them), e.g. {\"group\": \"urban\"}.")]
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
        table: Annotated[str, Field(description="The table the number is in (e.g. rates).")],
        where: Annotated[dict[str, Any], Field(description="Columns that pick one row, e.g. {\"culture\": \"A3\", "
                                                           "\"time_h\": 4}.")],
        column: Annotated[str | None, Field(description="The number's column, e.g. growth_rate_per_h (default: the "
                                                        "last number column).")] = None,
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
    ) -> dict[str, Any]:
        """Where one number came from: the recorded call of the method's function that returned exactly that value
        (for a formula used on a whole column, that row), its inputs and formula with the numbers put in, and in
        turn the calls that gave those inputs."""
        r = pick_run(run, experiment)
        return explainer.provenance(store, r, table, where, column)

    # ------------------------------------------------------------------ curves and corrections

    def resolved_settings(e: dict, m: methodfile.Method) -> dict[str, Any]:
        """The experiment's settings: its own values, the method's defaults for the rest."""
        values = {s["id"]: s["default"] for s in methodfile.settings(m)}
        values.update(e.get("params") or {})
        return values

    def remembered(key: str, compute):
        """A raw file's curves (or its table), kept for the last few files and settings asked for."""
        with CURVES_LOCK:
            if key in CURVES:
                return CURVES[key]
        value = compute()
        with CURVES_LOCK:
            while len(CURVES) >= CURVES_KEPT:
                CURVES.pop(next(iter(CURVES)))
            CURVES[key] = value
        return value

    def table_curve(df: pd.DataFrame, what: str, item: Any, item_column: str | None, x: str, y: str
                    ) -> tuple[np.ndarray, np.ndarray, None]:
        """One item's points from a table with one row per point (x and y columns, a column naming the item)."""
        if not item_column:
            raise LabError("Say which column of the table names the curves (its item column).")
        missing = [c for c in (item_column, x, y) if c not in df.columns]
        if missing:
            raise LabError(f"The table {what} has no column {', '.join(missing)}. Columns: "
                           f"{', '.join(map(str, df.columns))}.")
        part = df[explainer.equals(df[item_column], item)][[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
        part = part.sort_values(x)
        return part[x].to_numpy(float), part[y].to_numpy(float), None

    def file_curve(e: dict, input_id: str, item: Any, item_column: str | None, x: str | None, y: str | None
                   ) -> tuple[np.ndarray, np.ndarray, str | None]:
        """One curve from a raw file of the experiment: a table file read like a table of the run (item, x and y
        columns), any other file by the curve reader registered for its kind (toolbox/registry.py)."""
        import toolbox
        from toolbox import registry
        from toolbox.tables import TABLE_EXTENSIONS

        try:
            m = methodfile.parse(method_of(e)["source"])
        except SyntaxError as err:
            raise LabError(f"The method can't be read (line {err.lineno}): {err.msg}.") from err
        params = [p for p in (m.recipe_function.params if m.recipe_function else []) if p.endswith("_file")]
        files = experiment_inputs(e, params)
        if input_id not in files:
            raise LabError(f"The experiment has no file for the input '{input_id}'.")
        f = files[input_id]
        path = store.path(f["path"])
        st = path.stat()
        ext = Path(f.get("name") or path.name).suffix.lower()
        where = f"{path}|{st.st_size}|{st.st_mtime_ns}"
        if x and y and ext in TABLE_EXTENSIONS:
            df = remembered(f"table|{where}", lambda: toolbox.read_table(path))
            return table_curve(df, f.get("name") or input_id, item, item_column, x, y)
        reader = registry.curve_reader(ext)
        if reader is None:
            hint = "; a table file needs the x and y columns" if ext in TABLE_EXTENSIONS else ""
            raise LabError(f"No curves can be read from a {ext or 'nameless'} file: the toolbox has no curve reader "
                           f"for it{hint}.")
        settings = resolved_settings(e, m)
        key = f"curves|{where}|{ext}|{json.dumps(jsonable(settings), sort_keys=True, default=str)}"

        def read() -> dict[str, Any]:
            toolbox.cache.use(store.root / "cache")
            return {str(jsonable(c["item"])): c for c in reader(path, settings)}

        found = remembered(key, read).get(str(jsonable(item)))
        if found is None:
            raise LabError(f"The file {f.get('name') or input_id} has no curve {item}.")
        return np.asarray(found["x"], float), np.asarray(found["y"], float), found.get("name")

    @mcp.tool()
    async def curve(
        experiment: Annotated[str, Field(description="Experiment id.")],
        item: Annotated[Any, Field(description="Which curve: the item's value (a sample name, a culture, a "
                                               "number...).")],
        table: Annotated[str | None, Field(description="A table of the run with one row per point (an output name "
                                                       "or 'step:<function>').")] = None,
        item_column: Annotated[str | None, Field(description="The table's column naming the curve.")] = None,
        x: Annotated[str | None, Field(description="The table's x column.")] = None,
        y: Annotated[str | None, Field(description="The table's y column.")] = None,
        file: Annotated[str | None, Field(description="Or: a raw file input of the experiment. A table file (CSV, "
                                                      "Excel...) is read with item_column, x and y; any other kind "
                                                      "by the toolbox's curve reader for it.")] = None,
        start: Annotated[float | None, Field(description="From this x; default the start.")] = None,
        end: Annotated[float | None, Field(description="To this x; default the end.")] = None,
        points: Annotated[int, Field(description="At most this many points (min/max kept per bucket).")] = 1200,
        run: Annotated[str | None, Field(description="A run id (default the experiment's latest).")] = None,
    ) -> dict[str, Any]:
        """One curve, for a page's review card: from a table of the run (x and y columns, one curve per item), or
        from a raw file of the experiment. Thinned to `points`: {item, name, x, y}."""
        e = store.get_experiment(experiment)
        if file:
            xs, ys, name = await asyncio.to_thread(file_curve, e, file, item, item_column, x, y)
        else:
            if not (table and item_column and x and y):
                raise LabError("Give the table with its item, x and y columns (or a raw file).")
            r = pick_run(run, experiment)
            xs, ys, name = table_curve(store.run_table(r["id"], table), table, item, item_column, x, y)
        if not len(xs):
            return {"item": item, "x": [], "y": [], "name": name}
        idx = thin(xs, ys, start, end, points)
        return {"item": item, "name": name, "x": [round(float(v), 6) for v in xs[idx]],
                "y": [round(float(v), 6) for v in ys[idx]]}

    @mcp.tool()
    def result_value(
        output: Annotated[str, Field(description="A returned value or text (not a table or figure).")],
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
    ) -> dict[str, Any]:
        """A single value or a text the recipe returned (a number, a test's result, a model summary)."""
        r = pick_run(run, experiment)
        o = (r.get("outputs") or {}).get(output)
        if not o or o.get("kind") not in ("value", "text"):
            raise LabError(f"Run {r['id']} returned no value or text called '{output}'.")
        if o["kind"] == "text":
            return {"run": r["id"], "output": output, "kind": "text",
                    "text": (store.run_dir(r["id"]) / o["file"]).read_text("utf-8")}
        return {"run": r["id"], "output": output, "kind": "value", "value": o.get("value")}

    @mcp.tool()
    def correction_kinds(experiment: Annotated[str, Field(description="Experiment id.")]) -> dict[str, Any]:
        """The kinds of correction the experiment's method declares (CORRECTIONS): each kind's label, help, fields
        (type, optional) and the fields that make two corrections the same decision."""
        e = store.get_experiment(experiment)
        return {"kinds": kinds_of(e)}

    @mcp.tool()
    def correction_propose(
        experiment: Annotated[str, Field(description="Experiment id.")],
        kind: Annotated[str, Field(description="One of the kinds the method declares (correction_kinds lists "
                                               "them).")],
        reason: Annotated[str, Field(description="Why, in a sentence a reviewer understands. Required.")],
        fields: Annotated[dict[str, Any] | None, Field(description="The kind's fields, e.g. {\"respondent\": 17} or "
                                                                   "{\"culture\": \"A3\", \"start_h\": 2.5, "
                                                                   "\"end_h\": 6}.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Record a correction with its reason. From the user's own panel it applies at the next run; from an AI
        agent it is only proposed: the user approves or rejects it in the panel (Corrections). Raw data is never
        changed; every correction is listed in the report. What a correction does is up to the method's code: the
        approved ones reach its recipe's `corrections` input."""
        if not reason.strip():
            raise LabError("Every correction needs a reason.")
        e = store.get_experiment(experiment)
        given = dict(fields or {})
        kinds = kinds_of(e)
        try:
            checked = declarations.check_correction(kinds, kind, given)
        except ValueError as err:
            raise LabError(str(err)) from None
        user = _is_user(ctx)
        c: dict[str, Any] = {"id": new_id("c"), "kind": kind, **checked, "reason": reason.strip(),
                             "status": "approved" if user else "proposed", "by": _who(ctx), "at": now()}
        corr = e.setdefault("corrections", [])
        if user:  # a newer decision on the same thing replaces the older one
            for old in corr:
                if old.get("status") == "approved" and declarations.same_target(kinds, old, c):
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
            kinds = kinds_of(e)
            for old in e["corrections"]:
                if old is not c and old.get("status") == "approved" and declarations.same_target(kinds, old, c):
                    old["status"], old["replaced_by"] = "replaced", c["id"]
        e["changed"] = now()
        store.save_experiment(e)
        changed("corrections", experiment=experiment)
        return {"correction": c}

    # ------------------------------------------------------------------ export

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

    @mcp.tool(name="examples")
    def examples_list() -> dict[str, Any]:
        """The example experiments `lab_sample` can make, to try the plugin without real data: {examples: [{id,
        title, description, method}]}."""
        return {"examples": examples.listing()}

    def example_experiment(title: str, method: str, files, settings: dict | None, corrections: list | None,
                           picks: dict | None, who: str) -> dict:
        """One experiment of an example, saved: its made-up raw files stored, its settings, corrections and the
        results of other experiments it reads. The first one made for a method becomes its reference."""
        m = store.get_method(method)
        settings_form, inputs = form(m)
        e = {"id": new_id("e"), "title": title, "method": method, "inputs": examples.stored_files(store, files),
             "params": checked_params(settings_form, settings), "corrections": [], "created": now(),
             "created_by": who}
        add_corrections(e, corrections, True, "the example")
        e["sources"] = sources_mod.check(store, e, inputs, picks)
        store.save_experiment(e)
        if not store.method_settings(method).get("reference"):
            store.set_method_settings(method, reference=e["id"])
        changed("experiment", experiment=e["id"])
        return e

    @mcp.tool()
    async def lab_sample(which: Annotated[str, Field(description="Which example: its id, as the examples tool lists "
                                                                 "them.")],
                         ctx: Context | None = None) -> dict[str, Any]:
        """Create an example experiment with made-up raw files, to try a method without real data. The first
        example made for a method becomes its reference experiment. An example that compares experiments first
        makes and runs the experiments it reads: their ids come back in `made`."""
        ex = examples.get(which)
        who = _who(ctx)
        made: dict[str, str] = {}
        for key, spec in (getattr(ex, "SOURCES", None) or {}).items():
            e = await asyncio.to_thread(example_experiment, spec["title"], spec["method"], spec.get("files"),
                                        spec.get("settings"), spec.get("corrections"), None, who)
            run = await asyncio.to_thread(runner.run, e, method_of(e))
            changed("run", experiment=e["id"], run=run["id"], status=run["status"], finished=True)
            if run["status"] != "done":
                raise LabError(f"The example's experiment '{spec['title']}' didn't run: "
                               f"{(run.get('error') or {}).get('message')}")
            made[key] = e["id"]
        picks = {i: [{"experiment": made[p["source"]], "result": p["result"]} for p in chosen]
                 for i, chosen in (getattr(ex, "PICKS", None) or {}).items()}
        e = await asyncio.to_thread(example_experiment, ex.EXPERIMENT, ex.METHOD, getattr(ex, "files", None),
                                    getattr(ex, "SETTINGS", None), getattr(ex, "CORRECTIONS", None), picks, who)
        out = experiment_summary(e)
        if made:
            out["made"] = list(made.values())
        return out

    # ------------------------------------------------------------------ the flow, inside a run, code help
    from . import code_tools, flow_tools, inside_tools

    api = SimpleNamespace(runner=runner, changed=changed, parsed=parsed, pick_run=pick_run, method_of=method_of,
                          who=_who, is_user=_is_user, store=store)
    flow_tools.register(mcp, store, api)
    inside_tools.register(mcp, store, api)
    code_tools.register(mcp, store, api)

    return store


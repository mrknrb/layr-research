"""MCP tools for methods, experiments, runs, corrections, explanations and the results database.

Rules the tools enforce (also in the server's instructions for agents):
- Numbers come only from runs: an agent reads results with result_table / provenance and cites the run id.
- Methods change only through method_patch (validated as a whole; every change is a new version with a diff).
- Corrections (renames, exclusions, manual peak boundaries) always carry a reason; one made by an agent is only
  proposed until the user approves it in the panel (correction_decide asks the user when an agent calls it).
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from mcp.server.fastmcp import Context, FastMCP, Image
from pydantic import Field

from . import engine
from .core import REGISTRY, StepError
from .gcassay_import import corrections_from, params_from_config, parse_yaml
from .store import Store, new_id, now, records

LOCK = threading.Lock()  # one run (or raw-file read) at a time: they share the cache
LIVE: dict[str, dict[str, Any]] = {}  # experiment id -> the run in progress
TASKS: set[asyncio.Future] = set()


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


def register(mcp: FastMCP, data_dir: Path, emit) -> Store:
    store = Store(data_dir / "lab")
    runner = engine.Runner(store)

    def changed(what: str, **data: Any) -> None:
        emit("research:lab", {"what": what, **data})

    def experiment_summary(e: dict) -> dict:
        try:
            m = store.get_method(e["method"], e.get("method_version"))
            mname = m.get("name")
        except StepError:
            mname = e["method"] + " (missing)"
        pending = sum(1 for c in e.get("corrections") or [] if c.get("status") == "proposed")
        return {"experiment": e["id"], "title": e.get("title"), "method": e["method"], "method_name": mname,
                "files": {k: v.get("name") for k, v in (e.get("inputs") or {}).items() if isinstance(v, dict)},
                "last_run": e.get("last_run"), "running": e["id"] in LIVE, "proposed_corrections": pending,
                "updated": e.get("updated")}

    def latest_run(exp_id: str) -> dict:
        e = store.get_experiment(exp_id)
        if not e.get("last_run"):
            raise StepError(f"Experiment {exp_id} has not been run yet: call run first.")
        return store.get_run(e["last_run"]["id"])

    def pick_run(run: str | None, experiment: str | None) -> dict:
        if run:
            return store.get_run(run)
        if experiment:
            return latest_run(experiment)
        raise StepError("Give a run id, or an experiment id for its latest run.")

    # ------------------------------------------------------------------ catalogue and methods

    @mcp.tool()
    def step_catalog(category: Annotated[str | None, Field(description="Only this category: GC, Kinetics, "
                                                                        "Tables, Statistics, Plots.")] = None
                     ) -> list[dict[str, Any]]:
        """Every step type a method can use: its id (the step's `type`), what it does, its inputs and outputs
        (ports with kinds: file, injections, table, value, corrections, figure) and its parameters (with defaults).
        Use these exact ids and parameter names in method_create / method_patch; don't invent step types."""
        return [st.describe() for st in REGISTRY.values() if not category or st.category.lower() == category.lower()]

    @mcp.tool()
    def methods() -> list[dict[str, Any]]:
        """The analysis methods (recipes): id, name, latest version, number of steps, inputs, built in or not."""
        return store.list_methods()

    @mcp.tool()
    def method_get(method: Annotated[str, Field(description="Method id, e.g. gc-activity-assay.")],
                   version: Annotated[int | None, Field(description="A version; default the latest.")] = None
                   ) -> dict[str, Any]:
        """A method's full definition: inputs, parameters (with defaults), steps in order (type, inputs as
        references "$input" / "step.port", params where "@name" means the method parameter), outputs (with
        `database: true` for the ones kept in the PhD-wide results database), plus any validation problems."""
        m = store.get_method(method, version)
        return {"method": m, "problems": engine.validate(m, store)}

    @mcp.tool()
    def method_versions(method: Annotated[str, Field(description="Method id.")]) -> list[dict[str, Any]]:
        """Every saved version of a method: number, when, by whom, and what changed."""
        return store.method_versions(method)

    @mcp.tool()
    def method_diff(method: Annotated[str, Field(description="Method id.")],
                    from_version: Annotated[int, Field(description="Older version.")],
                    to_version: Annotated[int | None, Field(description="Newer version; default latest.")] = None
                    ) -> list[str]:
        """What changed between two versions of a method, in plain lines."""
        return engine.diff(store.get_method(method, from_version), store.get_method(method, to_version))

    @mcp.tool()
    def method_create(
        method: Annotated[str, Field(description="New id: lowercase letters, digits and dashes, e.g. growth-rate.")],
        name: Annotated[str, Field(description="Name people read, e.g. 'Growth rate from OD600'.")],
        description: Annotated[str, Field(description="One or two sentences: what it measures and how.")] = "",
        copy_of: Annotated[str | None, Field(description="Start as a copy of this method id.")] = None,
        inputs: Annotated[list[dict[str, Any]] | None, Field(
            description="Its inputs: [{id, kind: 'file', label}] (raw files the experiment supplies).")] = None,
        params: Annotated[list[dict[str, Any]] | None, Field(
            description="Its parameters: [{id, type, label, default, unit?, help?, group?}].")] = None,
        steps: Annotated[list[dict[str, Any]] | None, Field(
            description="Steps in order: [{id, type, label?, note?, inputs: {port: ref}, params: {...}}].")] = None,
        outputs: Annotated[dict[str, Any] | None, Field(
            description="Named results: {name: {from: 'step.port', database?: true, label?}}.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a new method (version 1). Give its steps here or add them later with method_patch. It is
        validated first; with errors nothing is saved and the problems are returned."""
        try:
            store.get_method(method)
            raise StepError(f"A method '{method}' exists already; change it with method_patch.")
        except StepError as e:
            if "exists already" in str(e):
                raise
        base = store.get_method(copy_of) if copy_of else {}
        m = {"id": method, "name": name, "description": description or base.get("description", ""),
             "inputs": inputs if inputs is not None else base.get("inputs", []),
             "params": params if params is not None else base.get("params", []),
             "steps": steps if steps is not None else base.get("steps", []),
             "outputs": outputs if outputs is not None else base.get("outputs", {})}
        probs = engine.validate(m, store)
        if engine.errors(probs):
            return {"saved": False, "problems": probs}
        saved = store.save_method(m, _who(ctx), f"Created{f' as a copy of {copy_of}' if copy_of else ''}")
        changed("method", method=method)
        return {"saved": True, "method": method, "version": saved["version"], "problems": probs}

    @mcp.tool()
    def method_patch(
        method: Annotated[str, Field(description="Method id.")],
        ops: Annotated[list[dict[str, Any]], Field(description=(
            "Operations, applied in order: {op: 'add', step: {id, type, inputs, params, label?, note?}, after?: id, "
            "before?: id, first?: true} · {op: 'remove', step: id} · {op: 'move', step: id, after?|before?|first?} · "
            "{op: 'set', step: id, params?: {...}, inputs?: {...}, label?, note?} (merged; null removes a key) · "
            "{op: 'method', name?, description?, inputs?, params?, outputs?} · {op: 'add_param', param: {...}} · "
            "{op: 'remove_param', param: id} · {op: 'output', name, from: 'step.port' | null, database?}"))],
        change: Annotated[str, Field(description="One sentence: why, for the version history.")],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Change a method with a batch of operations, checked as a whole: with errors nothing is saved and the
        problems come back (fix and send the whole batch again). Saved changes become a new version (the user sees
        the diff and can go back). Built-in methods get their changed copy as the next version."""
        old = store.get_method(method)
        new = engine.apply_patch(old, ops)
        probs = engine.validate(new, store)
        lines = engine.diff(old, new)
        if engine.errors(probs):
            return {"saved": False, "problems": probs, "diff": lines}
        if not lines:
            return {"saved": False, "problems": probs, "diff": [], "note": "Nothing changed."}
        saved = store.save_method(new, _who(ctx), change)
        changed("method", method=method, version=saved["version"])
        return {"saved": True, "version": saved["version"], "diff": lines, "problems": probs}

    @mcp.tool()
    def method_revert(method: Annotated[str, Field(description="Method id.")],
                      version: Annotated[int, Field(description="The version to go back to.")],
                      ctx: Context | None = None) -> dict[str, Any]:
        """Go back to an earlier version of a method (saved as a new version, so nothing is lost)."""
        old = store.get_method(method, version)
        cur = store.get_method(method)
        saved = store.save_method(old, _who(ctx), f"Back to version {version}")
        changed("method", method=method, version=saved["version"])
        return {"version": saved["version"], "diff": engine.diff(cur, old)}

    # ------------------------------------------------------------------ files

    def import_path(path: str) -> dict:
        p = Path(path).expanduser()
        if not p.is_file():
            raise StepError(f"No file at {p}.")
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
        parsed = urllib.parse.urlsplit(url)
        hub = urllib.parse.urlsplit(os.environ.get("LAYR_HUB_URL") or "http://127.0.0.1:4300/mcp")
        if not parsed.path.startswith("/_layr/files/"):
            raise StepError("That is not a layr file address.")
        target = f"{hub.scheme}://{hub.netloc}{parsed.path}?{parsed.query}"
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
            raise StepError("Bad upload id.")
        with open(store.root / "uploads" / uid, "ab") as f:
            f.write(base64.b64decode(data))
        return {"upload": uid, "size": (store.root / "uploads" / uid).stat().st_size}

    @mcp.tool()
    async def upload_finish(upload: Annotated[str, Field(description="Upload id.")],
                            name: Annotated[str, Field(description="The file's name, e.g. run5.gaml.")]
                            ) -> dict[str, Any]:
        """For the Research panel: put a sent file into the store; returns its file reference."""
        if not upload.startswith("u") or not upload[1:].isalnum():
            raise StepError("Bad upload id.")
        p = store.root / "uploads" / upload
        if not p.exists():
            raise StepError("No such upload.")
        return await asyncio.to_thread(store.add_file, p, name, True)

    # ------------------------------------------------------------------ experiments

    @mcp.tool()
    def experiments() -> list[dict[str, Any]]:
        """All experiments (a method applied to raw files): id, title, method, files, last run (status, QC flag
        counts), proposed corrections waiting for the user."""
        return [experiment_summary(e) for e in store.list_experiments()]

    @mcp.tool()
    def experiment_get(experiment: Annotated[str, Field(description="Experiment id, like e1a2b3c4d.")]
                       ) -> dict[str, Any]:
        """One experiment in full: method, files (with checksums), parameter values (and the method's defaults for
        the rest), corrections with reasons and status, last run."""
        e = store.get_experiment(experiment)
        m = store.get_method(e["method"], e.get("method_version"))
        return {**e, "resolved_params": engine.param_values(m, e.get("params")),
                "method_params": m.get("params"), "method_inputs": m.get("inputs"), "running": experiment in LIVE}

    def _apply_files(e: dict, m: dict, files: dict[str, Any] | None) -> None:
        ids = {i["id"] for i in m.get("inputs") or []}
        for k, v in (files or {}).items():
            if k not in ids:
                raise StepError(f"Method {m['id']} has no input '{k}'. Inputs: {', '.join(ids)}.")
            if isinstance(v, str):
                v = import_path(v)
            if not isinstance(v, dict) or not v.get("path"):
                raise StepError(f"Input {k}: give a path on this PC or a file reference from file_import.")
            e.setdefault("inputs", {})[k] = v

    @mcp.tool()
    async def experiment_create(
        title: Annotated[str, Field(description="Title, e.g. 'Activity assay 06 - VLB120 ΔalkB'.")],
        method: Annotated[str, Field(description="Method id (methods lists them).")] = "gc-activity-assay",
        files: Annotated[dict[str, Any] | None, Field(
            description="Raw files per method input: {input id: absolute path on this PC, or a file reference}.")
        ] = None,
        params: Annotated[dict[str, Any] | None, Field(description="Parameter values (the rest use the method's "
                                                                   "defaults).")] = None,
        gcassay_config: Annotated[str | None, Field(
            description="Instead: path of a gcassay experiment config (YAML); its parameters, raw file and "
                        "corrections file are taken over.")] = None,
        corrections: Annotated[list[dict[str, Any]] | None, Field(
            description="Corrections to start with (as gcassay_settings returns them); from an agent they are "
                        "only proposed.")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create an experiment: a method plus its raw files and parameter values. Then run it with run."""
        m = store.get_method(method)
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
                raise StepError(f"Correction {c} needs a kind (rename, exclude, manual_integration) and a reason.")
            e["corrections"].append({**c, "id": new_id("c"), "status": "approved" if user else "proposed",
                                     "by": c.get("by") or _who(ctx), "at": c.get("at") or now()})
        e["changed"] = now()
        await asyncio.to_thread(_apply_files, e, m, files)
        store.save_experiment(e)
        changed("experiment", experiment=e["id"])
        return experiment_summary(e)

    @mcp.tool()
    def experiment_update(
        experiment: Annotated[str, Field(description="Experiment id.")],
        title: Annotated[str | None, Field(description="New title.")] = None,
        params: Annotated[dict[str, Any] | None, Field(description="Parameter values to set (merged; null resets "
                                                                   "one to the method default).")] = None,
        files: Annotated[dict[str, Any] | None, Field(description="Replace raw files: {input id: path or file "
                                                                  "reference}.")] = None,
        method_version: Annotated[int | None, Field(description="Pin a method version (0: always the latest).")]
        = None,
        notes: Annotated[str | None, Field(description="Free notes about the experiment.")] = None,
    ) -> dict[str, Any]:
        """Change an experiment's title, parameter values, raw files, pinned method version or notes. Run again to
        see the effect."""
        e = store.get_experiment(experiment)
        m = store.get_method(e["method"], e.get("method_version"))
        if title is not None:
            e["title"] = title.strip() or e["title"]
        known = {p["id"] for p in m.get("params") or []}
        for k, v in (params or {}).items():
            if k not in known:
                raise StepError(f"Method {m['id']} has no parameter '{k}'. Parameters: {', '.join(sorted(known))}.")
            if v is None:
                e.setdefault("params", {}).pop(k, None)
            else:
                e.setdefault("params", {})[k] = v
        _apply_files(e, m, files)
        if method_version is not None:
            e["method_version"] = method_version or None
        if notes is not None:
            e["notes"] = notes
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
        """Read a gcassay config (and corrections file) into this plugin's parameter values and corrections,
        without saving anything (the panel uses it to fill an experiment)."""
        p, info = params_from_config(parse_yaml(config, "gcassay config"))
        corr = corrections_from(parse_yaml(corrections, "corrections file")) if corrections else []
        return {"params": p, "title": info["title"], "raw_file": info.get("raw_file"), "corrections": corr}

    @mcp.tool()
    def experiment_delete(experiment: Annotated[str, Field(description="Experiment id.")]) -> dict[str, Any]:
        """Delete an experiment, its runs and its rows in the results database (raw files stay in the store)."""
        store.get_experiment(experiment)
        store.delete_experiment(experiment)
        changed("experiment", experiment=experiment, deleted=True)
        return {"deleted": experiment}

    @mcp.tool()
    def method_delete(method: Annotated[str, Field(description="Method id.")]) -> dict[str, Any]:
        """Delete a method made here and all its versions (built-in methods return to their shipped version)."""
        used = [e["id"] for e in store.list_experiments() if e.get("method") == method]
        if used and method not in {m["id"] for m in store.list_methods() if m["builtin"]}:
            raise StepError(f"Experiments use this method: {', '.join(used)}.")
        store.delete_method(method)
        changed("method", method=method, deleted=True)
        return {"deleted": method}

    # ------------------------------------------------------------------ running

    def start_run(exp_id: str) -> asyncio.Future:
        e = store.get_experiment(exp_id)
        if exp_id in LIVE:
            return LIVE[exp_id]["future"]
        state: dict[str, Any] = {"run": None, "status": "running", "step": None, "message": "", "started": now()}
        loop = asyncio.get_running_loop()

        def progress(msg: str) -> None:
            state["message"] = msg

        def on_change(ev: dict) -> None:
            state["run"], state["step"] = ev["run"], ev["step"]
            loop.call_soon_threadsafe(lambda: changed("run", experiment=exp_id, run=ev["run"], step=ev["step"],
                                                      status=ev["status"]))

        def work() -> dict:
            with LOCK:
                return runner.run_experiment(e, progress, on_change)

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
                "summary": run.get("summary"),
                "steps": [{"id": s["id"], "type": s["type"], "status": s["status"], "ms": s.get("ms"),
                           "cached": s.get("cached"), "error": s.get("error"),
                           "tables": {p: {"rows": o.get("rows"), "columns": o.get("columns")}
                                      for p, o in (s.get("outputs") or {}).items() if o.get("kind") == "table"}}
                          for s in run.get("steps") or []],
                "outputs": list(run.get("outputs") or {})}

    @mcp.tool()
    async def run(experiment: Annotated[str, Field(description="Experiment id.")],
                  wait_s: Annotated[float, Field(description="How long to wait for it to finish (seconds, max 55); "
                                                             "if it is still running, ask run_get later.")] = 50
                  ) -> dict[str, Any]:
        """Run an experiment's method on its raw files with its parameters and approved corrections. Steps whose
        inputs didn't change come from the cache, so a rerun after a correction is fast. Returns the run: status,
        each step's status and tables, QC flag counts. Read numbers with result_table, explanations with explain."""
        fut = start_run(experiment)
        try:
            r = await asyncio.wait_for(asyncio.shield(fut), timeout=max(0.0, min(wait_s, 55)))
            return run_brief(r)
        except TimeoutError:
            st = LIVE.get(experiment) or {}
            return {"run": st.get("run"), "status": "running", "step": st.get("step"), "message": st.get("message")}

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
    def runs(experiment: Annotated[str | None, Field(description="Only this experiment's runs.")] = None
             ) -> list[dict[str, Any]]:
        """Runs, newest first: id, experiment, status, times, method version, QC summary."""
        return store.list_runs(experiment)

    # ------------------------------------------------------------------ results

    @mcp.tool()
    def result_table(
        table: Annotated[str, Field(description="An output name (activities, concentrations, time_course, peaks, "
                                                "calibration, flags ...) or 'step.port'.")],
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
                raise StepError(f"No column '{c}'. Columns: {', '.join(map(str, df.columns))}.")
            vals = v if isinstance(v, list) else [v]
            df = df[df[c].astype(str).isin([str(x) for x in vals])]
        if columns:
            missing = [c for c in columns if c not in df.columns]
            if missing:
                raise StepError(f"No column {missing}. Columns: {', '.join(map(str, df.columns))}.")
            df = df[columns]
        total = len(df)
        part = df.iloc[max(0, offset): max(0, offset) + max(1, min(limit, 2000))]
        return {"run": r["id"], "table": table, "total": total, "columns": [str(c) for c in df.columns],
                "rows": records(part)}

    @mcp.tool(structured_output=False)
    def figure(run: Annotated[str | None, Field(description="Run id.")] = None,
               experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
               output: Annotated[str, Field(description="Figure output name or 'step.port'.")] = "figure") -> Image:
        """A figure a run drew (PNG)."""
        r = pick_run(run, experiment)
        f = store.output_file(r, output)
        if not f or f.get("kind") != "figure":
            raise StepError(f"Run {r['id']} has no figure '{output}'.")
        return Image(data=(store.run_dir(r["id"]) / f["file"]).read_bytes(), format="png")

    @mcp.tool()
    def explain(
        experiment: Annotated[str | None, Field(description="Explain this experiment's method with its parameter "
                                                            "values (and its latest run's numbers).")] = None,
        method: Annotated[str | None, Field(description="Or explain a method in general (its defaults).")] = None,
        step: Annotated[str | None, Field(description="Only this step id.")] = None,
        run: Annotated[str | None, Field(description="Use this run's numbers instead of the latest.")] = None,
        general: Annotated[bool, Field(description="true: formulas and rules only, no run numbers.")] = False,
    ) -> dict[str, Any]:
        """How every number is calculated: per step a plain sentence, formulas (LaTeX), symbols with units, the rules
        it follows, assumptions, the library call, and (run mode) the formulas with this run's numbers substituted.
        Also the Methods paragraph. Use this to explain an analysis to the user in their words."""
        rr = None
        if experiment:
            e = store.get_experiment(experiment)
            m = store.get_method(e["method"], e.get("method_version"))
            values = e.get("params")
            if not general:
                rr = store.get_run(run) if run else (store.get_run(e["last_run"]["id"]) if e.get("last_run") else None)
                if rr:
                    m = store.get_method(rr["method"]["id"], rr["method"]["version"])
                    values = rr.get("params")
        elif method:
            m = store.get_method(method)
            values = None
        else:
            raise StepError("Give an experiment or a method.")
        sheets = engine.explain_method(store, m, values, rr)
        if step:
            sheets = [s for s in sheets if s["step"] == step]
            if not sheets:
                raise StepError(f"No step '{step}' in {m['id']}.")
        return {"method": {"id": m["id"], "name": m.get("name"), "version": m["version"],
                           "description": m.get("description")},
                "run": rr["id"] if rr else None, "steps": sheets,
                "methods_text": engine.methods_text(store, m, values, rr)}

    @mcp.tool()
    def provenance(
        table: Annotated[str, Field(description="The table the number is in (e.g. concentrations).")],
        where: Annotated[dict[str, Any], Field(description="Columns that pick one row, e.g. {\"injection\": 12, "
                                                           "\"compound\": \"HHAME\"}.")],
        run: Annotated[str | None, Field(description="Run id.")] = None,
        experiment: Annotated[str | None, Field(description="Or an experiment id: its latest run.")] = None,
    ) -> dict[str, Any]:
        """Where one result came from: every step on the way that has this row (peak, ratio, concentration ...),
        with its formula and this row's numbers substituted."""
        r = pick_run(run, experiment)
        return {"run": r["id"], "chain": engine.provenance(store, r, table, where)}

    # ------------------------------------------------------------------ chromatograms and corrections

    def sequence_of(e: dict):
        f = (e.get("inputs") or {}).get("raw")
        if not f:
            raise StepError("This experiment has no GC sequence file.")
        channel = engine.param_values(store.get_method(e["method"], e.get("method_version")), e.get("params")) \
            .get("channel")
        with LOCK:
            out = REGISTRY["gc.read_gaml"].run(engine.Ctx(cache=runner.cache, store_path=store.path),
                                               {"file": f}, {"channel": channel})
        return out.outputs["injections"]

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
            raise StepError(f"No injection {injection} (1-{len(seq)}).")
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
                peaks = records(pk[["compound", "status", "rt_expected", "rt_tolerance", "rt", "start", "end", "area",
                                    "height", "snr", "baseline_start_y", "baseline_end_y", "note", "vendor_rt",
                                    "vendor_area"]])
            except (StepError, KeyError):
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
            raise StepError("Every correction needs a reason.")
        if kind == "manual_integration":
            if not compound or start_min is None or end_min is None or end_min <= start_min:
                raise StepError("A manual integration needs the compound and a start before the end (min).")
        if kind == "rename" and not (new_name or "").strip():
            raise StepError("A rename needs the new name.")
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
            raise StepError(f"No correction {correction}.")
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
        """The PhD-wide results database: one table per method output marked for it (activities, time_course,
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
                        "with the Methods text, explanations and results")],
        experiment: Annotated[str | None, Field(description="Experiment id (its latest run).")] = None,
        run: Annotated[str | None, Field(description="Or a run id.")] = None,
        inline: Annotated[bool, Field(description="Also return the file's bytes (base64), for the panel.")] = False,
    ) -> dict[str, Any]:
        """Write a report of a run: the method explained step by step (formulas with this run's numbers), the
        Methods paragraph, results, figures, QC flags, every correction with its reason, checksums and versions.
        Returns where it was saved in the plugin's exports folder."""
        from . import export as exp_mod

        r = pick_run(run, experiment)
        e = store.get_experiment(r["experiment"])
        path = await asyncio.to_thread(exp_mod.write, store, e, r, format)
        out: dict[str, Any] = {"path": str(path), "name": path.name, "size": path.stat().st_size}
        if inline:
            out["data"] = base64.b64encode(path.read_bytes()).decode()
        return out

    @mcp.tool()
    def lab_sample(ctx: Context | None = None) -> dict[str, Any]:
        """Create the example GC experiment (a made-up activity assay: substrate SUB turned into product PROD, internal
        standard octanol), to try the methods without real data."""
        from .sample import make_sample

        f = make_sample(store)
        e = {"id": new_id("e"), "title": "Example: made-up GC activity assay", "method": "gc-activity-assay",
             "inputs": {"raw": f}, "params": SAMPLE_PARAMS, "corrections": [], "created": now(),
             "created_by": _who(ctx)}
        store.save_experiment(e)
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

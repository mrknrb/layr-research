"""Running an experiment: its settings and files go to the calculation process (lab/worker.py), the answer becomes
a run (run.json, tables, figures, the record of every call).

The calculation process is started ahead of time and kept warm, so a run doesn't wait for pandas and SciPy to load.
One run at a time; `stop()` ends it (the process is killed and a fresh one started), and so does the time limit.

A step whose code and inputs are the same as in an earlier run isn't calculated again (`<store>/cache/steps/`, see
lab/recorder.py's StepCache): it is marked "cached" in the run's steps. `fresh=True` calculates every step (the
reference comparison and anything that proves numbers use it). A trial run (`trial=True`, also of an unsaved
source) doesn't become the experiment's latest run.

Runs pile up: `prune_runs` (every few runs) keeps the newest `TRIAL_KEEP` trial runs of each experiment and lets
only the newest `INSIDE_KEEP` runs of each experiment (and its latest run) keep the values inside their steps
(inside.json, values/); older runs keep everything else: their settings, steps' tables, outputs and calls.
"""

from __future__ import annotations

import functools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable

import pandas as pd

from . import methodfile
from .errors import LabError
from .store import Store, jsonable, new_id, now

WORKER = Path(__file__).resolve().parent / "worker.py"
BACKEND = WORKER.parent.parent
TIME_LIMIT_S = 900
TRIAL_KEEP = 20  # trial runs kept per experiment (the newest)
INSIDE_KEEP = 5  # runs per experiment that keep the values inside their steps (the newest; the latest run always)
PRUNE_EVERY = 10  # runs between two tidy-ups


class Stopped(Exception):
    """The run was stopped: by the user, or by the time limit."""


class Worker:
    """One calculation process and a thread reading its answers."""

    def __init__(self) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen([sys.executable, "-u", str(WORKER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=None, cwd=str(BACKEND), env=env,
                                     creationflags=flags)
        self.lines: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.paths: list[str] = []  # the method packages' folders this process has on its path
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg.get("ready"):
                self.ready.set()
                continue
            self.lines.put(msg)
        self.lines.put(None)  # the process ended

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, job: dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(jsonable(job), ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


class Pool:
    """Keeps one warm calculation process; runs one job at a time in it."""

    def __init__(self) -> None:
        self._warm: Worker | None = None
        self._lock = threading.Lock()  # one job at a time
        self._guard = threading.Lock()
        self.current: Worker | None = None
        self.current_job: dict[str, Any] | None = None
        self.stop_reason: str | None = None

    def warm(self) -> None:
        """Start a process now (in the background), so the next run doesn't wait for it."""
        with self._guard:
            if self._warm is None or not self._warm.alive():
                try:
                    self._warm = Worker()
                except OSError:
                    self._warm = None

    def _take(self) -> Worker:
        with self._guard:
            w = self._warm if self._warm and self._warm.alive() else None
            self._warm = None
        return w or Worker()

    def run(self, job: dict[str, Any], progress: Callable[[dict], None] | None = None,
            time_limit_s: float | None = None) -> dict[str, Any]:
        with self._lock:
            w = self._take()
            paths = list(job.get("paths") or [])
            if paths and w.paths and w.paths != paths:  # another method's packages are loaded: start clean
                w.kill()
                w = Worker()
            if paths:
                w.paths = paths
            self.current, self.current_job = w, job
            self.stop_reason = None
            limit = time_limit_s or TIME_LIMIT_S
            try:
                started = time.monotonic()
                while not w.ready.wait(timeout=0.25):
                    if self.stop_reason:
                        w.kill()
                        raise Stopped(self.stop_reason)
                    if not w.alive():
                        raise RuntimeError(f"The calculation process couldn't start (exit code {w.proc.wait()}).")
                    if time.monotonic() - started > 120:
                        w.kill()
                        raise RuntimeError("The calculation process didn't start in 2 minutes.")
                deadline = time.monotonic() + limit
                w.send(job)
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.stop_reason = self.stop_reason or f"the time limit ({limit:g} s)"
                        w.kill()
                        raise Stopped(self.stop_reason)
                    try:
                        msg = w.lines.get(timeout=min(left, 0.5))
                    except queue.Empty:
                        if self.stop_reason:
                            w.kill()
                            raise Stopped(self.stop_reason)
                        continue
                    if msg is None:
                        if self.stop_reason:
                            raise Stopped(self.stop_reason)
                        raise RuntimeError(f"The calculation process ended unexpectedly (exit code "
                                           f"{w.proc.wait()}): the method may have used up the memory, or crashed "
                                           "Python.")
                    if "progress" in msg and progress:
                        progress(msg)
                    if "done" in msg:
                        return msg["done"]
            finally:
                self.current = self.current_job = None
                if w.alive() and not self.stop_reason:
                    with self._guard:
                        if self._warm is None:
                            self._warm = w
                        else:
                            w.kill()
                else:
                    w.kill()
                    threading.Thread(target=self.warm, daemon=True).start()

    def stop(self, reason: str = "you", experiment: str | None = None) -> bool:
        """Stop the job running now; with `experiment`, only when it is that experiment's (another one's run may be
        calculating while this one waits)."""
        w = self.current
        if w is None or (experiment and (self.current_job or {}).get("experiment") != experiment):
            return False
        self.stop_reason = reason
        w.kill()
        return True

    def close(self) -> None:
        for w in (self._warm, self.current):
            if w:
                w.kill()


# ---------------------------------------------------------------------------------------------------- runs


def approved_corrections(exp: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in exp.get("corrections") or [] if c.get("status") == "approved"]


def experiment_inputs(exp: dict[str, Any], file_params: list[str]) -> dict[str, Any]:
    """The experiment's raw file for each file input it has one for."""
    given = {k: v for k, v in (exp.get("inputs") or {}).items() if isinstance(v, dict)}
    return {p: given[p] for p in file_params if p in given}


@functools.lru_cache(maxsize=8)
def _parse(source: str) -> methodfile.Method:
    """The method read once per source (a recalculation after every change reads the same one again)."""
    return methodfile.parse(source)


def resolve(store: Store, exp: dict[str, Any], source: str) -> dict[str, Any]:
    """What the recipe gets from this experiment: {recipe, kinds, args, params (shown), inputs, notes}."""
    try:
        parsed = _parse(source)
    except SyntaxError as e:
        raise LabError(f"The method can't be read (line {e.lineno}): {e.msg}. Fix it in the method's editor.") from e
    fn = parsed.recipe_function
    if fn is None:
        raise LabError("The method has no recipe function to run.")
    files = [p for p in fn.params if p.endswith("_file")]
    inputs = experiment_inputs(exp, files)
    given = exp.get("params") or {}
    args: dict[str, Any] = {}
    shown: dict[str, Any] = {}
    for p in fn.params:
        if p == "corrections":
            args[p] = approved_corrections(exp)
        elif p in files:
            f = inputs.get(p)
            if f is None:
                if p in fn.defaults:
                    continue
                raise LabError(f"The experiment has no file for the input `{p}`: choose one in its Settings.")
            path = store.path(f["path"])
            if not path.exists():
                raise LabError(f"The raw file {f.get('name') or f['path']} is missing from the store.")
            args[p] = str(path)
        elif p in given:
            args[p] = given[p]
            shown[p] = given[p]
        elif p not in fn.defaults:
            raise LabError(f"The setting `{p}` has no value: set it in the experiment's Settings.")
        else:
            d = fn.defaults[p]
            shown[p] = None if d is methodfile.UNKNOWN else d
    notes = [f"The setting `{k}` was left out: the method has no such setting any more." for k in given
             if k not in fn.params]
    kinds = {f.name: f.kind for f in parsed.functions}
    labels = fn.doc.returns
    from .pages import requirements

    return {"recipe": fn.name, "kinds": kinds, "args": args, "params": shown, "inputs": inputs, "notes": notes,
            "labels": labels, "requirements": requirements(parsed.constants)}


def versions_here() -> dict[str, str]:
    import platform

    import matplotlib
    import numpy
    import scipy

    return {"python": platform.python_version(), "pandas": pd.__version__, "numpy": numpy.__version__,
            "scipy": scipy.__version__, "matplotlib": matplotlib.__version__}


class Runner:
    def __init__(self, store: Store, pool: Pool | None = None, remember_steps: bool = True, record_inside: bool = True):
        self.store = store
        self.pool = pool or Pool()
        self.remember_steps = remember_steps
        self.record_inside = record_inside
        self._since_prune = PRUNE_EVERY - 1  # the first run tidies up

    def run(self, exp: dict[str, Any], method: dict[str, Any], *, trial: bool = False,
            on_change: Callable[[dict], None] | None = None, time_limit_s: float | None = None,
            reason: str | None = None, fresh: bool = False) -> dict[str, Any]:
        """Run `method` (a stored version, a draft, or any {"id", "source"} such as an unsaved source in the editor)
        on an experiment. A trial run (a comparison after a change, a recalculation while editing) is kept apart: it
        doesn't become the experiment's latest run. `fresh` calculates every step
        instead of reusing remembered ones (it still remembers them for the next run)."""
        store = self.store
        info = resolve(store, exp, method["source"])
        run = {"id": new_id("r"), "experiment": exp["id"], "title": exp.get("title"), "status": "running",
               "trial": trial, "reason": reason, "started": now(), "finished": None, "fresh": fresh,
               "inside": self.record_inside,
               "method": {"id": method["id"], "version": method.get("version"), "name": method.get("name"),
                          "draft": bool(method.get("draft"))},
               "params": info["params"], "notes": info["notes"],
               "settings": {k: v for k, v in info["args"].items() if not methodfile.is_input(k)},
               "inputs": {k: {kk: v.get(kk) for kk in ("name", "sha256", "size")} for k, v in info["inputs"].items()},
               "corrections": approved_corrections(exp), "steps": [], "outputs": {}, "versions": versions_here()}
        rdir = store.run_dir(run["id"])
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "method.py").write_text(method["source"], "utf-8", newline="\n")
        store.save_run(run)
        job = {"job": "run", "source": str(rdir / "method.py"), "run_dir": str(rdir), "recipe": info["recipe"],
               "kinds": info["kinds"], "args": info["args"], "cache_dir": str(store.root / "cache"),
               "step_cache": str(store.root / "cache" / "steps") if self.remember_steps else None,
               "fresh": fresh, "inside": self.record_inside, "experiment": exp["id"]}

        def progress(msg: dict) -> None:
            run["step"] = msg.get("step")
            if on_change:
                on_change({"run": run["id"], "experiment": exp["id"], "step": msg.get("step"), "status": "running",
                           "message": msg.get("progress")})

        try:
            if info["requirements"]:
                from . import packages

                where = packages.ready(info["requirements"]) or packages.install(
                    info["requirements"], lambda text: progress({"progress": text, "step": None}))
                job["paths"] = [str(where)]
                run["requirements"] = info["requirements"]
            reply = self.pool.run(job, progress, time_limit_s)
            run["status"] = reply.get("status", "failed")
            run["steps"] = reply.get("steps") or []
            run["outputs"] = reply.get("outputs") or {}
            run["ms"] = reply.get("ms")
            if reply.get("versions"):
                run["versions"] = reply["versions"]
            if reply.get("cache"):
                run["cache"] = reply["cache"]
            if reply.get("error"):
                run["error"] = reply["error"]
        except Stopped as e:
            run["status"] = "stopped"
            run["error"] = {"message": f"Stopped by {e}.", "type": "Stopped"}
        except Exception as e:  # the process itself failed
            run["status"] = "failed"
            run["error"] = {"message": str(e), "type": type(e).__name__}
        for name, o in run["outputs"].items():
            if info["labels"].get(name):
                o["label"] = info["labels"][name]
        run.pop("step", None)
        run["finished"] = now()
        run["summary"] = self._summary(run, rdir)
        store.save_run(run)
        if not trial:
            e = store.get_experiment(exp["id"])
            e["last_run"] = {"id": run["id"], "status": run["status"], "started": run["started"],
                             "finished": run["finished"], "summary": run["summary"],
                             "error": (run.get("error") or {}).get("message"),
                             "error_at": {k: (run.get("error") or {}).get(k) for k in ("line", "function")},
                             "method_version": run["method"]["version"]}
            store.save_experiment(e)
        self._since_prune += 1
        if self._since_prune >= PRUNE_EVERY:
            self._since_prune = 0
            try:
                prune_runs(store)
            except Exception as ex:  # tidying up must never break a run
                print(f"couldn't tidy up old runs: {ex}", file=sys.stderr)
        return run

    @staticmethod
    def _summary(run: dict[str, Any], rdir: Path) -> dict[str, Any]:
        failed = next((s["function"] for s in run["steps"] if s.get("status") == "failed"), None)
        summary: dict[str, Any] = {"steps": len(run["steps"]), "failed_step": failed}
        flags = run["outputs"].get("flags")
        if run["status"] == "done" and flags and flags.get("kind") == "table":
            try:
                fl = pd.read_csv(rdir / flags["file"])
                summary["flags"] = {str(k): int(v) for k, v in fl.level.value_counts().items()} if len(fl) else {}
            except (OSError, pd.errors.EmptyDataError, AttributeError):
                summary["flags"] = {}
        return summary


def prune_runs(store: Store, trial_keep: int = TRIAL_KEEP, inside_keep: int = INSIDE_KEEP) -> dict[str, int]:
    """Delete old trial runs and the values inside the steps of old runs (see the module's docstring)."""
    import shutil

    def started(r: dict[str, Any]) -> tuple:  # runs started in the same second: by when run.json was written
        try:
            written = (store.run_dir(r["id"]) / "run.json").stat().st_mtime_ns
        except OSError:
            written = 0
        return r.get("started") or "", written

    by_exp: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(store.list_runs(None, trials=True), key=started, reverse=True):  # newest first
        by_exp.setdefault(r.get("experiment") or "", []).append(r)
    latest = {}
    for e in store.list_experiments():
        if e.get("last_run"):
            latest[e["id"]] = e["last_run"].get("id")
    deleted = cleared = 0
    for exp_id, runs in by_exp.items():
        trials = [r for r in runs if r.get("trial") and r.get("status") != "running"]
        for r in trials[trial_keep:]:
            shutil.rmtree(store.run_dir(r["id"]), ignore_errors=True)
            deleted += 1
        gone = {r["id"] for r in trials[trial_keep:]}
        left = [r for r in runs if r["id"] not in gone]
        for r in left[inside_keep:]:
            if r["id"] == latest.get(exp_id) or r.get("status") == "running":
                continue
            rdir = store.run_dir(r["id"])
            if not (rdir / "inside.json").exists() and not (rdir / "values").exists():
                continue
            (rdir / "inside.json").unlink(missing_ok=True)
            shutil.rmtree(rdir / "values", ignore_errors=True)
            try:
                full = store.get_run(r["id"])
                full["inside"] = "cleared"
                store.save_run(full)
            except (LabError, OSError, ValueError):
                pass
            cleared += 1
    return {"deleted_trials": deleted, "cleared_inside": cleared}

"""The calculation process: runs a method in a process of its own, so an endless loop or a crash can be stopped
without taking the backend down.

The backend starts it ahead of time (pandas, SciPy and matplotlib load once) and sends it one job per line on stdin
(JSON); it answers with JSON lines on stdout: {"ready": true} once, then {"progress": ...} while a job runs and
{"done": {...}} when it ends. Whatever the method prints goes to stderr (the plugin's log), never into the answers.

A job: {"job": "run", "source": path of the method file (the run's own copy), "run_dir": ..., "recipe": name,
"kinds": {function: kind}, "args": {parameter: value}, "cache_dir": ..., "limit": calls kept per function,
"step_cache": folder of remembered steps or null (none), "fresh": true to run every step (still remembering them),
"inside": false to skip recording the values inside the functions, "values_budget": bytes of tables per run,
"all_budget": light values of every round of the loops kept per run (lab/recorder.py, ALL_BUDGET),
"paths": folders of the method's own packages (lab/packages.py)}. A returned text (long or of several lines, or a
report object such as a statsmodels summary) is kept as outputs/<name>.txt.
The run folder gets outputs/<name>.csv|png, steps/<n>_<step>.csv (each step's table), calls.json (every call),
inside.json (the values inside the functions, by call id) and values/ (every table once; see lab/values.py).

A remembered step's files (its tables in steps/, a figure it made) are kept in its folder of the step cache too, and
a run that takes the step from the cache gets them as hard links instead of writing them again. An output that is a
step's table is a hard link to that step's file. A figure a step returns is drawn once, into values/ (lab/values.py),
and a returned figure's outputs/<name>.png is a hard link to that picture. The links only while nothing but checked
steps can have changed a value (lab/recorder.py, StepCache.trusted); otherwise every file is written from the value.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROTO = sys.stdout  # the answers; everything else that prints goes to stderr
sys.stdout = sys.stderr


def send(msg: dict[str, Any]) -> None:
    PROTO.buffer.write((dumps(msg) + "\n").encode("utf-8"))
    PROTO.buffer.flush()


def dumps(v: Any) -> str:
    """JSON text; `clean` (slow on big records) only when the plain way fails (NaN, NumPy numbers, odd keys)."""
    try:
        return json.dumps(v, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps(clean(v), ensure_ascii=False)


def link(src: Path, dest: Path) -> bool:
    """Make `dest` the same file as `src` (a hard link; a copy where the disk can't link). False when src is gone."""
    try:
        os.link(src, dest)
        return True
    except FileExistsError:
        return False
    except OSError:
        try:
            shutil.copyfile(src, dest)
            return True
        except OSError:
            return False


def fresh(dest: Path) -> Path:
    """`dest`, with a file already there removed first: it may be a hard link to a remembered step's file, which
    writing into it would change for every run sharing it."""
    try:
        dest.unlink()
    except FileNotFoundError:
        pass
    return dest


def clean(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if v is None or isinstance(v, (str, int, bool)):
        return v
    try:
        import numpy as np

        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return clean(float(v))
        if isinstance(v, np.bool_):
            return bool(v)
    except ImportError:
        pass
    return str(v)


def versions() -> dict[str, str]:
    import matplotlib
    import numpy
    import pandas
    import scipy

    out = {"python": platform.python_version(), "pandas": pandas.__version__, "numpy": numpy.__version__,
           "scipy": scipy.__version__, "matplotlib": matplotlib.__version__}
    from importlib import metadata

    for name in ("statsmodels", "scikit-learn", "pyreadstat"):  # read without importing them (they load slowly)
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return out


# ---------------------------------------------------------------------------------------------------- errors


def _frames(tb, filename: str) -> list[traceback.FrameSummary]:
    return [f for f in traceback.extract_tb(tb) if os.path.normcase(f.filename) == os.path.normcase(filename)]


def _tables_in(frame_locals: dict, line: str) -> list[tuple[str, Any]]:
    import pandas as pd

    out = []
    for name, value in frame_locals.items():
        if isinstance(value, pd.DataFrame) and name in line:
            out.append((name, value))
    return out


def explain_error(exc: BaseException, filename: str) -> dict[str, Any]:
    """The error in plain words, at the method's own line: which function, which line, what is wrong."""
    tb = exc.__traceback__
    frames = _frames(tb, filename)
    where = frames[-1] if frames else None
    frame_locals: dict = {}
    t = tb
    while t is not None:
        if os.path.normcase(t.tb_frame.f_code.co_filename) == os.path.normcase(filename):
            frame_locals = dict(t.tb_frame.f_locals)
        t = t.tb_next
    line = (where.line or "") if where else ""
    detail = f"{type(exc).__name__}: {exc}"
    message = str(exc) or type(exc).__name__
    name = type(exc).__name__
    if name == "KeyError" and exc.args:
        key = exc.args[0]
        tables = _tables_in(frame_locals, line)
        missing_in = [(n, t) for n, t in tables if key not in t.columns] or tables
        if missing_in:
            n, table = missing_in[0]
            message = (f"The table `{n}` has no column {key!r}. Its columns are: "
                       f"{', '.join(map(str, table.columns))}.")
        else:
            dicts = [(n, v) for n, v in frame_locals.items() if isinstance(v, dict) and n in line and key not in v]
            if dicts:
                n, d = dicts[0]
                keys = ", ".join(map(repr, list(d)[:20]))
                message = f"`{n}` has no entry {key!r}. It has: {keys or 'nothing'}."
            else:
                message = f"Nothing is called {key!r} here (a column or key that doesn't exist)."
    elif name == "AttributeError" and "'DataFrame' object has no attribute" in str(exc):
        attr = str(exc).split("attribute")[-1].strip().strip("'\"")
        tables = _tables_in(frame_locals, line)
        if tables:
            n, table = tables[0]
            message = (f"The table `{n}` has no column {attr!r}. Its columns are: "
                       f"{', '.join(map(str, table.columns))}.")
        else:
            message = f"A table has no column {attr!r}."
    elif name == "UnboundLocalError":
        var = str(exc).split("'")[1] if "'" in str(exc) else "a name"
        message = (f"`{var}` is used before it gets a value here. If `{var}` is also a function's name, the result "
                   f"kept under the same name hides the function: give the result another name.")
    elif name == "NameError":
        var = str(exc).split("'")[1] if "'" in str(exc) else "a name"
        message = f"`{var}` is not defined here: is it spelled right, and defined or imported above?"
    elif name == "ZeroDivisionError":
        message = "A division by zero."
    elif name == "TypeError" and "required positional argument" in str(exc):
        message = f"A function was called without all its inputs: {exc}."
    elif name == "TypeError" and "unexpected keyword argument" in str(exc):
        message = f"A function was given an input it doesn't take: {exc}."
    elif name not in ("ValueError", "RuntimeError"):
        message = detail
    return {"message": message, "type": name, "detail": detail, "line": where.lineno if where else None,
            "function": where.name if where else None, "code": line.strip(),
            "trace": [{"function": f.name, "line": f.lineno, "code": (f.line or "").strip()} for f in frames]}


# ---------------------------------------------------------------------------------------------------- running


def text_of(value: Any) -> str | None:
    """A result kept as text: a long or multi-line text, or a report object that prints itself as one (a
    statsmodels summary, a fitted model's results). None: kept as a small value."""
    if isinstance(value, str):
        return value if len(value) > 200 or "\n" in value else None
    if hasattr(value, "as_text") and callable(value.as_text):
        try:
            return str(value.as_text())
        except Exception:
            return None
    module = type(value).__module__ or ""
    if module.startswith("statsmodels") and callable(getattr(value, "summary", None)):
        try:
            return str(value.summary())
        except Exception:
            return None
    return None



def run_job(job: dict[str, Any]) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    import toolbox
    import toolbox.cache  # the toolbox's own package no longer loads it
    from lab.recorder import WARM, Inside, Recorder, StepCache, load, plain_recipe, summarize
    from lab.values import BUDGET, Later, ValueStore, link_or_copy

    t0 = time.perf_counter()
    added = [p for p in job.get("paths") or [] if p not in sys.path]
    if added:  # the method's own packages (lab/packages.py): after the plugin's, so they never replace them
        sys.path.extend(added)
        import importlib

        importlib.invalidate_caches()
    run_dir = Path(job["run_dir"])
    (run_dir / "steps").mkdir(parents=True, exist_ok=True)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    source_path = Path(job["source"])
    source = source_path.read_text("utf-8")
    toolbox.cache.use(job.get("cache_dir"))
    saved: dict[int, str] = {}
    keep_alive: list[Any] = []
    counter = {"n": 0}
    figures: dict[int, tuple[Any, str]] = {}  # id → (a figure a step returned, its picture in values/)
    later = Later()  # files written while the run goes on (lab/values.py); waited for before the reply

    def save_table(df: pd.DataFrame, rel: str, role: str, cached: bool) -> str:
        origin = recorder.step_origin if cache is not None and cache.trusted else None
        kept = origin / "files" / f"{role}.csv" if origin is not None else None
        if kept is None or not cached or not link(kept, run_dir / rel):
            df.to_csv(fresh(run_dir / rel), index=False)
            if kept is not None:
                later(keep_file, run_dir / rel, kept)
        saved[id(df)] = rel
        keep_alive.append(df)
        return rel

    def keep_file(src: Path, dest: Path) -> None:
        try:
            link_or_copy(src, dest)
        except OSError as e:  # only a speed-up for the next run
            print(f"couldn't keep {src.name} with the remembered step: {e}", file=sys.stderr)

    def on_step_start(name: str) -> None:
        values.figs.clear()  # the step may change a figure drawn before: draw it again when it is shown
        send({"progress": f"{name}…", "step": name})

    def on_step(entry: dict, result: Any, raw: dict | None) -> None:
        counter["n"] += 1
        n = counter["n"]
        name = entry["function"]
        cached = bool(entry.get("cached"))
        for k, v in (raw or {}).items():
            if isinstance(v, pd.DataFrame) and isinstance(entry["inputs"].get(k), dict):
                rel = saved.get(id(v)) or save_table(v, f"steps/{n:02d}_{name}_in_{k}.csv", f"in_{k}", cached)
                entry["inputs"][k]["file"] = rel
        if isinstance(result, pd.DataFrame):
            entry["result"]["file"] = save_table(result, f"steps/{n:02d}_{name}.csv", "result", cached)
        elif isinstance(result, tuple) and isinstance(entry["result"], dict) and "items" in entry["result"]:
            for i, part in enumerate(result):
                if isinstance(part, pd.DataFrame) and i < len(entry["result"]["items"]):
                    entry["result"]["items"][i]["file"] = save_table(part, f"steps/{n:02d}_{name}_{i + 1}.csv",
                                                                     f"result_{i + 1}", cached)
        elif type(result).__name__ == "Figure" and hasattr(result, "savefig"):
            # its picture, kept once in values/ (a remembered step's picture is taken over, not drawn again)
            origin = recorder.step_origin if cache is not None and cache.trusted else None
            kept = origin / "files" / "figure.png" if origin is not None else None
            got = values.figure(result, kept if cached else None)
            if got.get("file"):
                if isinstance(entry.get("result"), dict):
                    entry["result"]["file"] = got["file"]
                figures[id(result)] = (result, got["file"])
                if kept is not None and not cached:
                    later(keep_file, run_dir / got["file"], kept)
        entry["order"] = n

    values = ValueStore(run_dir, int(job.get("values_budget") or BUDGET), later)
    from lab.recorder import ALL_BUDGET, ALL_ROUNDS, INSIDE_LIMIT, ROUNDS

    all_budget = int(job.get("all_budget") or ALL_BUDGET)
    inside = Inside(source, str(source_path), values, all_budget=all_budget) if job.get("inside", True) else None
    cache = None
    if job.get("step_cache"):
        try:
            mode = f"inside={inside is not None} limit={int(job.get('limit') or 1000)} {ROUNDS} {INSIDE_LIMIT} " \
                   f"{ALL_ROUNDS} {all_budget}"
            cache = StepCache(job["step_cache"], source, read=not job.get("fresh"), mode=mode, later=later)
            if plain_recipe(source, job["recipe"]):
                cache.remember_fingerprints()
        except SyntaxError:
            cache = None
    if cache is None or not cache.trusted:
        WARM.forget()  # this run may change values nobody checks: nothing kept from earlier runs is safe after it
    elif inside is not None:
        inside.known = cache
    recorder = Recorder(limit=int(job.get("limit") or 1000), on_step=on_step, inside=inside, cache=cache)
    recorder.on_step_start = on_step_start
    reply: dict[str, Any] = {"versions": versions()}
    module_name = "method"
    try:
        module = load(source, source_path, recorder, job.get("kinds") or {}, job["recipe"], module_name)
        sys.modules[module_name] = module  # so results made of the method's own classes can be remembered
        recipe = getattr(module, job["recipe"], None)
        if recipe is None:
            raise RuntimeError(f"The method has no function `{job['recipe']}` to run.")
        if inside is not None and not inside.start():
            inside = recorder.inside = None
        try:
            result = recipe(**(job.get("args") or {}))
        finally:
            if inside is not None:
                inside.stop()
        if not isinstance(result, dict):
            raise RuntimeError(f"The recipe `{job['recipe']}` must return a dict of its results, like "
                               '{"activities": activities}; it returned ' + type(result).__name__ + ".")
        outputs: dict[str, Any] = {}
        trusted = cache is not None and cache.trusted
        taken: set[str] = set()  # file names given (as the disk compares them: Rates and rates are one file)
        for key, value in result.items():
            key = str(key)
            safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in key)[:60] or "output"
            base, k = safe, 1
            while safe.lower() in taken:
                k += 1
                safe = f"{base}_{k}"
            taken.add(safe.lower())
            if isinstance(value, pd.DataFrame):
                rel = f"outputs/{safe}.csv"
                step_file = saved.get(id(value)) if trusted else None
                if step_file is None or not link(run_dir / step_file, run_dir / rel):
                    value.to_csv(fresh(run_dir / rel), index=False)
                outputs[key] = {"kind": "table", "file": rel, "rows": int(len(value)),
                                "columns": [str(c) for c in value.columns]}
            elif type(value).__name__ == "Figure" and hasattr(value, "savefig"):
                dest = run_dir / "outputs" / f"{safe}.png"
                fig = figures.get(id(value)) if trusted else None  # only steps changed it: its picture is right
                if fig is None or fig[0] is not value or not link(run_dir / fig[1], dest):
                    value.savefig(fresh(dest), format="png")
                outputs[key] = {"kind": "figure", "file": f"outputs/{safe}.png"}
            elif isinstance(value, (bytes, bytearray)) and bytes(value[:8]) == b"\x89PNG\r\n\x1a\n":
                fresh(run_dir / "outputs" / f"{safe}.png").write_bytes(bytes(value))
                outputs[key] = {"kind": "figure", "file": f"outputs/{safe}.png"}
            elif (text := text_of(value)) is not None:
                fresh(run_dir / "outputs" / f"{safe}.txt").write_text(text, "utf-8")
                outputs[key] = {"kind": "text", "file": f"outputs/{safe}.txt", "chars": len(text),
                                "preview": text[:300]}
            else:
                outputs[key] = {"kind": "value", "value": summarize(value)}
        reply.update(status="done", outputs=outputs)
    except Exception as e:  # her code failed: say where, in plain words
        reply.update(status="failed", error=explain_error(e, str(source_path)))
    finally:
        recorder.restore()
        sys.modules.pop(module_name, None)
        plt.close("all")
        try:
            toolbox.cache.save()
        except Exception as e:  # the cache is a speed-up only
            print(f"couldn't save the cache: {e}", file=sys.stderr)
    report = recorder.report()
    steps = []
    for c in report["calls"]:
        if c.get("step"):
            step = {"call": c["id"], "function": c["function"], "kind": c["kind"], "ms": c.get("ms"),
                    "status": "failed" if c.get("error") else "cached" if c.get("cached") else "done",
                    "error": c.get("error"), "inputs": c.get("inputs"), "output": c.get("result")}
            if c.get("cached"):
                step["ms_fresh"] = c.get("ms_fresh")
            if c.get("cache"):
                step["cache"] = c["cache"]
            steps.append(step)
    (run_dir / "calls.json").write_text(dumps(report), "utf-8")
    if inside is not None:
        try:
            values.save_index()
            record = inside.report(report["calls"])
            (run_dir / "inside.json").write_text(dumps(record), "utf-8")
        except Exception as e:  # the values inside are a help for reading, never a reason to fail
            print(f"couldn't write the values inside: {e}", file=sys.stderr)
    later.wait()
    if cache is not None:
        cache.wait()
        cache.finish()
        reply["cache"] = {"hits": cache.hits, "misses": cache.misses, "stored": cache.stored, "fresh":
                          bool(job.get("fresh"))}
        if cache.stored:
            try:
                cache.prune()
            except Exception as e:
                print(f"couldn't tidy the remembered steps: {e}", file=sys.stderr)
    reply["steps"] = steps
    reply["ms"] = round((time.perf_counter() - t0) * 1000)
    return reply


def warm_up_figures() -> None:
    """Draw one small figure now: the first drawing in a process loads the fonts and the formula parser (most of a
    second), which a run's figure would otherwise wait for."""
    import io

    import matplotlib.pyplot as plt

    try:
        fig, ax = plt.subplots(figsize=(2, 1.5))
        ax.plot([0, 1], [0, 1], marker="o", label="a")
        ax.set_xlabel("time (min) µ²")
        ax.set_title("$x^2$")
        ax.legend()
        fig.savefig(io.BytesIO(), format="png")
        plt.close(fig)
    except Exception as e:  # only a speed-up
        print(f"couldn't warm up the figures: {e}", file=sys.stderr)


def main() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot  # noqa: F401  (load everything now, not during the first run)
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import scipy.signal  # noqa: F401
    import scipy.stats  # noqa: F401

    import toolbox  # noqa: F401

    warm_up_figures()
    send({"ready": True, "pid": os.getpid()})
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return
        try:
            job = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if job.get("job") == "run":
            send({"done": run_job(job)})
        elif job.get("job") == "ping":
            send({"done": {"pong": True}})


if __name__ == "__main__":
    main()

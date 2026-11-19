"""The calculation process: runs a method in a process of its own, so an endless loop or a crash can be stopped
without taking the backend down.

The backend starts it ahead of time (pandas, SciPy and matplotlib load once) and sends it one job per line on stdin
(JSON); it answers with JSON lines on stdout: {"ready": true} once, then {"progress": ...} while a job runs and
{"done": {...}} when it ends. Whatever the method prints goes to stderr (the plugin's log), never into the answers.

A job: {"job": "run", "source": path of the method file (the run's own copy), "run_dir": ..., "recipe": name,
"kinds": {function: kind}, "args": {parameter: value}, "cache_dir": ..., "limit": calls kept per function}.
The run folder gets outputs/<name>.csv|png, steps/<n>_<step>.csv (each step's table) and calls.json (the record).
"""

from __future__ import annotations

import json
import math
import os
import platform
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROTO = sys.stdout  # the answers; everything else that prints goes to stderr
sys.stdout = sys.stderr


def send(msg: dict[str, Any]) -> None:
    PROTO.buffer.write((json.dumps(clean(msg), ensure_ascii=False) + "\n").encode("utf-8"))
    PROTO.buffer.flush()


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

    return {"python": platform.python_version(), "pandas": pandas.__version__, "numpy": numpy.__version__,
            "scipy": scipy.__version__, "matplotlib": matplotlib.__version__}


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


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    import toolbox
    from lab.recorder import Recorder, load, summarize

    t0 = time.perf_counter()
    run_dir = Path(job["run_dir"])
    (run_dir / "steps").mkdir(parents=True, exist_ok=True)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    source_path = Path(job["source"])
    source = source_path.read_text("utf-8")
    toolbox.cache.use(job.get("cache_dir"))
    saved: dict[int, str] = {}
    keep_alive: list[Any] = []
    counter = {"n": 0}

    def save_table(df: pd.DataFrame, rel: str) -> str:
        df.to_csv(run_dir / rel, index=False)
        saved[id(df)] = rel
        keep_alive.append(df)
        return rel

    def on_step_start(name: str) -> None:
        send({"progress": f"{name}…", "step": name})

    def on_step(entry: dict, result: Any, raw: dict | None) -> None:
        counter["n"] += 1
        n = counter["n"]
        name = entry["function"]
        for k, v in (raw or {}).items():
            if isinstance(v, pd.DataFrame) and isinstance(entry["inputs"].get(k), dict):
                rel = saved.get(id(v)) or save_table(v, f"steps/{n:02d}_{name}_in_{k}.csv")
                entry["inputs"][k]["file"] = rel
        if isinstance(result, pd.DataFrame):
            entry["result"]["file"] = save_table(result, f"steps/{n:02d}_{name}.csv")
        elif isinstance(result, tuple) and isinstance(entry["result"], dict) and "items" in entry["result"]:
            for i, part in enumerate(result):
                if isinstance(part, pd.DataFrame) and i < len(entry["result"]["items"]):
                    entry["result"]["items"][i]["file"] = save_table(part, f"steps/{n:02d}_{name}_{i + 1}.csv")
        entry["order"] = n

    recorder = Recorder(limit=int(job.get("limit") or 1000), on_step=on_step)
    recorder.on_step_start = on_step_start
    reply: dict[str, Any] = {"versions": versions()}
    try:
        module = load(source, source_path, recorder, job.get("kinds") or {}, job["recipe"])
        recipe = getattr(module, job["recipe"], None)
        if recipe is None:
            raise RuntimeError(f"The method has no function `{job['recipe']}` to run.")
        result = recipe(**(job.get("args") or {}))
        if not isinstance(result, dict):
            raise RuntimeError(f"The recipe `{job['recipe']}` must return a dict of its results, like "
                               '{"activities": activities}; it returned ' + type(result).__name__ + ".")
        outputs: dict[str, Any] = {}
        for key, value in result.items():
            key = str(key)
            safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in key)[:60] or "output"
            if isinstance(value, pd.DataFrame):
                value.to_csv(run_dir / "outputs" / f"{safe}.csv", index=False)
                outputs[key] = {"kind": "table", "file": f"outputs/{safe}.csv", "rows": int(len(value)),
                                "columns": [str(c) for c in value.columns]}
            elif type(value).__name__ == "Figure" and hasattr(value, "savefig"):
                value.savefig(run_dir / "outputs" / f"{safe}.png", format="png")
                outputs[key] = {"kind": "figure", "file": f"outputs/{safe}.png"}
            elif isinstance(value, (bytes, bytearray)) and bytes(value[:8]) == b"\x89PNG\r\n\x1a\n":
                (run_dir / "outputs" / f"{safe}.png").write_bytes(bytes(value))
                outputs[key] = {"kind": "figure", "file": f"outputs/{safe}.png"}
            else:
                outputs[key] = {"kind": "value", "value": summarize(value)}
        reply.update(status="done", outputs=outputs)
    except Exception as e:  # her code failed: say where, in plain words
        reply.update(status="failed", error=explain_error(e, str(source_path)))
    finally:
        plt.close("all")
        try:
            toolbox.cache.save()
        except Exception as e:  # the cache is a speed-up only
            print(f"couldn't save the cache: {e}", file=sys.stderr)
    report = recorder.report()
    steps = []
    for c in report["calls"]:
        if c.get("step"):
            steps.append({"call": c["id"], "function": c["function"], "kind": c["kind"], "ms": c.get("ms"),
                          "status": "failed" if c.get("error") else "done", "error": c.get("error"),
                          "inputs": c.get("inputs"), "output": c.get("result")})
    (run_dir / "calls.json").write_text(json.dumps(clean(report), ensure_ascii=False), "utf-8")
    reply["steps"] = steps
    reply["ms"] = round((time.perf_counter() - t0) * 1000)
    return reply


def main() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot  # noqa: F401  (load everything now, not during the first run)
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import scipy.signal  # noqa: F401
    import scipy.stats  # noqa: F401

    import toolbox  # noqa: F401

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

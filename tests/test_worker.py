"""The calculation process: a method that never ends is stopped (time limit, or Stop), a crash is reported, and the
next run works in a fresh process. Errors name the method's own line. Run from plugins/research:
uv run pytest tests -q
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab.runner import Pool, Runner  # noqa: E402
from lab.store import Store, new_id, now  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "sample" / "plant_growth.csv"

METHOD = '''"""Test method"""

import os

from toolbox import read_table


def stuck(table, mode):
    """Never ends, or crashes, or works."""
    if mode == "loop":
        while True:
            pass
    if mode == "crash":
        os._exit(3)
    if mode == "error":
        return table["no_such_column"]
    return table


def recipe(table_file, mode="ok"):
    """Run it."""
    table = read_table(table_file)
    result = stuck(table, mode)
    return {"result": result}
'''


@pytest.fixture()
def lab(tmp_path: Path):
    store = Store(tmp_path / "lab")
    f = store.add_file(SAMPLE)
    exp = {"id": new_id("e"), "title": "t", "method": "test", "inputs": {"table_file": f}, "params": {},
           "corrections": [], "created": now()}
    store.save_experiment(exp)
    runner = Runner(store, Pool())
    method = {"id": "test", "version": 1, "name": "Test", "source": METHOD}
    yield store, runner, exp, method
    runner.pool.close()


def with_mode(exp: dict, mode: str) -> dict:
    return {**exp, "params": {"mode": mode}}


def test_time_limit_stops_a_loop_that_never_ends(lab):
    store, runner, exp, method = lab
    t0 = time.monotonic()
    run = runner.run(with_mode(exp, "loop"), method, time_limit_s=3)
    assert run["status"] == "stopped" and "time limit (3 s)" in run["error"]["message"]
    assert time.monotonic() - t0 < 30
    ok = runner.run(exp, method)  # a fresh process takes the next run
    assert ok["status"] == "done", ok.get("error")
    assert store.get_experiment(exp["id"])["last_run"]["id"] == ok["id"]


def test_stop_ends_a_running_calculation(lab):
    store, runner, exp, method = lab
    runner.pool.warm()
    timer = threading.Timer(2.0, lambda: runner.pool.stop("you"))
    timer.start()
    run = runner.run(with_mode(exp, "loop"), method, time_limit_s=120)
    timer.cancel()
    assert run["status"] == "stopped" and run["error"]["message"] == "Stopped by you."
    assert runner.run(exp, method)["status"] == "done"


def test_a_crash_is_reported_and_the_next_run_works(lab):
    store, runner, exp, method = lab
    run = runner.run(with_mode(exp, "crash"), method)
    assert run["status"] == "failed" and "ended unexpectedly" in run["error"]["message"]
    assert runner.run(exp, method)["status"] == "done"


def test_an_error_names_the_line_in_plain_words(lab):
    store, runner, exp, method = lab
    run = runner.run(with_mode(exp, "error"), method)
    err = run["error"]
    assert run["status"] == "failed" and err["function"] == "stuck" and err["line"] == 16
    assert err["code"] == 'return table["no_such_column"]'
    assert "The table `table` has no column 'no_such_column'" in err["message"] and "height_cm" in err["message"]
    assert [s["status"] for s in run["steps"]] == ["done", "failed"]  # read_table, then stuck


def test_warm_process_keeps_reruns_fast(lab):
    store, runner, exp, method = lab
    runner.run(exp, method)
    t0 = time.monotonic()
    again = runner.run(exp, method)
    assert again["status"] == "done" and time.monotonic() - t0 < 5
    assert (store.run_dir(again["id"]) / "method.py").read_text("utf-8") == METHOD
    assert (store.run_dir(again["id"]) / "outputs" / "result.csv").exists()

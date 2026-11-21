"""Recording inside the method's functions, values stored once, remembered steps and trial runs.

The values every statement gave its names (loops capped, failed rounds kept), tables stored once by content and read
back (also from runs made before 0.4), the tools value_get and function_inside, and above all: a run made from
remembered steps gives exactly the outputs of a fresh run, also after a setting, a formula, a step or a correction
changed. Run from plugins/research:  uv run pytest tests/test_record.py -q
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import inside_tools, values  # noqa: E402
from lab.errors import LabError  # noqa: E402
from lab.runner import Pool, Runner, prune_runs  # noqa: E402
from lab.sample import make_sample  # noqa: E402
from lab.store import Store, new_id, now  # noqa: E402
from lab.tools import SAMPLE_PARAMS  # noqa: E402

PLANTS = Path(__file__).resolve().parent.parent / "sample" / "plant_growth.csv"
LAST = float(PLANTS.read_text("utf-8").strip().splitlines()[-1].split(",")[3])  # the last plant's height

INSIDE = '''"""Inside test"""

import pandas as pd

from toolbox import add_column, fit_lines, read_table


def double(x):
    """Twice x."""
    return x * 2


def walk(table, factor):
    """Go through the rows."""
    if table.empty:
        raise ValueError("No rows.")
    rows = []
    total = 0
    for value in table.height_cm:
        total += double(value) * factor
        rows.append({"value": value,
                     "total": total})
    same = table
    tall = table[table.height_cm > table.height_cm.median()]
    marked = table.copy()
    marked["flag"] = 1
    return pd.DataFrame(rows)


def broken(table, mode):
    """Fails in the third round when asked."""
    names = {0: "a", 1: "b"} if mode == "fail" else {i: str(i) for i in range(len(table))}
    seen = []
    for i in range(len(table)):
        seen.append(names[i])
    return seen


def heavier(dry_mass_g):
    """Twice the dry mass."""
    return dry_mass_g * 2


def recipe(table_file, factor=1.5, mode="ok"):
    """Run it."""
    table = read_table(table_file)
    walked = walk(table, factor)
    kept = broken(table, mode)
    lines = fit_lines(table, "leaves", "height_cm", by="treatment")
    heavy = add_column(table, heavier)
    return {"walked": walked, "lines": lines, "heavy": heavy}
'''


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("lab"))
    runner = Runner(store, Pool())
    plants = {"id": new_id("e"), "title": "plants", "method": "inside", "inputs": {"table_file": store.add_file(PLANTS)},
              "params": {}, "corrections": [], "created": now()}
    store.save_experiment(plants)
    gc = {"id": new_id("e"), "title": "gc", "method": "gc-activity-assay", "inputs": {"gaml_file": make_sample(store)},
          "params": dict(SAMPLE_PARAMS), "corrections": [], "created": now()}
    store.save_experiment(gc)
    yield SimpleNamespace(store=store, runner=runner, plants=plants, gc=gc,
                          inside={"id": "inside", "version": 1, "name": "Inside", "source": INSIDE},
                          method=store.get_method("gc-activity-assay"))
    runner.pool.close()


def record(store, run) -> dict:
    return json.loads((store.run_dir(run["id"]) / "inside.json").read_text("utf-8"))


def calls_of(store, run) -> dict:
    return json.loads((store.run_dir(run["id"]) / "calls.json").read_text("utf-8"))


def inside_of(store, run, function: str, nth: int = 0) -> dict:
    calls = [c for c in record(store, run)["calls"].values() if c["function"] == function]
    return calls[nth]


def by_line(rec: dict) -> dict[tuple, dict]:
    return {tuple(s["names"]): s for s in rec["lines"]}


# ---------------------------------------------------------------------------------------------------- inside


def test_every_statement_keeps_the_values_it_gave(lab):
    run = lab.runner.run(lab.plants, lab.inside, fresh=True)
    assert run["status"] == "done", run.get("error")
    walk = inside_of(lab.store, run, "walk")
    lines = by_line(walk)
    n = 48
    loop = lines[("value",)]
    assert loop["loop"] and loop["n"] == n
    assert [r["round"] for r in loop["rounds"]] == [1, 2, 3, n - 2, n - 1, n]
    assert loop["rounds"][0]["values"]["value"] == 15.0 and loop["rounds"][-1]["values"]["value"] == LAST
    total = lines[("total",)]
    assert total["n"] == n and total["rounds"][0]["values"]["total"] == 15.0 * 2 * 1.5
    assert total["rounds"][1]["values"]["total"] == pytest.approx(45.0 + 14.0 * 3)
    appended = lines[("rows",)]  # written over two lines: one statement
    assert appended["end_line"] == appended["line"] + 1 and appended["n"] == n
    last = appended["rounds"][-1]["values"]["rows"]
    assert last["length"] == n and last["last"]["items"]["value"] == LAST
    assert walk["returned"]["kind"] == "table" and walk["returned"]["rows"] == n
    assert walk["args"]["factor"] == 1.5 and walk["args"]["table"]["rows"] == n
    doubles = [c for c in record(lab.store, run)["calls"].values() if c["function"] == "double"]
    assert len(doubles) == n and doubles[0]["returned"] == 30.0


def test_tables_are_stored_once_by_content(lab):
    run = lab.runner.run(lab.plants, lab.inside)
    walk = inside_of(lab.store, run, "walk")
    lines = by_line(walk)
    table = walk["args"]["table"]["file"]
    assert lines[("same",)]["rounds"][0]["values"]["same"]["file"] == table  # the same table: the same file
    tall = lines[("tall",)]["rounds"][0]["values"]["tall"]
    assert tall["file"] != table and tall["rows"] < 48  # a filtered view is a table of its own
    copied = lines[("marked",)]
    first, changed = [s for s in walk["lines"] if s["names"] == ["marked"]]
    assert first["rounds"][0]["values"]["marked"]["file"] == table  # a copy has the same content
    assert changed["rounds"][0]["values"]["marked"]["file"] not in (table, tall["file"])  # changed in place
    assert copied is changed
    got = values.read(lab.store, run["id"], tall["file"], offset=0, limit=5)
    assert got["total"] == tall["rows"] and len(got["rows"]) == 5 and got["stored"] == "full"
    assert got["columns"] == ["index"] + tall["columns"] and got["index_columns"] == ["index"]  # its row numbers
    assert isinstance(got["rows"][0]["plant"], str) and got["rows"][0]["index"] > 0
    files = list((lab.store.run_dir(run["id"]) / "values").glob("*.csv.gz"))
    assert len(files) == len({f.name for f in files}) and len(files) <= 12


def test_the_failed_round_is_kept(lab):
    run = lab.runner.run({**lab.plants, "params": {"mode": "fail"}}, lab.inside, fresh=True)
    assert run["status"] == "failed" and run["error"]["function"] == "broken"
    broken = inside_of(lab.store, run, "broken")
    seen = by_line(broken)[("seen",)]
    assert seen["n"] == 3 and [r["round"] for r in seen["rounds"]] == [1, 2, 3]
    assert seen["rounds"][-1]["failed"] and seen["rounds"][-1]["values"]["seen"]["length"] == 2
    assert broken["error"].startswith("KeyError")


def test_values_get_a_size_budget(lab, tmp_path):
    import pandas as pd

    store = values.ValueStore(tmp_path)
    big = pd.DataFrame({"a": range(1000), "b": [str(i) for i in range(1000)]})
    assert "stored" not in store.table(big)
    store.budget = store.bytes - 1  # the budget is used up
    second = store.table(big.assign(c=1))
    assert second["stored"] == "sampled" and store.report()["sampled"] == 1
    store.save_index()
    assert values.read(SimpleNamespace(run_dir=lambda r: tmp_path, get_run=lambda r: {}), "r00000000",
                       second["file"])["note"].startswith("Only the first 200 of 1000 rows")
    store.budget = store.bytes // 3  # twice the budget is used up
    third = store.table(big.assign(c=2))
    assert third["stored"] == "left_out" and "file" not in third


def test_toolbox_calls_inside_ready_made_steps_are_recorded(lab):
    for _ in range(2):  # the second run in the same calculation process too (the wrappers were put back)
        run = lab.runner.run(lab.plants, lab.inside, fresh=True)
        calls = calls_of(lab.store, run)["calls"]
        step = next(c for c in calls if c["function"] == "fit_lines")
        inner = [c for c in calls if c["function"] == "fit_line"]
        assert len(inner) == 3 and all(c["parent"] == step["id"] for c in inner)  # three treatments
        heavy = next(c for c in calls if c["function"] == "add_column")
        assert heavy["inputs"]["formula"] == {"kind": "function", "name": "heavier"}
        assert any(c["function"] == "heavier" and c["parent"] == heavy["id"] for c in calls)


# ---------------------------------------------------------------------------------------------------- tools


def tools_on(store):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")

    def pick_run(run, experiment):
        if run:
            return store.get_run(run)
        return store.get_run(store.get_experiment(experiment)["last_run"]["id"])

    inside_tools.register(mcp, store, SimpleNamespace(pick_run=pick_run))
    return mcp


def call(mcp, tool: str, args: dict):
    result = asyncio.run(mcp.call_tool(tool, args))
    content = result[1] if isinstance(result, tuple) else result
    if isinstance(content, dict):
        return content.get("result", content)
    return json.loads(content[0].text)


def test_function_inside_and_value_get_tools(lab):
    run = lab.runner.run(lab.plants, lab.inside)
    mcp = tools_on(lab.store)
    got = call(mcp, "function_inside", {"experiment": lab.plants["id"], "function": "walk"})
    assert got["run"] == run["id"] and got["kind"] == "step"
    assert got["docstring"]["line"] == got["line"] + 1 and got["body_line"] == got["line"] + 2
    assert got["guards"] == [{"line": got["line"] + 2, "end_line": got["line"] + 3, "condition": "table.empty",
                              "message": "No rows."}]
    st = {tuple(s["names"]): s for s in got["statements"]}
    assert st[("rows",)]["code"].strip().startswith("rows.append({") and st[("total",)]["n"] == 48
    assert got["returned"]["rows"] == 48 and got["count"] == 1 and got["call"]["args"]["factor"] == 1.5
    used = {u["function"]: u for u in got["uses"]}
    assert used["double"]["count"] == 48 and used["double"]["direct"]
    d = call(mcp, "function_inside", {"run": run["id"], "function": "double"})
    assert d["count"] == 48 and d["returned"] == 30.0 and len(d["calls"]) == 48
    last = call(mcp, "function_inside", {"run": run["id"], "function": "double", "call": d["calls"][-1]})
    assert last["call"]["args"]["x"] == LAST and last["returned"] == LAST * 2
    with pytest.raises(Exception, match="isn't a call of `double`"):
        asyncio.run(mcp.call_tool("function_inside", {"run": run["id"], "function": "double", "call": 0}))
    table = got["call"]["args"]["table"]["file"]
    rows = call(mcp, "value_get", {"run": run["id"], "file": table, "offset": 46, "limit": 10})
    assert rows["total"] == 48 and len(rows["rows"]) == 2 and rows["rows"][-1]["plant"] == "P48"
    step_csv = next(s for s in run["steps"] if s["function"] == "walk")["output"]["file"]
    assert call(mcp, "value_get", {"run": run["id"], "file": step_csv})["total"] == 48
    with pytest.raises(Exception, match="not a value"):
        asyncio.run(mcp.call_tool("value_get", {"run": run["id"], "file": "../../experiments/x.json"}))


def test_runs_made_before_0_4_still_open(lab):
    run = lab.runner.run(lab.plants, lab.inside)
    rdir = lab.store.run_dir(run["id"])
    (rdir / "inside.json").unlink()
    shutil.rmtree(rdir / "values")
    old = inside_tools.inside_view(lab.store, lab.store.get_run(run["id"]), "walk")
    assert old["statements"] == [] and "before Research 0.4" in old["note"] and old["guards"]
    assert old["count"] == 1 and old["uses"][0]["function"] == "double"
    out = run["outputs"]["walked"]["file"]
    assert values.read(lab.store, run["id"], out)["total"] == 48
    with pytest.raises(LabError, match="no longer keeps"):
        values.read(lab.store, run["id"], "values/0123456789abcdef01234567.csv.gz")


def test_a_damaged_pack_or_record_is_said_in_plain_words(lab):
    run = lab.runner.run(lab.plants, lab.inside)
    rdir = lab.store.run_dir(run["id"])
    index = json.loads((rdir / "values" / "index.json").read_text("utf-8"))
    name, facts = next((n, f) for n, f in index.items() if f.get("pack"))
    pack = rdir / facts["pack"]
    pack.unlink()  # a hard link to the remembered step's pack: replaced, not written into
    pack.write_bytes(b"\x1f\x8b not gzip at all" * 100)
    with pytest.raises(LabError, match="damaged"):
        values.read(lab.store, run["id"], name)
    (rdir / "inside.json").write_text("{cut sh", "utf-8")
    got = inside_tools.inside_view(lab.store, lab.store.get_run(run["id"]), "walk")
    assert got["statements"] == [] and got["count"] == 1 and got["note"]


# ---------------------------------------------------------------------------------------------------- remembered steps


def same_outputs(store, a: dict, b: dict) -> None:
    """Every output table and every step's table of two runs: byte for byte the same files."""
    assert set(a["outputs"]) == set(b["outputs"])
    for name, o in a["outputs"].items():
        if o["kind"] == "table":
            fa = (store.run_dir(a["id"]) / o["file"]).read_bytes()
            fb = (store.run_dir(b["id"]) / b["outputs"][name]["file"]).read_bytes()
            assert fa == fb, name
    sa = sorted(p.name for p in (store.run_dir(a["id"]) / "steps").iterdir())
    sb = sorted(p.name for p in (store.run_dir(b["id"]) / "steps").iterdir())
    assert sa == sb
    for n in sa:
        assert (store.run_dir(a["id"]) / "steps" / n).read_bytes() == \
            (store.run_dir(b["id"]) / "steps" / n).read_bytes(), n
    ca, cb = calls_of(store, a), calls_of(store, b)
    assert ca["counts"] == cb["counts"]
    assert [(c["id"], c["function"], c["parent"], c["n"]) for c in ca["calls"]] == \
        [(c["id"], c["function"], c["parent"], c["n"]) for c in cb["calls"]]
    ra, rb = record(store, a), record(store, b)
    assert sorted(ra["calls"]) == sorted(rb["calls"])
    for cid, r in ra["calls"].items():
        assert r["lines"] == rb["calls"][cid]["lines"], (cid, r["function"])


def statuses(run: dict) -> dict[str, str]:
    return {s["function"]: s["status"] for s in run["steps"]}


def test_a_remembered_run_equals_a_fresh_one(lab):
    fresh = lab.runner.run(lab.gc, lab.method, fresh=True)
    assert fresh["status"] == "done" and fresh["fresh"] and set(statuses(fresh).values()) == {"done"}
    again = lab.runner.run(lab.gc, lab.method)
    assert again["status"] == "done" and set(statuses(again).values()) == {"cached"}, statuses(again)
    assert again["cache"]["hits"] == 24 and again["ms"] < fresh["ms"]
    same_outputs(lab.store, fresh, again)


def changed_and_compared(lab, exp: dict, method: dict) -> dict:
    """A run reusing remembered steps after a change, compared with a fresh run of the same change."""
    cached = lab.runner.run(exp, method)
    fresh = lab.runner.run(exp, method, fresh=True)
    assert cached["status"] == fresh["status"] == "done", (cached.get("error"), fresh.get("error"))
    same_outputs(lab.store, fresh, cached)
    return statuses(cached)


def test_after_a_setting_changed(lab):
    lab.runner.run(lab.gc, lab.method)
    exp = {**lab.gc, "params": {**lab.gc["params"], "max_replicate_cv_pct": 5}}
    st = changed_and_compared(lab, exp, lab.method)
    assert st["replicate_checks"] == "done" and st["combine_flags"] == "done"
    assert st["initial_rates"] == "cached" and st["integrate_peaks"] == "cached"


def test_after_a_formula_changed(lab):
    source = lab.method["source"].replace("return rate_mM_per_min * 1000 / biomass_gCDW_per_L",
                                          "return rate_mM_per_min * 1000 / biomass_gCDW_per_L / 2")
    assert source != lab.method["source"]
    st = changed_and_compared(lab, lab.gc, {**lab.method, "source": source, "version": None})
    assert st["initial_rates"] == "done" and st["interval_rates"] == "done"
    assert st["quantify"] == "cached" and st["time_course"] == "cached" and st["mass_balance"] == "cached"


def test_after_a_step_changed(lab):
    same = lab.method["source"].replace('"sum_mM": group["mean"].sum()})', '"sum_mM": group["mean"].sum() + 0})')
    assert same != lab.method["source"]
    st = changed_and_compared(lab, lab.gc, {**lab.method, "source": same, "version": None})
    assert st["mass_balance"] == "done" and st["mass_balance_checks"] == "cached"  # its table came out the same
    source = lab.method["source"].replace('"sum_mM": group["mean"].sum()})', '"sum_mM": group["mean"].sum() * 2})')
    st = changed_and_compared(lab, lab.gc, {**lab.method, "source": source, "version": None})
    assert st["mass_balance"] == "done" and st["mass_balance_checks"] == "done"
    assert st["time_course"] == "cached" and st["interval_rates"] == "cached"


def test_after_a_block_was_added_in_the_flow(lab):
    """A block adds its name to the `from toolbox import …` line: no other step's key may change."""
    from lab import flow

    lab.runner.run(lab.gc, lab.method)
    source = flow.edit(lab.method["source"], {"op": "add_step", "function": "pick_rows", "out": "sub_rows",
                                              "args": {"table": "concentrations", "column": {"value": "compound"},
                                                       "equals": {"value": "SUB"}}})
    assert "pick_rows" in source.split("from toolbox import")[1].split(")")[0]
    method = {**lab.method, "source": source, "version": None}
    trial = lab.runner.run(lab.gc, method, trial=True)
    fresh = lab.runner.run(lab.gc, method, trial=True, fresh=True)
    assert trial["status"] == fresh["status"] == "done", (trial.get("error"), fresh.get("error"))
    st = statuses(trial)
    assert st.pop("pick_rows") == "done" and set(st.values()) == {"cached"}, st
    same_outputs(lab.store, fresh, trial)


def test_a_definition_a_step_uses_is_in_its_key(lab):
    """A module-level constant a step reads belongs in its key; an unrelated new constant doesn't."""
    lab.runner.run(lab.gc, lab.method)
    unrelated = lab.method["source"].replace("\nDESIGN_COLUMNS = ", "\nUNUSED_LIMIT = 3\nDESIGN_COLUMNS = ")
    st = statuses(lab.runner.run(lab.gc, {**lab.method, "source": unrelated, "version": None}, trial=True))
    assert set(st.values()) == {"cached"}, st
    used = lab.method["source"].replace('"std_conc_mM", "std_series", "collected"]',
                                        '"std_conc_mM", "std_series", "collected", "extra"]')
    assert used != lab.method["source"]
    st = statuses(lab.runner.run(lab.gc, {**lab.method, "source": used, "version": None}, trial=True))
    assert st["sample_names"] == "done" and st["read_gaml"] == "cached"


def test_a_formula_given_to_a_ready_made_step(lab):
    """add_column(table, heavier): remembered on the next run, and calculated again when the formula, or a function
    of the method the formula calls, changed."""
    source = INSIDE.replace("    return dry_mass_g * 2\n", "    return double(dry_mass_g)\n")
    method = {**lab.inside, "source": source}
    lab.runner.run(lab.plants, method)
    again = lab.runner.run(lab.plants, method, trial=True)
    assert statuses(again)["add_column"] == "cached"
    changed = {**lab.inside, "source": source.replace("    return x * 2\n", "    return x * 3\n")}
    st = changed_and_compared(lab, lab.plants, changed)
    assert st["add_column"] == "done" and st["read_table"] == "cached"


def test_after_a_correction(lab):
    exp = {**lab.gc, "corrections": [{"id": "c1", "kind": "exclude", "injection": 20, "compound": "SUB",
                                      "reason": "Test", "status": "approved"}]}
    st = changed_and_compared(lab, exp, lab.method)
    assert st["sample_names"] == "done" and st["integrate_peaks"] == "done" and st["read_gaml"] == "cached"


def test_a_step_that_changes_its_input_is_never_remembered(lab):
    source = INSIDE.replace('    marked = table.copy()\n', '    marked = table\n')  # walk now changes `table`
    method = {**lab.inside, "source": source}
    first = lab.runner.run(lab.plants, method, fresh=True)
    walk = next(s for s in first["steps"] if s["function"] == "walk")
    assert "changes a value it was given" in walk["cache"]
    again = lab.runner.run(lab.plants, method)
    assert statuses(again)["walk"] == "done" and statuses(again)["read_table"] == "cached"
    same_outputs(lab.store, first, again)


def test_a_remembered_run_takes_over_files_instead_of_writing_them(lab):
    first = lab.runner.run(lab.gc, lab.method)
    again = lab.runner.run(lab.gc, lab.method)
    assert set(statuses(again).values()) == {"cached"}
    a, b = lab.store.run_dir(first["id"]), lab.store.run_dir(again["id"])
    assert (b / "outputs" / "figure.png").read_bytes() == (a / "outputs" / "figure.png").read_bytes()
    assert (b / "outputs" / "figure.png").stat().st_nlink > 1  # the remembered step's figure, not drawn again
    step = next(s for s in again["steps"] if s["function"] == "quantify")["output"]["file"]
    assert (b / step).stat().st_nlink > 1
    assert (b / "outputs" / "concentrations.csv").read_bytes() == (b / step).read_bytes()
    packs = list((b / "values").glob("*.pack"))
    assert packs and not list((b / "values").glob("*.csv.gz"))  # tables in packs, not one file each
    index = json.loads((b / "values" / "index.json").read_text("utf-8"))
    name, facts = next((n, f) for n, f in index.items() if f.get("stored") == "full")
    got = values.read(lab.store, again["id"], name, limit=3)
    assert got["total"] == facts["rows"] and got["columns"] == facts["columns"][:len(got["columns"])]


def test_two_results_with_the_same_file_name_never_write_into_a_remembered_step(lab):
    """`x_y` and `x y` both become outputs/x_y.csv: the second must not be written into the first's file, which is the
    remembered step's own file (a hard link), or every later run would show the wrong table for that step."""
    import pandas as pd

    source = INSIDE.replace('return {"walked": walked, "lines": lines, "heavy": heavy}',
                            'return {"x_y": walked, "x y": heavy}')
    method = {**lab.inside, "source": source}
    first = lab.runner.run(lab.plants, method, fresh=True)
    again = lab.runner.run(lab.plants, method)
    assert first["status"] == again["status"] == "done", (first.get("error"), again.get("error"))
    assert statuses(again)["walk"] == "cached"
    for run in (first, again):
        rdir = lab.store.run_dir(run["id"])
        walk = next(s for s in run["steps"] if s["function"] == "walk")["output"]["file"]
        assert list(pd.read_csv(rdir / walk).columns) == ["value", "total"]
        files = {o["file"] for o in run["outputs"].values()}
        assert len(files) == 2  # each result its own file
        for name, o in run["outputs"].items():
            assert list(pd.read_csv(rdir / o["file"]).columns) == o["columns"], name


def test_remembered_steps_where_the_disk_cant_make_hard_links(tmp_path, monkeypatch):
    """On a disk without hard links every file is a copy: the remembered run still equals a fresh one, and no file
    is shared."""
    import importlib
    import os

    out = sys.stdout
    worker = importlib.import_module("lab.worker")
    sys.stdout = out  # the worker sends its answers on the real stdout; here it is called in this process

    class InProcess:
        def run(self, job, progress=None, time_limit_s=None):
            return worker.run_job(job)

        def close(self):
            pass

    def no_links(src, dst, *a, **k):
        raise OSError("this disk can't make hard links")

    monkeypatch.setattr(os, "link", no_links)
    store = Store(tmp_path / "lab")
    runner = Runner(store, InProcess())
    exp = {"id": new_id("e"), "title": "plants", "method": "inside", "inputs": {"table_file": store.add_file(PLANTS)},
           "params": {}, "corrections": [], "created": now()}
    store.save_experiment(exp)
    method = {"id": "inside", "version": 1, "name": "Inside", "source": INSIDE}
    fresh = runner.run(exp, method, fresh=True)
    again = runner.run(exp, method)
    assert fresh["status"] == again["status"] == "done", (fresh.get("error"), again.get("error"))
    assert statuses(again)["walk"] == "cached"
    same_outputs(store, fresh, again)
    files = [p for p in store.root.rglob("*") if p.is_file()]
    assert all(p.stat().st_nlink == 1 for p in files) and not [p for p in files if p.name.endswith(".tmp")]


FIGURES = '''"""Figures"""

import matplotlib.pyplot as plt

from toolbox import plot_bars, read_table, summarize


def heights_figure(means):
    """A bar chart of the mean heights."""
    fig, ax = plt.subplots()
    ax.bar(means.treatment, means["mean"])
    return fig


def recipe(table_file):
    """Figures."""
    table = read_table(table_file)
    means = summarize(table, "height_cm", by="treatment")
    chart = plot_bars(means, "treatment", "mean")
    own = heights_figure(means)
    return {"means": means, "figure": own}
'''


def test_figures_are_stored_once_and_shown_returned_or_not(lab):
    """A figure the recipe returns and one it only makes: each has a picture in values/ that value_get shows, in
    the flow's boxes too, fresh and from remembered steps."""
    import base64

    from lab import flow
    from lab.flow_tools import run_lines

    method = {"id": "figures", "version": 1, "name": "Figures", "source": FIGURES}
    for run in (lab.runner.run(lab.plants, method, fresh=True), lab.runner.run(lab.plants, method)):
        assert run["status"] == "done", run.get("error")
        steps = {s["function"]: s for s in run["steps"]}
        for fn in ("plot_bars", "heights_figure"):
            out = steps[fn]["output"]
            assert out["kind"] == "figure" and out["file"].startswith("values/") and out["file"].endswith(".png")
            got = values.read(lab.store, run["id"], out["file"])
            assert got["kind"] == "figure" and base64.b64decode(got["data"])[:4] == b"\x89PNG"
        full = lab.store.get_run(run["id"])
        source = (lab.store.run_dir(run["id"]) / "method.py").read_text("utf-8")
        g = flow.graph(source, {**full, "source": source, "lines": run_lines(lab.store, full, source)})
        nodes = {n["id"]: n for n in g["nodes"]}
        assert nodes["chart"]["values"]["chart"]["file"] == steps["plot_bars"]["output"]["file"]
        assert nodes["own"]["values"]["own"]["file"] == steps["heights_figure"]["output"]["file"]
        assert {o["id"]: o for o in g["outputs"]}["figure"]["value"]["file"] == nodes["own"]["values"]["own"]["file"]
        rdir = lab.store.run_dir(run["id"])
        assert (rdir / run["outputs"]["figure"]["file"]).read_bytes() ==             (rdir / steps["heights_figure"]["output"]["file"]).read_bytes()
    assert statuses(run)["plot_bars"] == "cached" and statuses(run)["heights_figure"] == "cached"
    inside = inside_of(lab.store, run, "heights_figure")
    assert inside["returned"]["file"] == steps["heights_figure"]["output"]["file"]


def test_the_results_database_after_remembered_runs(lab):
    exp = {**lab.gc, "id": new_id("e"), "title": "gc db"}
    lab.store.save_experiment(exp)

    def activities():
        df = lab.store.db_query("activities", limit=100000)
        return df[df.experiment == exp["id"]]

    x = lab.runner.run(exp, lab.method)
    assert set(activities().run) == {x["id"]}
    again = lab.runner.run(exp, lab.method)  # every table the same: only the run's id and time change
    rows = activities()
    assert set(rows.run) == {again["id"]} and len(rows) == len(lab.store.run_table(x["id"], "activities"))
    other = {**exp, "params": {**exp["params"], "biomass_gcdw_per_l": 0.3}}
    lab.store.save_experiment(other)
    y = lab.runner.run(other, lab.method)
    values_y = sorted(activities().activity_U_per_gCDW.dropna())
    lab.store.save_experiment(exp)
    back = lab.runner.run(exp, lab.method)  # remembered from the first run, but the database holds y's numbers
    assert set(statuses(back).values()) == {"cached"}
    rows = activities()
    assert set(rows.run) == {back["id"]} and sorted(rows.activity_U_per_gCDW.dropna()) != values_y
    assert sorted(rows.activity_U_per_gCDW.dropna()) == \
        sorted(lab.store.run_table(back["id"], "activities").activity_U_per_gCDW.dropna())
    assert y["id"] not in set(rows.run)


def test_a_function_called_many_times_keeps_its_first_calls_inside(lab):
    source = INSIDE.replace("    for value in table.height_cm:\n", "    for value in list(table.height_cm) * 2:\n")
    run = lab.runner.run(lab.plants, {**lab.inside, "source": source}, fresh=True)
    assert run["status"] == "done", run.get("error")
    rec = record(lab.store, run)
    doubles = [c for c in rec["calls"].values() if c["function"] == "double"]
    assert rec["calls_limit"] == 50 and len(doubles) == 50  # of 96
    calls = calls_of(lab.store, run)
    assert calls["counts"]["double"] == 96
    later = [c["id"] for c in calls["calls"] if c["function"] == "double"][60]
    got = inside_tools.inside_view(lab.store, lab.store.get_run(run["id"]), "double", later)
    assert got["statements"] == [] and "only the first 50 calls" in got["note"]
    assert by_line(inside_of(lab.store, run, "walk"))[("total",)]["n"] == 96  # the step itself: every round


def test_a_broken_cache_entry_is_a_miss(lab):
    lab.runner.run(lab.gc, lab.method)
    for p in (lab.store.root / "cache" / "steps").rglob("entry.pkl"):
        p.write_bytes(b"not a pickle")
    run = lab.runner.run(lab.gc, lab.method)
    assert run["status"] == "done" and set(statuses(run).values()) == {"done"}


# ---------------------------------------------------------------------------------------------------- trials


def test_a_trial_run_of_an_unsaved_source(lab):
    lab.runner.run(lab.plants, lab.inside)
    before = lab.store.get_experiment(lab.plants["id"]).get("last_run")
    source = INSIDE.replace("factor=1.5", "factor=3")
    trial = lab.runner.run(lab.plants, {"id": "inside", "source": source}, trial=True)
    assert trial["status"] == "done" and trial["trial"] and trial["method"]["version"] is None
    assert statuses(trial)["walk"] == "done" and statuses(trial)["read_table"] == "cached"
    assert lab.store.get_experiment(lab.plants["id"]).get("last_run") == before
    assert trial["id"] not in set(lab.store.db_query("walked")["run"])  # not in the results database
    assert inside_of(lab.store, trial, "walk")["args"]["factor"] == 3


def test_stopping_one_experiment_never_stops_another_ones_run():
    pool = Pool()
    killed = []
    pool.current = SimpleNamespace(kill=lambda: killed.append(1))
    pool.current_job = {"experiment": "e00000001"}  # a trial of another experiment calculates now
    assert pool.stop("you", "e00000002") is False and not killed
    assert pool.stop("you", "e00000001") is True and killed == [1]


def test_pruning_old_trials_and_insides(lab):
    exp = lab.plants
    trials = [lab.runner.run(exp, lab.inside, trial=True) for _ in range(4)]
    kept = lab.runner.run(exp, lab.inside)
    result = prune_runs(lab.store, trial_keep=2, inside_keep=1)
    assert result["deleted_trials"] >= 2
    left = {r["id"] for r in lab.store.list_runs(exp["id"], trials=True)}
    assert trials[-1]["id"] in left and trials[0]["id"] not in left
    assert (lab.store.run_dir(kept["id"]) / "inside.json").exists()  # the latest run keeps its values inside
    cleared = lab.store.get_run(trials[-1]["id"])
    assert cleared["inside"] == "cleared" and not (lab.store.run_dir(cleared["id"]) / "values").exists()
    assert (lab.store.run_dir(cleared["id"]) / "steps").exists() and cleared["outputs"]

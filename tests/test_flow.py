"""The flow: the recipe drawn as a graph (nodes, ports, types, columns read, groups, the quality lane, a run's values)
and every change made in the flow as an exact edit of the code, checked by parsing it back, the style checker and
the graph, and by undoing it to the same bytes. Run from plugins/research:  uv run pytest tests/test_flow.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import checker, flow, methodfile  # noqa: E402
from lab.errors import LabError  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
GC = (PLUGIN / "backend" / "lab" / "methods" / "gc-activity-assay.py").read_text("utf-8")
# the recipe's line that integrates the peaks (the lines above it change with the method's PAGES and CORRECTIONS)
P = next(i for i, ln in enumerate(GC.splitlines(), 1) if ln.strip().startswith("peaks = integrate_peaks("))
PLANTS_CSV = PLUGIN / "sample" / "plant_growth.csv"
PROTOTYPE = PLUGIN.parent.parent / "docs" / "proposals" / "research-flow" / "build_prototype.py"

SMALL = '''"""Plant heights

Mean height per treatment, and relative to the control.
"""

import pandas as pd

from toolbox import read_table


def relative(value, control_mean):
    """A value divided by the control's mean."""
    return value / control_mean


def mean_heights(table, min_height_cm):
    """The mean height of each treatment, from the plants at least this tall."""
    tall = table[table.height_cm >= min_height_cm]
    rows = []
    for treatment, group in tall.groupby("treatment"):
        rows.append({"treatment": treatment, "mean_cm": group.height_cm.mean(), "n": len(group)})
    return pd.DataFrame(rows)


def relative_heights(means, control):
    """Each treatment's mean height relative to the control's."""
    control_mean = means[means.treatment == control].mean_cm.iloc[0]
    result = means.copy()
    result["relative"] = relative(result.mean_cm, control_mean)
    return result


def plants(table_file, min_height_cm=0.0, control="control"):
    """Plant heights.

    Inputs:
        table_file: The plants (CSV) [.csv]

    Settings:
        min_height_cm: Shortest plant counted.
        control: Control group. The treatment the others are compared with.

    Returns:
        means: Mean heights
        relative: Relative heights
    """
    table = read_table(table_file)
    means = mean_heights(table, min_height_cm)
    relative_table = relative_heights(means, control)
    return {"means": means, "relative": relative_table}
'''

CODE = '''"""Code boxes

Shapes the flow can't draw as plain steps.
"""

from toolbox import fit_line, read_table
from toolbox.gc import PeakSettings


def scale(table, factor, *, column="height_cm"):
    """Multiply a column by a factor."""
    out = table.copy()
    out[column] = out[column] * factor
    return out


def both(table):
    """The table split in two halves."""
    half = len(table) // 2
    return table.iloc[:half], table.iloc[half:]


def recipe(table_file, factor=2.0, extra=()):
    """Code boxes.

    Inputs:
        table_file: A table [.csv]
    """
    # --- Load
    table = read_table(table_file)
    first, second = both(table)
    # --- Shape the data
    if factor > 1:
        table = scale(table, factor)
    scaled = scale(scale(first, 2), factor, column="leaves")
    more = scale(*extra)
    settings = PeakSettings(11, max_half_width_min=0.2)
    line = fit_line(second.leaves, second.height_cm)
    mystery = unknown_function(table)
    # ---
    n = len(scaled)
    return {"scaled": scaled, "line": line}
'''

QUALITY = ["design_flags", "standard_flags", "shape_flags", "instrument_flags", "calibration_flags",
           "concentration_flags", "replicate_flags", "balance_flags", "correction_flags", "flags"]


# ---------------------------------------------------------------------------------------------------- helpers


def messages(source: str) -> set[str]:
    return {m["message"] for m in checker.check(source)["messages"]}


def checked(new: str, before: str, new_warnings: bool = False) -> dict:
    """The edited source still reads, passes the checker without errors (and without new warnings), and draws."""
    methodfile.parse(new)
    result = checker.check(new)
    assert result["ok"], [m for m in result["messages"] if m["level"] == "error"]
    if not new_warnings:
        added = messages(new) - messages(before)
        assert not added, added
    return flow.graph(new)


def others_unchanged(before: str, after: str) -> None:
    """Every function but the recipe, the module docstring and the constants stay as they were."""
    b, a = methodfile.parse(before), methodfile.parse(after)
    assert [f.source for f in b.functions if f.kind != "recipe"] == \
        [f.source for f in a.functions if f.kind != "recipe" and b.function(f.name) is not None]
    assert b.name == a.name and b.description == a.description and b.constants == a.constants


def node(g: dict, node_id: str) -> dict:
    return next(n for n in g["nodes"] if n["id"] == node_id)


def port(n: dict, name: str) -> dict:
    return next(p for p in n["ports"] if p["name"] == name)


def roundtrip(source: str, op: dict, undo: dict, new_warnings: bool = False) -> tuple[str, dict]:
    """Apply a change, check it, undo it: the original bytes come back."""
    new = flow.edit(source, op)
    assert new != source
    g = checked(new, source, new_warnings)
    others_unchanged(source, new)
    assert flow.edit(new, undo) == source
    return new, g


# ---------------------------------------------------------------------------------------------------- runs


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """The GC method on the made-up sample, and the small method on the plant table (one calculation process)."""
    from lab import examples
    from lab.runner import Pool, Runner
    from lab.store import Store, new_id, now

    store = Store(tmp_path_factory.mktemp("flow") / "lab")
    runner = Runner(store, Pool())
    example = examples.get("gc")
    gc_exp = {"id": new_id("e"), "title": "Sample", "method": "gc-activity-assay",
              "inputs": examples.stored_files(store, example), "params": example.SETTINGS, "corrections": [],
              "created": now()}
    store.save_experiment(gc_exp)
    table = store.add_file(PLANTS_CSV)
    small_exp = {"id": new_id("e"), "title": "Plants", "method": "plants", "inputs": {"table_file": table},
                 "params": {"min_height_cm": 1.0}, "corrections": [], "created": now()}
    store.save_experiment(small_exp)
    try:
        def run(exp: dict, source: str) -> dict:
            r = runner.run(exp, {"id": exp["method"], "version": 1, "name": "t", "source": source})
            return {**r, "source": (store.run_dir(r["id"]) / "method.py").read_text("utf-8")}

        out = {"store": store, "runner": runner, "run": run, "gc_exp": gc_exp, "small_exp": small_exp,
               "gc": run(gc_exp, GC), "small": run(small_exp, SMALL)}
        yield out
    finally:
        runner.pool.close()


# ---------------------------------------------------------------------------------------------------- the graph


def test_gc_graph_nodes_ports_settings_results():
    g = flow.graph(GC)
    assert len(g["nodes"]) == 25 and all(n["kind"] == "call" for n in g["nodes"])
    assert [n["id"] for n in g["nodes"]][:4] == ["injections", "samples", "design_flags", "settings"]
    assert g["messages"] == [] and g["groups"] == [] and g["run"] is None and g["stale"] is False
    assert g["method"]["recipe"] == "activity_assay" and g["method"]["name"] == "GC-FID activity assay"
    inj = node(g, "injections")
    assert inj["fn"] == "read_gaml" and inj["fn_kind"] == "toolbox" and inj["opens_as"] == "code"
    assert port(inj, "gaml_file")["source"] == "input" and port(inj, "channel")["setting"] == "channel"
    assert node(g, "settings")["fn_kind"] == "class" and node(g, "samples")["fn_kind"] == "step"
    standards = node(g, "standards")
    assert standards["title"] == "Calibration standards"
    assert standards["sentence"].startswith("Mark the standards the calibration lines are fitted to")
    assert [(p["name"], p["source"]) for p in standards["ports"]] == [
        ("ratios", "wire"), ("cal_min_conc_mM", "setting"), ("cal_max_conc_mM", "setting"), ("cal_series", "setting")]
    assert port(standards, "ratios")["from"] == "ratios"
    peaks = node(g, "peaks")
    assert peaks["line"] == P and peaks["end_line"] == P + 1 and peaks["code"].startswith("peaks = integrate_peaks(")
    assert {p["setting"] for p in peaks["ports"] if p["source"] == "setting"} == {"internal_standard",
                                                                                   "rt_drift_correction"}
    assert port(peaks, "settings")["wire"] == "settings" and port(peaks, "corrections")["input"] == "corrections"
    flags = node(g, "flags")
    assert flags["fn"] == "combine_flags" and len(flags["ports"]) == 9 and flags["types"] == {"flags": "flags"}
    assert node(g, "activities")["result"] == {"id": "activities", "label": "Specific activities"}
    assert node(g, "course")["result"] == {"id": "time_course", "label": "Time course"}
    assert node(g, "ratios")["result"] is None
    assert [o["id"] for o in g["outputs"]] == ["activities", "time_course", "concentrations", "interval_rates",
                                               "mass_balance", "calibration", "calibration_points", "peaks", "flags",
                                               "figure"]
    assert next(o for o in g["outputs"] if o["id"] == "interval_rates")["wire"] == "intervals"
    settings = {s["id"]: s for s in g["settings"]}
    assert settings["shoulder_fwhm_ratio"]["used_by"] == ["standard_flags", "shape_flags"]
    assert settings["internal_standard"]["used_by"] == ["peaks", "ratios", "standard_flags", "shape_flags"]
    assert settings["min_r2"]["value"] == 0.995 and settings["min_r2"]["set"] is False
    assert {i["id"]: i["used_by"] for i in g["inputs"]} == {"gaml_file": ["injections"],
                                                            "corrections": ["samples", "design_flags", "peaks"]}
    assert {"from": "ratios", "to": "concentrations", "wire": "ratios", "port": "ratios"} in g["edges"]
    # types without a run: annotations and the code's own returns
    assert node(g, "injections")["types"] == {"injections": "data"}
    assert node(g, "samples")["types"] == {"samples": "table"}
    assert node(g, "design_flags")["types"] == {"design_flags": "flags"}
    assert node(g, "figure")["types"] == {"figure": "figure"}
    assert port(standards, "cal_min_conc_mM")["type"] == "number" and port(peaks, "samples")["type"] == "table"


def test_quality_lane_equals_the_prototype():
    g = flow.graph(GC)
    assert g["quality"] == QUALITY
    assert [n["id"] for n in g["nodes"] if n["quality"]] == QUALITY
    spec = importlib.util.spec_from_file_location("build_prototype", PROTOTYPE)
    proto = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proto)
    pg = proto.recipe_graph(methodfile.parse(GC))
    outs = {n["id"]: n["out"] for n in pg["nodes"]}
    assert [outs[i] for i in proto.quality_only(pg)] == QUALITY


def test_columns_read_from_the_code():
    g = flow.graph(GC)
    assert port(node(g, "ratios"), "peaks")["columns_read"] == ["compound", "status", "injection", "area"]
    # `used` is made by calibration_standards itself before it reads it, and read by fit_calibration
    assert "used" not in port(node(g, "standards"), "ratios")["columns_read"]
    assert port(node(g, "calibration"), "standards")["columns_read"] == ["compound", "used", "std_conc_mM", "ratio"]
    # through a helper (integrate_injection reads the references' columns) and the toolbox's own code
    assert port(node(g, "peaks"), "references")["columns_read"] == ["compound", "rt_ref", "rt_tolerance", "fwhm_ref"]
    assert port(node(g, "figure"), "time_course")["columns_read"][:3] == ["strain", "substrate", "compound"]
    # a merged table's columns could come from either side: not counted
    assert port(node(g, "calibration_points"), "standards")["columns_read"] == ["compound"]
    assert all(p["columns_read"] == [] for n in g["nodes"] for p in n["ports"] if p["source"] != "wire")
    assert all(p["missing"] == [] for n in g["nodes"] for p in n["ports"])


def test_graph_is_fast_enough_for_every_keystroke():
    flow.graph(GC)  # the toolbox and pandas are read once
    best = min(_timed(lambda: flow.graph(GC)) for _ in range(3))
    assert best < 0.1, best
    method = methodfile.parse(GC)
    assert min(_timed(lambda: flow.graph(method)) for _ in range(3)) < 0.1


def _timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_code_boxes_nested_calls_starred_keywords_tuples_classes_unknown():
    g = flow.graph(CODE)
    ids = [n["id"] for n in g["nodes"]]
    assert ids == ["table", "first", "table-2", "scaled", "more", "settings", "line", "mystery", "n"]
    first = node(g, "first")
    assert first["kind"] == "call" and first["outs"] == ["first", "second"]
    box = node(g, "table-2")
    assert box["kind"] == "code" and box["fn"] is None and box["outs"] == ["table"]
    assert box["code"] == "if factor > 1:\n    table = scale(table, factor)" and box["title"] == "if factor > 1"
    assert [(p["name"], p["source"]) for p in box["ports"]] == [("factor", "setting"), ("table", "wire")]
    assert port(box, "table")["from"] == "table"
    scaled = node(g, "scaled")
    assert [(p["name"], p["source"], p["code"]) for p in scaled["ports"]] == [
        ("table", "value", "scale(first, 2)"), ("factor", "setting", "factor"), ("column", "value", '"leaves"')]
    assert port(scaled, "table")["reads"] == ["first"] and port(scaled, "column")["keyword"] is True
    assert {"from": "first", "to": "scaled", "wire": "first", "port": "table"} in g["edges"]
    more = node(g, "more")
    assert [(p["name"], p["source"]) for p in more["ports"]] == [("*extra", "value")]
    settings = node(g, "settings")
    assert settings["fn_kind"] == "class"
    assert [(p["name"], p["source"]) for p in settings["ports"]][:2] == [
        ("smoothing_points", "value"), ("max_half_width_min", "value")]
    assert settings["ports"][2:] and all(p["source"] == "default" for p in settings["ports"][2:])
    line = node(g, "line")
    assert [p["name"] for p in line["ports"]] == ["x", "y"] and port(line, "x")["reads"] == ["second"]
    assert node(g, "line")["types"] == {"line": "data"}
    assert node(g, "mystery")["fn_kind"] == "unknown"
    assert any("unknown_function" in m["message"] for m in g["messages"])
    assert node(g, "n")["fn_kind"] == "builtin" and node(g, "n")["ports"][0]["wire"] == "scaled"
    # the later `table` is the code box's: `mystery` reads that one
    assert port(node(g, "mystery"), "table")["from"] == "table-2"  # unknown inputs are named by their argument
    assert g["groups"] == [
        {"id": "group-load", "title": "Load", "line": 28, "end_line": 30, "nodes": ["table", "first"],
         "closed_by": None},
        {"id": "group-shape-the-data", "title": "Shape the data", "line": 31, "end_line": 38,
         "nodes": ["table-2", "scaled", "more", "settings", "line", "mystery"], "closed_by": 39}]
    assert node(g, "n")["group"] is None and node(g, "scaled")["group"] == "group-shape-the-data"


def test_opens_as_flow_for_a_function_of_calls_only():
    src = SMALL.replace('def plants(', 'def both_steps(table, min_height_cm, control):\n'
                        '    """Both steps at once."""\n'
                        '    means = mean_heights(table, min_height_cm)\n'
                        '    return relative_heights(means, control)\n\n\ndef plants(')
    src = src.replace("relative_table = relative_heights(means, control)",
                      "relative_table = both_steps(table, min_height_cm, control)")
    g = flow.graph(src)
    assert node(g, "relative_table")["opens_as"] == "flow" and node(g, "means")["opens_as"] == "code"


# ---------------------------------------------------------------------------------------------------- runs


def test_run_values_status_and_types(runs):
    run = runs["gc"]
    assert run["status"] == "done", run.get("error")
    g = flow.graph(GC, run, runs["gc_exp"])
    assert g["stale"] is False and g["run"]["id"] == run["id"] and g["run"]["status"] == "done"
    assert all(n["status"] == "done" for n in g["nodes"])
    peaks = node(g, "peaks")
    assert peaks["ms"] > 0 and peaks["values"]["peaks"]["kind"] == "table" and "file" in peaks["values"]["peaks"]
    assert node(g, "flags")["types"]["flags"] == "flags" and node(g, "figure")["values"]["figure"]["kind"] == "figure"
    # PeakSettings isn't recorded as a step; its value is what the next step got
    settings = node(g, "settings")
    assert settings["values"]["settings"]["kind"] == "record" and settings["types"]["settings"] == "data"
    assert node(g, "design_flags")["types"]["design_flags"] == "flags"
    assert next(o for o in g["outputs"] if o["id"] == "activities")["value"]["kind"] == "table"
    by = {n["id"]: n for n in g["nodes"]}
    for n in g["nodes"]:  # every column read from the code is a column the incoming table really has
        for p in n["ports"]:
            if p["columns_read"]:
                have = set(by[p["from"]]["values"][p["wire"]]["columns"])
                assert set(p["columns_read"]) <= have, (n["id"], p["name"], set(p["columns_read"]) - have)
            assert p["missing"] == []
    s = {x["id"]: x for x in g["settings"]}
    assert s["biomass_gcdw_per_l"]["set"] is True and s["biomass_gcdw_per_l"]["value"] is not None
    assert next(i for i in g["inputs"] if i["id"] == "gaml_file")["value"] == "Example activity assay.gaml"


def test_every_wire_has_its_value_from_the_inside_record(runs):
    from lab.flow_tools import run_lines

    store, run = runs["store"], runs["gc"]
    lines = run_lines(store, run, run["source"])
    assert lines and all({"line", "end_line", "rounds"} <= set(x) for x in lines)
    g = flow.graph(GC, {**run, "lines": lines}, runs["gc_exp"])
    missing = [(n["id"], o) for n in g["nodes"] for o in n["outs"] if n["values"].get(o) is None]
    assert not missing, missing
    assert node(g, "settings")["values"]["settings"]["kind"] == "record"
    # a rerun reuses every step (cached), and still every wire has its value
    again = runs["run"](runs["gc_exp"], GC)
    g2 = flow.graph(GC, {**again, "lines": run_lines(store, again, again["source"])}, runs["gc_exp"])
    assert {n["status"] for n in g2["nodes"] if n["fn_kind"] in ("step", "toolbox")} == {"cached"}
    assert not [(n["id"], o) for n in g2["nodes"] for o in n["outs"] if n["values"].get(o) is None]
    assert node(g2, "peaks")["values"]["peaks"]["rows"] == node(g, "peaks")["values"]["peaks"]["rows"]
    # a code box (no step records it) gets its value from the recipe's own lines
    src = flow.edit(SMALL, {"op": "add_code", "code": "tall_pct = (table.height_cm > 10).mean() * 100",
                            "after": "table"})
    r = runs["run"](runs["small_exp"], src)
    assert r["status"] == "done", r.get("error")
    g3 = flow.graph(src, {**r, "lines": run_lines(store, r, r["source"])}, runs["small_exp"])
    box = node(g3, "tall_pct")
    assert box["kind"] == "code" and isinstance(box["values"]["tall_pct"], float)
    # an old run with no inside record still draws
    assert flow.graph(GC, {**run, "lines": []})["nodes"][0]["status"] == "done"


def _block(source: str, out: str, function: str, args: dict) -> str:
    return flow.edit(source, {"op": "add_step", "function": function, "out": out, "args": args})


def _ports(g: dict, node_id: str) -> dict:
    return {p["name"]: (p["columns_read"], p["missing"]) for p in node(g, node_id)["ports"] if p["source"] == "wire"}


def test_ready_made_steps_read_the_columns_named_at_the_call(runs):
    small, exp = runs["small"], runs["small_exp"]
    cases = [  # (function, args with a column the table has, the columns read; the same with one it lacks)
        ("pick_rows", {"table": "table", "column": {"value": "treatment"}, "equals": {"value": "control"}},
         {"table": ["treatment"]}, {"column": {"value": "colour"}}, {"table": ["colour"]}),
        ("summarize", {"table": "table", "values": {"value": "height_cm"}, "by": {"value": ["treatment", "light"]}},
         {"table": ["height_cm", "treatment", "light"]}, {"by": {"value": "pot"}}, {"table": ["pot"]}),
        ("fit_lines", {"table": "table", "x": {"value": "leaves"}, "y": {"value": "height_cm"},
                       "by": {"value": "treatment"}},
         {"table": ["leaves", "height_cm", "treatment"]}, {"x": {"value": "day"}}, {"table": ["day"]}),
        ("join_tables", {"first": "table", "second": "means", "on": {"value": "treatment"}},
         {"first": ["treatment"], "second": ["treatment"]}, {"on": {"value": "light"}}, {"second": ["light"]}),
    ]
    for function, args, reads, other, lacking in cases:
        src = _block(SMALL, "picked", function, args)
        checked(src, SMALL, new_warnings=True)
        bare = _ports(flow.graph(src), "picked")
        assert {k: r for k, (r, _) in bare.items()} == reads, (function, bare)
        assert all(m == [] for _, m in bare.values())  # no run: nothing is known to be missing
        # with the run's tables (the new box is not_run; the wires into it keep the run's values)
        got = _ports(flow.graph(src, small, exp), "picked")
        assert got == {k: (r, []) for k, r in reads.items()}, (function, got)
        (port_name, spec), = other.items()
        bad = flow.edit(src, {"op": "connect", "node": "picked", "port": port_name, **spec})
        got = _ports(flow.graph(bad, small, exp), "picked")
        for table_port, cols in lacking.items():
            assert got[table_port][1] == cols, (function, got)


def test_a_column_named_in_a_variable_is_not_a_column_called_str():
    src = SMALL.replace('    tall = table[table.height_cm >= min_height_cm]\n',
                        '    tall = table[table.height_cm >= min_height_cm]\n'
                        '    keep = tall["treatment"].str.len() > 0\n'
                        '    tall = tall[keep]\n')
    src = src.replace("def mean_heights(table, min_height_cm):",
                      'def mean_heights(table, min_height_cm, label="treatment"):')
    src = src.replace("    rows = []\n    for treatment",
                      "    names = tall[label].str.upper().dt\n    rows = []\n    for treatment", 1)
    assert src.count("tall[keep]") == 1 and src.count("tall[label].str") == 1
    reads = _ports(flow.graph(src), "means")["table"][0]
    assert "str" not in reads and "dt" not in reads and "upper" not in reads
    assert reads[:2] == ["height_cm", "treatment"]


def test_stale_run_and_a_missing_column(runs):
    run = runs["gc"]
    new = flow.edit(GC, {"op": "connect", "node": "calibration", "port": "standards", "wire": "ratios"})
    g = flow.graph(new, run, runs["gc_exp"])
    assert g["stale"] is True and g["run"]["stale"] is True
    cal = node(g, "calibration")
    assert cal["status"] == "not_run" and node(g, "ratios")["status"] == "done"
    assert port(cal, "standards")["missing"] == ["used"]  # calibration_standards made it; ratios lacks it
    assert node(g, "figure")["status"] == "done"
    # a renamed wire changes the text of the lines that use it: those are not_run, the rest keep their values
    renamed = flow.edit(GC, {"op": "rename_wire", "wire": "course", "to": "mean_course"})
    g = flow.graph(renamed, run)
    assert node(g, "mean_course")["status"] == "not_run" and node(g, "intervals")["status"] == "not_run"
    assert node(g, "peaks")["status"] == "done" and node(g, "peaks")["values"]["peaks"]["kind"] == "table"


def test_failed_run_marks_the_failing_node(runs):
    exp = {**runs["small_exp"], "params": {"control": "nobody"}}
    run = runs["run"](exp, SMALL)
    assert run["status"] == "failed"
    g = flow.graph(SMALL, run, exp)
    # steps reused from the run before (B2's recalculation) are `cached`
    assert [n["status"] for n in g["nodes"]][2] == "failed"
    assert all(n["status"] in ("done", "cached") for n in g["nodes"][:2])
    assert node(g, "relative_table")["error"] == run["error"]["message"]
    ok = flow.graph(SMALL, runs["small"], runs["small_exp"])
    assert [n["status"] for n in ok["nodes"]] == ["done", "done", "done"]
    means = node(ok, "means")
    assert port(means, "table")["columns_read"] == ["height_cm", "treatment"] and port(means, "table")["missing"] == []
    assert port(node(ok, "relative_table"), "means")["columns_read"] == ["treatment", "mean_cm"]
    assert {s["id"]: s["value"] for s in ok["settings"]} == {"min_height_cm": 1.0, "control": "control"}


# ---------------------------------------------------------------------------------------------------- edits on the GC method


def test_add_and_remove_step_gc():
    op = {"op": "add_step", "function": "time_course", "args": {"concentrations": "concentrations"}, "out": "again"}
    new, g = roundtrip(GC, op, {"op": "remove_step", "node": "again"})
    added = node(g, "again")
    assert added["line"] == P + 7 and [n["id"] for n in g["nodes"]].index("again") == 11  # right after its input
    assert port(added, "concentrations")["from"] == "concentrations"
    assert "    again = time_course(concentrations)\n" in new
    # a toolbox function not imported yet: the import line gets it, wrapped as the file wraps it
    op = {"op": "add_step", "function": "read_table", "args": {"table_file": {"value": "extra.csv"}}}
    new, g = roundtrip(GC, op, {"op": "remove_step", "node": "table"})
    assert 'table = read_table("extra.csv")' in new and g["nodes"][0]["id"] == "table"
    head = new.split("\n\n# The kinds of correction")[0]
    assert "flags_table, match_name, read_table\n" in head
    assert all(len(line) <= 120 for line in new.splitlines())


def test_remove_step_refuses_what_is_used():
    with pytest.raises(LabError, match=r"`ratios` is used by Calibration standards and Quantify: connect those to "
                                       r"something else first"):
        flow.edit(GC, {"op": "remove_step", "node": "ratios"})
    with pytest.raises(LabError, match=r"`figure` is a result \(Time course figure\): remove the result first"):
        flow.edit(GC, {"op": "remove_step", "node": "figure"})
    with pytest.raises(LabError, match="There is no step `nope`"):
        flow.edit(GC, {"op": "remove_step", "node": "nope"})
    # once the result is gone the step can go, and its toolbox import with it
    src = flow.edit(GC, {"op": "remove_result", "name": "figure"})
    src2 = flow.edit(src, {"op": "remove_step", "node": "figure"})
    checked(src2, GC)
    assert "plot_time_courses" not in src2 and "figure:" not in src2.split("Returns:")[1]


def test_connect_and_back_gc():
    op = {"op": "connect", "node": "concentrations", "port": "ratios", "wire": "standards"}
    new, g = roundtrip(GC, op, {"op": "connect", "node": "concentrations", "port": "ratios", "wire": "ratios"})
    assert port(node(g, "concentrations"), "ratios")["from"] == "standards"
    assert "    concentrations = quantify(standards, calibration, concentration_factor)\n" in new
    # a setting, a value, and a parameter that wasn't passed
    new = flow.edit(GC, {"op": "connect", "node": "balance_flags", "port": "mass_balance_range_pct",
                         "value": [70, 130]})
    assert "balance_flags = mass_balance_checks(balance, [70, 130])" in new
    checked(new, GC)
    new = flow.edit(GC, {"op": "connect", "node": "standard_flags", "port": "shoulder_fwhm_ratio",
                         "setting": "min_r2"})
    assert port(node(checked(new, GC), "standard_flags"), "shoulder_fwhm_ratio")["setting"] == "min_r2"
    with pytest.raises(LabError, match="There is no setting `nope`"):
        flow.edit(GC, {"op": "connect", "node": "standard_flags", "port": "shoulder_fwhm_ratio", "setting": "nope"})
    with pytest.raises(LabError, match="has no input `nope`"):
        flow.edit(GC, {"op": "connect", "node": "standard_flags", "port": "nope", "wire": "peaks"})


def test_connect_a_later_wire_moves_or_refuses():
    with pytest.raises(LabError, match="make the order loop"):
        flow.edit(GC, {"op": "connect", "node": "samples", "port": "injections", "wire": "peaks"})
    # `copy` doesn't need Mean heights, but Relative heights (in between) does: Mean heights can't move after it
    src = flow.edit(SMALL, {"op": "add_code", "code": "copy = table.copy()", "after": "relative_table"})
    with pytest.raises(LabError, match=r"Relative heights in between needs the result of Mean heights: the order "
                                       "would loop"):
        flow.edit(src, {"op": "connect", "node": "means", "port": "table", "wire": "copy"})
    new = flow.edit(GC, {"op": "connect", "node": "design_flags", "port": "samples", "wire": "ratios"})
    g = checked(new, GC)
    order = [n["id"] for n in g["nodes"]]
    assert order.index("design_flags") == order.index("ratios") + 1
    assert port(node(g, "design_flags"), "samples")["from"] == "ratios"


def test_rename_wire_and_back_gc():
    op = {"op": "rename_wire", "wire": "course", "to": "mean_course"}
    new, g = roundtrip(GC, op, {"op": "rename_wire", "wire": "mean_course", "to": "course"})
    assert '"time_course": mean_course' in new and "intervals = interval_rates(mean_course, biomass_gcdw_per_l, biomass_by_substrate)" in new
    assert node(g, "mean_course")["result"]["id"] == "time_course"
    assert "course" not in [w for n in g["nodes"] for w in n["outs"]]
    # a longer name: the lines that use it are wrapped again at 120 columns, aligned under the first argument
    new = flow.edit(GC, {"op": "rename_wire", "wire": "peaks", "to": "integrated_peaks_of_every_injection"})
    checked(new, GC)
    assert all(len(line) <= 120 for line in new.splitlines())
    assert "    standard_flags = internal_standard_checks(integrated_peaks_of_every_injection, references, " \
           "internal_standard,\n                                              is_area_tolerance_pct, " \
           "shoulder_fwhm_ratio)\n" in new
    for taken in ("peaks", "quantify", "fit_line", "min_r2", "len", "DESIGN_COLUMNS"):
        with pytest.raises(LabError, match="is taken"):
            flow.edit(GC, {"op": "rename_wire", "wire": "ratios", "to": taken})
    with pytest.raises(LabError, match="can't be a wire name"):
        flow.edit(GC, {"op": "rename_wire", "wire": "ratios", "to": "2nd"})


def test_add_and_remove_setting_gc():
    op = {"op": "add_setting", "name": "max_blank_snr", "default": 5, "label": "Largest blank signal",
          "help": "A blank above it is flagged.", "group": "Checks"}
    new, g = roundtrip(GC, op, {"op": "remove_setting", "setting": "max_blank_snr"})
    s = next(x for x in g["settings"] if x["id"] == "max_blank_snr")
    assert s["group"] == "Checks" and s["default"] == 5 and s["label"] == "Largest blank signal"
    assert s["help"] == "A blank above it is flagged." and s["used_by"] == []
    assert "cv_min_conc_mM=0.02,\n                   mass_balance_range_pct=(80, 120), max_blank_snr=5):" in new
    # a new group, and a required setting without a default
    op = {"op": "add_setting", "name": "dilution", "label": "Dilution", "unit": "x", "group": "Sample prep",
          "type": "number"}
    new, g = roundtrip(GC, op, {"op": "remove_setting", "setting": "dilution"})
    s = next(x for x in g["settings"] if x["id"] == "dilution")
    assert s["group"] == "Sample prep" and s["required"] is True and s["unit"] == "x"
    assert "def activity_assay(gaml_file, corrections, compounds, dilution, channel=None," in new
    with pytest.raises(LabError, match=r"`min_r2` is used by Calibration checks: connect that to something else"):
        flow.edit(GC, {"op": "remove_setting", "setting": "min_r2"})
    with pytest.raises(LabError, match="is taken"):
        flow.edit(GC, {"op": "add_setting", "name": "min_r2", "default": 1})


def test_set_default_gc():
    new, g = roundtrip(GC, {"op": "set_default", "setting": "min_r2", "value": 0.99},
                       {"op": "set_default", "setting": "min_r2", "value": 0.995})
    assert next(s for s in g["settings"] if s["id"] == "min_r2")["default"] == 0.99
    # a setting without a default gets one: it moves among the settings with defaults (Python needs that)
    rows = [{"compound": "octanol", "rt_min": 4.2}]
    new = flow.edit(GC, {"op": "set_default", "setting": "compounds", "value": rows})
    g = checked(new, GC)
    s = next(x for x in g["settings"] if x["id"] == "compounds")
    assert s["default"] == rows and s["required"] is False and s["type"] == "rows"
    assert 'def activity_assay(gaml_file, corrections, compounds=[{"compound": "octanol", "rt_min": 4.2}], ' \
           'channel=None,' in new


def test_add_input_gc():
    new = flow.edit(GC, {"op": "add_input", "name": "plate_file", "label": "Plate reader export",
                         "types": [".csv", "xlsx"]})
    g = checked(new, GC)
    assert "def activity_assay(gaml_file, plate_file, corrections, compounds," in new
    assert "        plate_file: Plate reader export [.csv, .xlsx]\n" in new
    assert next(i for i in g["inputs"] if i["id"] == "plate_file")["types"] == [".csv", ".xlsx"]
    with pytest.raises(LabError, match="ends in _file"):
        flow.edit(GC, {"op": "add_input", "name": "plate"})
    assert flow.edit(new, {"op": "remove_input", "input": "plate_file"}) == GC
    with pytest.raises(LabError, match=r"`gaml_file` is used by Read gaml: remove that"):
        flow.edit(GC, {"op": "remove_input", "input": "gaml_file"})
    with pytest.raises(LabError, match="There is no input `nope`"):
        flow.edit(GC, {"op": "remove_input", "input": "nope"})


DEFAULTS = '''"""Defaults

Inputs taken back to their defaults.
"""

from toolbox import read_table


def shift(table, a=1, b=2, *, column="x"):
    """Add a and b to a column."""
    out = table.copy()
    out[column] = out[column] + a + b
    return out


def recipe(table_file):
    """Defaults.

    Inputs:
        table_file: A table [.csv]
    """
    table = read_table(table_file)
    moved = shift(table, 5, 6, column="y")
    return {"moved": moved}
'''


def test_disconnect_takes_an_input_back_to_its_default():
    new = flow.edit(DEFAULTS, {"op": "disconnect", "node": "moved", "port": "column"})
    assert "    moved = shift(table, 5, 6)\n" in new
    checked(new, DEFAULTS)
    assert flow.edit(new, {"op": "connect", "node": "moved", "port": "column", "value": "y"}) == DEFAULTS
    # a positional argument: the ones after it keep their input by name
    new = flow.edit(DEFAULTS, {"op": "disconnect", "node": "moved", "port": "a"})
    assert '    moved = shift(table, b=6, column="y")\n' in new
    g = checked(new, DEFAULTS)
    assert port(node(g, "moved"), "b")["code"] == "6"
    assert flow.edit(new, {"op": "disconnect", "node": "moved", "port": "a"}) == new  # not connected: nothing to do
    with pytest.raises(LabError, match="has no default: it must be connected"):
        flow.edit(DEFAULTS, {"op": "disconnect", "node": "moved", "port": "table"})
    with pytest.raises(LabError, match="has no input `nope`"):
        flow.edit(DEFAULTS, {"op": "disconnect", "node": "moved", "port": "nope"})
    # the GC method's steps take every input on purpose (no defaults): disconnecting is refused in plain words
    with pytest.raises(LabError, match="`shoulder_fwhm_ratio` of Internal standard checks has no default"):
        flow.edit(GC, {"op": "disconnect", "node": "standard_flags", "port": "shoulder_fwhm_ratio"})


def test_add_and_remove_result_gc():
    op = {"op": "add_result", "wire": "ratios", "label": "Area ratios"}
    new, g = roundtrip(GC, op, {"op": "remove_result", "name": "ratios"})
    assert node(g, "ratios")["result"] == {"id": "ratios", "label": "Area ratios"}
    assert '"flags": flags, "figure": figure,\n            "ratios": ratios}\n' in new and "        ratios: Area ratios\n" in new
    with pytest.raises(LabError, match="already a result called `peaks`"):
        flow.edit(GC, {"op": "add_result", "wire": "peaks"})
    # removing one in the middle keeps the rest in their places
    new = flow.edit(GC, {"op": "remove_result", "name": "mass_balance"})
    g = checked(new, GC)
    assert [o["id"] for o in g["outputs"]] == ["activities", "time_course", "concentrations", "interval_rates",
                                               "calibration", "calibration_points", "peaks", "flags", "figure"]
    assert "mass_balance: Mass balance" not in new and "balance = mass_balance(course)" in new


def test_group_and_ungroup_gc():
    checks = QUALITY[1:-1]
    new, g = roundtrip(GC, {"op": "group", "nodes": checks, "title": "Quality checks"},
                       {"op": "ungroup", "group": "group-quality-checks"})
    assert g["groups"] == [{"id": "group-quality-checks", "title": "Quality checks", "line": P + 11, "end_line": P + 20,
                            "nodes": checks, "closed_by": P + 21}]
    assert node(g, "flags")["group"] is None
    with pytest.raises(LabError, match="follow each other"):
        flow.edit(GC, {"op": "group", "nodes": ["samples", "peaks"], "title": "x"})
    with pytest.raises(LabError, match="already in the group"):
        flow.edit(new, {"op": "group", "nodes": ["shape_flags"], "title": "Again"})


def test_add_code_and_remove_it_gc():
    op = {"op": "add_code", "code": "n_peaks = peaks.shape[0]"}
    _, g = roundtrip(GC, op, {"op": "remove_step", "node": "n_peaks"}, new_warnings=True)
    box = node(g, "n_peaks")
    assert box["kind"] == "code" and [p["wire"] for p in box["ports"]] == ["peaks"]
    assert [n["id"] for n in g["nodes"]].index("n_peaks") == 6
    with pytest.raises(LabError, match="is taken"):
        flow.edit(GC, {"op": "add_code", "code": "peaks = 1"})
    with pytest.raises(LabError, match="would loop"):
        flow.edit(GC, {"op": "add_code", "code": "x = course.shape", "after": "peaks"})


def test_new_function_then_call_it_gc():
    new = flow.edit_all(GC, [
        {"op": "new_function", "name": "peak_count", "params": ["peaks"],
         "sentence": "The number of peaks of each compound", "body": 'return peaks.groupby("compound").size()'},
        {"op": "add_step", "function": "peak_count", "out": "counts"},  # `peaks` is connected by its name
    ])
    g = checked(new, GC)
    counts = node(g, "counts")
    assert counts["fn_kind"] == "step" and port(counts, "peaks")["from"] == "peaks"
    assert counts["sentence"] == "The number of peaks of each compound."
    assert port(counts, "peaks")["columns_read"] == ["compound"]
    assert ('def peak_count(peaks):\n    """The number of peaks of each compound."""\n'
            '    return peaks.groupby("compound").size()\n\n\n'
            "# ---------------------------------------------------------------- the recipe") in new
    with pytest.raises(LabError, match="is taken"):
        flow.edit(GC, {"op": "new_function", "name": "quantify"})


def test_comments_in_the_recipe_stay_byte_for_byte():
    src = GC.replace("    ratios = area_ratios(peaks, internal_standard)\n",
                     "    # the internal standard cancels the injection volume\n"
                     "    ratios = area_ratios(peaks,  # every compound\n"
                     "                         internal_standard)\n")
    new = flow.edit(src, {"op": "rename_wire", "wire": "peaks", "to": "all_peaks"})
    assert "    # the internal standard cancels the injection volume\n" \
           "    ratios = area_ratios(all_peaks,  # every compound\n" \
           "                         internal_standard)\n" in new
    assert flow.edit(new, {"op": "rename_wire", "wire": "all_peaks", "to": "peaks"}) == src


def test_comments_on_the_signature_and_the_import_line_stay():
    src = SMALL.replace('def plants(table_file, min_height_cm=0.0, control="control"):',
                        'def plants(table_file, min_height_cm=0.0, control="control"):  # the recipe')
    src = src.replace("from toolbox import read_table\n", "from toolbox import read_table  # reading files\n")
    new = flow.edit(src, {"op": "add_setting", "name": "unit", "default": "cm"})
    assert 'def plants(table_file, min_height_cm=0.0, control="control", unit="cm"):  # the recipe\n' in new
    new = flow.edit(src, {"op": "add_step", "function": "pick_rows", "out": "picked",
                          "args": {"table": "means", "column": {"value": "treatment"}, "equals": {"value": "x"}}})
    assert "from toolbox import pick_rows, read_table  # reading files\n" in new


# ---------------------------------------------------------------------------------------------------- edits on a small method


def test_every_op_on_the_small_method():
    src = SMALL
    steps = [
        ({"op": "add_setting", "name": "unit", "default": "cm"}, False),
        ({"op": "set_default", "setting": "control", "value": "compost"}, False),
        ({"op": "add_input", "name": "notes_file", "optional": True, "types": ".txt"}, False),
        ({"op": "new_function", "name": "count_plants", "params": ["table"], "sentence": "How many plants",
          "body": "return len(table)"}, False),
        ({"op": "add_step", "function": "count_plants", "args": {"table": "table"}, "out": "count"}, False),
        ({"op": "add_result", "wire": "count", "label": "Number of plants"}, False),
        ({"op": "connect", "node": "relative_table", "port": "means", "wire": "means"}, None),
        ({"op": "rename_wire", "wire": "means", "to": "mean_table"}, False),
        ({"op": "group", "nodes": ["mean_table", "relative_table"], "title": "Heights"}, False),
        ({"op": "add_code", "code": "tall = mean_table[mean_table.mean_cm > 10]"}, True),
        ({"op": "remove_step", "node": "tall"}, False),
        ({"op": "ungroup", "group": "group-heights"}, False),
        ({"op": "remove_result", "name": "count"}, False),
        ({"op": "remove_step", "node": "count"}, False),
        ({"op": "remove_setting", "setting": "unit"}, False),
    ]
    for op, warn in steps:
        new = flow.edit(src, op)
        if warn is None:
            assert new == src  # connecting what is already connected changes nothing
            continue
        g = checked(new, src, new_warnings=warn)
        src = new
    assert node(g, "mean_table")["outs"] == ["mean_table"] and g["groups"] == []
    assert 'def plants(table_file, min_height_cm=0.0, control="compost", notes_file=None):' in src
    assert "        notes_file: Notes [.txt]\n" in src
    assert "count_plants" in src and "count = count_plants" not in src


def test_a_method_built_from_nothing_runs(runs):
    src = flow.new_method("Plant heights", "Mean height per treatment.")
    assert checker.check(src)["ok"] and checker.check(src)["messages"] == []
    g = flow.graph(src)
    assert g["nodes"] == [] and g["outputs"] == [] and g["messages"] == []
    src = flow.edit_all(src, [
        {"op": "add_input", "name": "table_file", "label": "The plants", "types": [".csv"]},
        {"op": "add_step", "function": "read_table"},
        {"op": "add_setting", "name": "group_column", "default": "treatment", "label": "Group column"},
        {"op": "new_function", "name": "mean_heights", "params": ["table", "group_column"],
         "sentence": "The mean height of each group",
         "body": ['means = table.groupby(group_column, as_index=False)["height_cm"].mean()', "return means"]},
        {"op": "add_step", "function": "mean_heights", "args": {"table": "table"}, "out": "means"},
        {"op": "add_result", "wire": "means", "label": "Mean heights"},
    ])
    result = checker.check(src)
    assert result["ok"] and result["messages"] == [], result["messages"]
    assert "from toolbox import read_table\n" in src and "    table = read_table(table_file)\n" in src
    exp = {**runs["small_exp"], "params": {}}
    run = runs["run"](exp, src)
    assert run["status"] == "done", run.get("error")
    g = flow.graph(src, run, exp)
    assert [n["status"] for n in g["nodes"]][1] == "done"  # the read may be remembered from an earlier test
    assert g["nodes"][0]["status"] in ("done", "cached")
    assert node(g, "means")["values"]["means"]["columns"] == ["treatment", "height_cm"]
    assert node(g, "means")["result"] == {"id": "means", "label": "Mean heights"}


def test_edit_errors_are_plain():
    with pytest.raises(LabError, match="There is no change called `fly`"):
        flow.edit(SMALL, {"op": "fly"})
    with pytest.raises(LabError, match="needs `function`"):
        flow.edit(SMALL, {"op": "add_step"})
    with pytest.raises(LabError, match="There is no function `nope`"):
        flow.edit(SMALL, {"op": "add_step", "function": "nope"})
    with pytest.raises(LabError, match=r"Relative needs `value`, `control_mean`: connect them"):
        flow.edit(SMALL, {"op": "add_step", "function": "relative"})
    with pytest.raises(LabError, match="can't go before it"):
        flow.edit(SMALL, {"op": "add_step", "function": "relative_heights", "args": {"means": "means"},
                          "after": "table"})
    with pytest.raises(LabError, match=r"Change 2 \(rename_wire\)"):
        flow.edit_all(SMALL, [{"op": "add_setting", "name": "x", "default": 1},
                              {"op": "rename_wire", "wire": "means", "to": "x"}])
    with pytest.raises(LabError, match="can't be read"):
        flow.edit("def broken(:\n", {"op": "add_setting", "name": "x"})


# ---------------------------------------------------------------------------------------------------- the tools


class FakeApi:
    def __init__(self, store):
        self.store = store

    def pick_run(self, run, experiment):
        if run:
            return self.store.get_run(run)
        e = self.store.get_experiment(experiment)
        if not e.get("last_run"):
            raise LabError("not run yet")
        return self.store.get_run(e["last_run"]["id"])

    def method_of(self, e):
        return self.store.get_method(e["method"], e.get("method_version"))


def call(mcp, name: str, args: dict):
    result = asyncio.run(mcp.call_tool(name, args))
    if isinstance(result, tuple):
        result = result[1]
    return result["result"] if isinstance(result, dict) and list(result) == ["result"] else result


def test_tools(runs):
    from lab import flow_tools
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    store = runs["store"]
    mcp = FastMCP("t")
    flow_tools.register(mcp, store, FakeApi(store))
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"flow_get", "flow_edit", "flow_new"} <= tools

    got = call(mcp, "flow_get", {"method": "gc-activity-assay"})
    assert len(got["nodes"]) == 25 and got["method"]["id"] == "gc-activity-assay" and got["run"] is None
    got = call(mcp, "flow_get", {"run": runs["gc"]["id"]})  # the run's own code
    assert got["stale"] is False and node(got, "peaks")["status"] == "done"
    got = call(mcp, "flow_get", {"method": "gc-activity-assay", "run": runs["gc"]["id"],
                                 "experiment": runs["gc_exp"]["id"]})
    assert got["stale"] is False and got["experiment"]["id"] == runs["gc_exp"]["id"]
    got = call(mcp, "flow_get", {"source": SMALL})
    assert [n["id"] for n in got["nodes"]] == ["table", "means", "relative_table"]

    edited = call(mcp, "flow_edit", {"method": "gc-activity-assay", "ops": [
        {"op": "add_result", "wire": "ratios", "label": "Area ratios"}]})
    assert set(edited) >= {"source", "flow", "check", "diff"} and edited["ok"] is True and edited["check"] == []
    assert node(edited["flow"], "ratios")["result"]["id"] == "ratios"
    assert any(line.startswith("+") and '"ratios": ratios' in line for line in edited["diff"])
    assert store.get_method("gc-activity-assay")["source"] == GC  # nothing saved
    with pytest.raises(ToolError, match="is used by"):
        call(mcp, "flow_edit", {"source": GC, "ops": [{"op": "remove_step", "node": "ratios"}]})

    new = call(mcp, "flow_new", {"name": "Growth", "description": "Growth rates."})
    assert new["ok"] and new["check"] == [] and new["flow"]["nodes"] == []
    assert new["source"].startswith('"""Growth\n\nGrowth rates.\n"""')


OD = '''"""Growth

Growth rates from OD600.
"""

import numpy as np

from toolbox import read_table


def ln_od(od600, blank_od600):
    """The natural log of the blank-corrected OD600."""
    return np.log(od600 - blank_od600)


def growth(table_file, blank_od600=0.05):
    """Growth.

    Inputs:
        table_file: OD readings [.csv]

    Settings:
        blank_od600: Blank OD600.

    Returns:
        od: Readings
    """
    od = read_table(table_file)
    return {"od": od}
'''


def test_ready_made_blocks_through_add_step():
    new = flow.edit_all(OD, [
        {"op": "add_step", "function": "pick_rows", "out": "early",
         "args": {"table": "od", "column": {"value": "time_h"}, "between": {"value": [2, 6]}}},
        {"op": "add_step", "function": "add_column", "out": "logs",
         "args": {"table": "early", "formula": "ln_od", "blank_od600": "blank_od600"}},
        {"op": "add_step", "function": "fit_lines", "out": "rates",
         "args": {"table": "logs", "x": {"value": "time_h"}, "y": {"value": "ln_od"}, "by": {"value": "culture"}}},
        {"op": "add_result", "wire": "rates", "label": "Growth rates"},
    ])
    g = checked(new, OD)
    assert "from toolbox import add_column, fit_lines, pick_rows, read_table\n" in new
    assert '    early = pick_rows(od, "time_h", between=[2, 6])\n' in new
    assert "    logs = add_column(early, ln_od, blank_od600=blank_od600)\n" in new
    assert '    rates = fit_lines(logs, "time_h", "ln_od", by="culture")\n' in new
    formula = port(node(g, "logs"), "formula")
    assert formula["source"] == "formula" and formula["function"] == "ln_od" and formula["wire"] is None
    assert port(node(g, "logs"), "blank_od600")["setting"] == "blank_od600"
    assert port(node(g, "rates"), "by")["code"] == '"culture"' and port(node(g, "rates"), "by")["keyword"]
    assert {n["id"]: n["fn_kind"] for n in g["nodes"]} == {"od": "toolbox", "early": "toolbox", "logs": "toolbox",
                                                           "rates": "toolbox"}
    assert node(g, "rates")["types"] == {"rates": "table"} and node(g, "rates")["result"]["id"] == "rates"
    # a parenthesized import keeps its wrapping
    general = "from toolbox import combine_flags, fit_line, fit_line_through_zero, flag, flags_table, match_name\n"
    wrapped = GC.replace(general, "from toolbox import (combine_flags, fit_line, fit_line_through_zero, flag,\n"
                                  "                     flags_table, match_name)\n")
    assert general in GC and wrapped != GC
    gc = flow.edit(wrapped, {"op": "add_step", "function": "fit_lines", "out": "lines",
                             "args": {"table": "course", "x": {"value": "time_min"}, "y": {"value": "mean"},
                                      "by": {"value": ["strain", "compound"]}}})
    checked(gc, wrapped)
    others_unchanged(wrapped, gc)
    imp = [ln for ln in gc.split("\n\n# The kinds of correction")[0].splitlines()[10:] if ln.strip()]
    assert imp[0].startswith("from toolbox import (") and imp[1].endswith("match_name)")
    assert imp[1].startswith(" " * 21)  # continuation lines aligned under the first name
    assert imp[2].startswith("from toolbox.gc import ")  # the add-on's line is left alone
    assert "fit_lines" in " ".join(imp) and all(len(line) <= 120 for line in gc.splitlines())
    # undone: the import line is the only one that may be wrapped differently (it wasn't filled to 120 columns)
    back = flow.edit(gc, {"op": "remove_step", "node": "lines"})
    assert back.split("\n\n# The kinds of correction")[1] == wrapped.split("\n\n# The kinds of correction")[1]
    assert [i["names"] for i in methodfile.parse(back).imports] == \
        [i["names"] for i in methodfile.parse(wrapped).imports]


def test_an_add_on_function_gets_its_own_import_line():
    """A block of a toolbox add-on is imported from the add-on (`from toolbox.<add-on> import`): a new line after
    the other imports, gone again when its last use goes."""
    new = flow.edit(SMALL, {"op": "add_input", "name": "trace_file", "types": [".gaml"]})
    added = flow.edit(new, {"op": "add_step", "function": "read_gaml", "args": {"gaml_file": {"input": "trace_file"}},
                            "out": "injections"})
    assert "from toolbox import read_table\nfrom toolbox.gc import read_gaml\n" in added
    assert "    injections = read_gaml(trace_file)\n" in added
    g = checked(added, new)
    assert node(g, "injections")["fn_kind"] == "toolbox"
    assert flow.edit(added, {"op": "remove_step", "node": "injections"}) == new
    # not imported at all: the flow says from where to import it
    missing = added.replace("from toolbox.gc import read_gaml\n", "")
    assert any("`from toolbox.gc import` line" in m["message"] for m in flow.graph(missing)["messages"])

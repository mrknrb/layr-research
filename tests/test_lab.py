"""Methods, experiments and runs: through the MCP server as layr starts it, plus unit tests of the formula
language and the method checks. Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from test_server import SAMPLE, error_text, server, value

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import engine  # noqa: E402
from lab.core import REGISTRY, StepError  # noqa: E402
from lab.export import tex_image  # noqa: E402
from lab.formula import Formula  # noqa: E402
from lab.store import Store  # noqa: E402

# ---------------------------------------------------------------------------------------------------- formulas


def test_formula_evaluates_and_draws():
    df = pd.DataFrame({"od": [0.2, 0.4, 0.6, 0.8], "group": ["c", "c", "t", "t"], "dry mass": [1, 2, 3, 4]})
    f = Formula('od / mean(od[group == "c"])')
    assert list(f.evaluate(df).round(6)) == [round(x / 0.3, 6) for x in (0.2, 0.4, 0.6, 0.8)]
    assert f.columns() == ["od", "group"]
    assert f.latex() == r"\frac{\mathrm{od}}{\overline{\mathrm{od}}_{\mathrm{group} = \mathrm{c}}}"
    assert f.latex(df.iloc[2].to_dict(), df) == r"\frac{0.6}{0.3}"
    g = Formula("log2(`dry mass` / 2) ^ 2 - sd(od)")
    assert abs(g.evaluate(df).iloc[3] - (1.0 - df.od.std())) < 1e-12
    assert "\\log_{2}" in g.latex() and "dry\\ mass" in g.latex()
    by = Formula("od - mean(od)").evaluate(df, ["group"])
    assert list(by.round(6)) == [-0.1, 0.1, -0.1, 0.1]
    assert tex_image(f.latex()) and tex_image(g.latex())


@pytest.mark.parametrize("bad", ["__import__('os').system('x')", "od.__class__", "(lambda: 1)()", "[1, 2]",
                                 "od if od else 1", "open('x')", "od @ od"])
def test_formula_refuses_code(bad):
    with pytest.raises(StepError):
        Formula(bad)


def test_every_step_explains_and_renders():
    for st in REGISTRY.values():
        sh = st.explain(st.defaults())
        assert st.sentence and st.title, st.id
        for f in sh["formulas"]:
            assert tex_image(f["latex"]) is not None, (st.id, f["latex"])
        for s in sh["symbols"]:
            assert tex_image(s["symbol"]) is not None, (st.id, s["symbol"])


# ---------------------------------------------------------------------------------------------------- methods


def test_patch_is_validated_and_versioned(tmp_path: Path):
    store = Store(tmp_path)
    m = {"id": "plants", "name": "Plants", "inputs": [{"id": "data", "kind": "file", "label": "Table"}],
         "params": [], "steps": [{"id": "read", "type": "table.read", "inputs": {"file": "$data"}}], "outputs": {}}
    assert engine.validate(m, store) == []
    bad = engine.apply_patch(m, [{"op": "add", "step": {"id": "rel", "type": "table.compute",
                                                        "inputs": {"table": "nope.table"}}}])
    assert any("nope.table" in p["message"] for p in engine.errors(engine.validate(bad, store)))
    good = engine.apply_patch(m, [
        {"op": "add", "step": {"id": "rel", "type": "table.compute", "inputs": {"table": "read.table"},
                               "params": {"column": "rel", "formula": "height_cm / mean(height_cm)"}}},
        {"op": "output", "name": "relative", "from": "rel.table", "database": True}])
    assert engine.errors(engine.validate(good, store)) == []
    lines = engine.diff(m, good)
    assert any("Added step" in x for x in lines) and any("Outputs" in x for x in lines)
    with pytest.raises(StepError):
        engine.apply_patch(m, [{"op": "remove", "step": "missing"}])
    with pytest.raises(StepError):
        engine.apply_patch(m, [{"op": "explode"}])


# ---------------------------------------------------------------------------------------------------- end to end


async def test_sample_experiment_end_to_end(tmp_path: Path):
    async with server(tmp_path) as s:
        tools = {t.name for t in (await s.list_tools()).tools}
        assert {"step_catalog", "method_patch", "run", "explain", "provenance", "chromatogram",
                "correction_propose", "database_query", "export"} <= tools
        cat = value(await s.call_tool("step_catalog", {}))
        assert {"gc.integrate", "table.compute", "stats.compare_groups"} <= {c["type"] for c in cat}
        got = value(await s.call_tool("method_get", {"method": "gc-activity-assay"}))
        assert got["problems"] == []

        exp = value(await s.call_tool("lab_sample", {}))
        eid = exp["experiment"]
        run = value(await s.call_tool("run", {"experiment": eid}))
        assert run["status"] == "done", run
        acts = value(await s.call_tool("result_table", {"experiment": eid, "table": "activities"}))
        by = {r["compound"]: r for r in acts["rows"]}
        assert by["SUB"]["activity_U_per_gCDW"] < -20 and by["PROD"]["activity_U_per_gCDW"] > 15
        assert acts["run"] == run["run"]

        ex = value(await s.call_tool("explain", {"experiment": eid}))
        assert "Calibration lines" in ex["methods_text"] and len(ex["steps"]) == 13
        conc = next(x for x in ex["steps"] if x["step"] == "conc")
        assert conc["run"]["examples"] and r"\frac" in conc["run"]["examples"][0]["latex"]
        general = value(await s.call_tool("explain", {"method": "gc-activity-assay", "step": "cal"}))
        assert general["steps"][0]["formulas"] and "run" not in general["steps"][0]

        prov = value(await s.call_tool("provenance", {"experiment": eid, "table": "concentrations",
                                                      "where": {"injection": 20, "compound": "SUB"}}))
        assert [c["step"] for c in prov["chain"]] == ["peaks", "ratio", "conc"]

        chrom = value(await s.call_tool("chromatogram", {"experiment": eid, "injection": 20, "start_min": 3.4,
                                                         "end_min": 3.9, "points": 400}))
        assert 0 < len(chrom["time"]) <= 400 and {p["compound"] for p in chrom["peaks"]} == {"SUB", "PROD", "octanol"}

        # an agent (no panel caller here) only proposes; the decision makes it apply
        prop = value(await s.call_tool("correction_propose", {
            "experiment": eid, "kind": "exclude", "injection": 20, "compound": "SUB",
            "reason": "Test: pipetting error noted in the lab book"}))
        assert prop["correction"]["status"] == "proposed"
        value(await s.call_tool("run", {"experiment": eid}))
        pk = value(await s.call_tool("result_table", {"experiment": eid, "table": "peaks",
                                                      "where": {"injection": 20, "compound": "SUB"}}))
        assert pk["rows"][0]["status"] == "ok"
        value(await s.call_tool("correction_decide", {"experiment": eid, "correction": prop["correction"]["id"],
                                                      "decision": "approve"}))
        value(await s.call_tool("run", {"experiment": eid}))
        pk = value(await s.call_tool("result_table", {"experiment": eid, "table": "peaks",
                                                      "where": {"injection": 20, "compound": "SUB"}}))
        assert pk["rows"][0]["status"] == "excluded"
        bad = await s.call_tool("correction_propose", {"experiment": eid, "kind": "exclude", "injection": 3,
                                                       "reason": "  "})
        assert "reason" in error_text(bad)

        tables = value(await s.call_tool("database_tables", {}))
        assert {"activities", "time_course", "concentrations"} <= {t["table"] for t in tables}
        q = value(await s.call_tool("database_query", {"table": "activities", "where": {"compound": "SUB"}}))
        assert len(q["rows"]) == 1 and q["rows"][0]["experiment"] == eid

        for fmt in ("html", "xlsx", "docx"):
            out = value(await s.call_tool("export", {"experiment": eid, "format": fmt}))
            assert Path(out["path"]).stat().st_size > 5000, fmt
        out = value(await s.call_tool("export", {"experiment": eid, "format": "html"}))
        html = Path(out["path"]).read_text("utf-8")
        assert "How every number was calculated" in html and "Test: pipetting error" in html


async def test_table_method_from_scratch(tmp_path: Path):
    """What an agent does for a new kind of analysis: a method of table steps, an experiment, a run."""
    async with server(tmp_path) as s:
        made = value(await s.call_tool("method_create", {
            "method": "plant-growth", "name": "Plant growth by treatment",
            "inputs": [{"id": "data", "kind": "file", "label": "Measurements (CSV)"}],
            "steps": [
                {"id": "read", "type": "table.read", "inputs": {"file": "$data"}},
                {"id": "rel", "type": "table.compute", "inputs": {"table": "read.table"},
                 "params": {"column": "rel_height", "formula": 'height_cm / mean(height_cm[treatment == "control"])',
                            "description": "Height relative to the control mean"}},
                {"id": "sum", "type": "table.summarize", "inputs": {"table": "rel.table"},
                 "params": {"by": ["treatment"], "values": ["rel_height"], "stats": ["n", "mean", "sd", "sem"]}},
                {"id": "test", "type": "stats.compare_groups", "inputs": {"table": "rel.table"},
                 "params": {"value": "height_cm", "group": "treatment", "test": "anova"}}],
            "outputs": {"summary": {"from": "sum.table", "database": True}, "tests": {"from": "test.results"}}}))
        assert made["saved"], made
        broken = value(await s.call_tool("method_patch", {"method": "plant-growth", "change": "broken", "ops": [
            {"op": "set", "step": "sum", "inputs": {"table": "missing.table"}}]}))
        assert not broken["saved"] and broken["problems"]
        patched = value(await s.call_tool("method_patch", {"method": "plant-growth", "change": "Per light too", "ops": [
            {"op": "set", "step": "sum", "params": {"by": ["treatment", "light"]}}]}))
        assert patched["saved"] and patched["version"] == 2 and any("light" in x for x in patched["diff"])
        exp = value(await s.call_tool("experiment_create", {"title": "Plants", "method": "plant-growth",
                                                             "files": {"data": str(SAMPLE)}}))
        run = value(await s.call_tool("run", {"experiment": exp["experiment"]}))
        assert run["status"] == "done", run
        summ = value(await s.call_tool("result_table", {"experiment": exp["experiment"], "table": "summary"}))
        assert summ["total"] == 6
        ctrl = [r for r in summ["rows"] if r["treatment"] == "control"]
        assert ctrl and all(0.5 < r["mean"] < 1.5 for r in ctrl)
        tests = value(await s.call_tool("result_table", {"experiment": exp["experiment"], "table": "tests"}))
        assert tests["rows"][0]["test"].startswith("one-way ANOVA")
        ex = value(await s.call_tool("explain", {"experiment": exp["experiment"], "step": "rel"}))
        assert r"\overline{\mathrm{height\_cm}}" in ex["steps"][0]["formulas"][0]["latex"]
        assert ex["steps"][0]["run"]["examples"]
        back = value(await s.call_tool("method_revert", {"method": "plant-growth", "version": 1}))
        assert back["version"] == 3

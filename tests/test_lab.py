"""Methods, experiments and runs through the MCP server as layr starts it: the example GC experiment end to end,
AI drafts and the user's acceptance, the comparison on the reference experiment, a new kind of analysis written
from scratch. Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from test_server import SAMPLE, error_text, server, value

USER = {"layr/caller": {"kind": "panel", "plugin": "research"}}  # the Research panel: the user herself
AGENT = {"layr/caller": {"kind": "agent", "plugin": "claude", "label": "t_test"}}


async def wait_for(s, tool: str, args: dict, ready, timeout: float = 120) -> dict:
    for _ in range(int(timeout * 4)):
        got = value(await s.call_tool(tool, args))
        if ready(got):
            return got
        await asyncio.sleep(0.25)
    raise AssertionError(f"{tool} never got ready: {got}")


async def test_sample_experiment_end_to_end(tmp_path: Path):
    async with server(tmp_path) as s:
        tools = {t.name for t in (await s.list_tools()).tools}
        assert {"method_get", "method_write", "method_check", "draft_get", "draft_decide", "toolbox_docs",
                "compare_runs", "run", "run_stop", "explain", "explain_function", "provenance", "curve",
                "correction_propose", "export", "method_export", "notebook", "examples"} <= tools
        assert not {"step_catalog", "method_patch", "chromatogram", "database_query", "database_tables",
                    "gcassay_settings"} & tools
        manifest = json.loads((Path(__file__).resolve().parent.parent / "layr.json").read_text("utf-8"))
        grouped = [t for g in manifest["access"]["groups"] for t in g["tools"]]
        assert sorted(grouped) == sorted(tools), set(grouped) ^ tools  # every tool in exactly one group
        got = value(await s.call_tool("method_get", {"method": "gc-activity-assay"}))
        assert got["check"] == [] and got["summary"]["recipe"] == "activity_assay"
        kinds = {f["name"]: f["kind"] for f in got["summary"]["outline"]}
        assert kinds["specific_activity"] == "formula" and kinds["initial_rates"] == "step"
        assert {s["id"] for s in got["summary"]["settings"]} >= {"rt_tolerance_min", "rate_window_min",
                                                                 "biomass_gcdw_per_l", "sample_patterns", "compounds"}

        exp = value(await s.call_tool("lab_sample", {"which": "gc"}))
        eid = exp["experiment"]
        run = value(await s.call_tool("run", {"experiment": eid}))
        assert run["status"] == "done", run
        assert run["steps"][0]["function"] == "read_gaml" and len(run["steps"]) == 24
        acts = value(await s.call_tool("result_table", {"experiment": eid, "table": "activities"}))
        by = {r["compound"]: r for r in acts["rows"]}
        assert by["SUB"]["activity_U_per_gCDW"] < -20 and by["PROD"]["activity_U_per_gCDW"] > 15
        assert acts["run"] == run["run"]
        step_table = value(await s.call_tool("result_table", {"experiment": eid, "table": "step:area_ratios"}))
        assert "ratio" in step_table["columns"]

        ex = value(await s.call_tool("explain", {"experiment": eid}))
        steps = {x["function"]: x for x in ex["steps"]}
        rates = steps["initial_rates"]
        assert rates["sentence"].startswith("Fit a straight line")
        used = {u["function"]: u for u in rates["used"]}
        assert r"\frac" in used["specific_activity"]["formulas"][0]["latex"]
        assert used["specific_activity"]["example"]["latex"][0].count("=") == 2  # formula = numbers = result
        general = value(await s.call_tool("explain", {"method": "gc-activity-assay"}))
        assert general["run"] is None and general["steps"][0]["function"] == "sample_names"

        fn = value(await s.call_tool("explain_function", {"function": "concentration_mM", "experiment": eid}))
        assert fn["kind"] == "formula" and fn["count"] == 1 and fn["example"]["column_length"] > 10
        assert fn["example"]["latex"] and r"\mathrm{concentration}_{[\mathrm{mM}]}" in fn["formulas"][0]["latex"]
        tb = value(await s.call_tool("explain_function", {"function": "fit_line", "experiment": eid}))
        assert tb["kind"] == "toolbox" and tb["count"] == 4 and tb["formulas"]

        prov = value(await s.call_tool("provenance", {"experiment": eid, "table": "concentrations",
                                                      "where": {"injection": 20, "compound": "SUB"},
                                                      "column": "conc_mM"}))
        chain = [c["function"] for c in prov["chain"]]
        assert chain[0] == "concentration_mM" and "area_ratio" in chain and "integrate_peak" in chain, chain
        assert prov["chain"][0]["latex_numbers"]
        act = value(await s.call_tool("provenance", {"experiment": eid, "table": "activities",
                                                     "where": {"compound": "SUB"}, "column": "activity_U_per_gCDW"}))
        assert [c["function"] for c in act["chain"]][:2] == ["specific_activity", "fit_line"]

        nd = value(await s.call_tool("result_table", {"experiment": eid, "table": "concentrations",
                                                      "where": {"conc_flag": "n.d."}, "limit": 1}))["rows"][0]
        where = {"injection": nd["injection"], "compound": nd["compound"]}
        zero = value(await s.call_tool("provenance", {"experiment": eid, "table": "concentrations", "column": "conc_mM",
                                                      "where": where}))
        assert zero["value"] == 0 and zero["chain"] == [] and "`quantify`" in zero["note"]  # set to 0 there
        mean = value(await s.call_tool("provenance", {"experiment": eid, "table": "time_course", "column": "mean",
                                                      "where": {"compound": "SUB", "time_min": 0}}))
        assert mean["chain"] == [] and "`time_course`" in mean["note"]  # a mean worked out inside the step itself
        docs = value(await s.call_tool("toolbox_docs", {}))
        assert {"fit_line", "read_gaml", "integrate_peak"} <= {f["name"] for f in docs["functions"]}
        imports = {f["name"]: f["import"] for f in docs["functions"]}
        assert imports["fit_line"] == "toolbox" and imports["read_gaml"] == "toolbox.gc"
        assert [g["module"] for g in docs["groups"]][0] == "toolbox" and "toolbox.gc" in [g["module"] for g in
                                                                                         docs["groups"]]
        assert {r["function"] for r in docs["readers"]} >= {"read_table", "read_gaml"}

        # the raw file's curves come from the curve reader its add-on registered for .gaml
        trace = value(await s.call_tool("curve", {"experiment": eid, "item": 20, "file": "gaml_file", "start": 3.4,
                                                  "end": 3.9, "points": 400}))
        assert 0 < len(trace["x"]) <= 400 and min(trace["x"]) >= 3.4 and trace["name"]

        # an agent only proposes; the decision makes it apply
        prop = value(await s.call_tool("correction_propose", {
            "experiment": eid, "kind": "exclude", "fields": {"injection": 20, "compound": "SUB"},
            "reason": "Test: pipetting error noted in the lab book"}))
        assert prop["correction"]["status"] == "proposed"
        value(await s.call_tool("run", {"experiment": eid}))
        pk = value(await s.call_tool("result_table", {"experiment": eid, "table": "peaks",
                                                      "where": {"injection": 20, "compound": "SUB"}}))
        assert pk["rows"][0]["status"] == "ok"
        value(await s.call_tool("correction_decide", {"experiment": eid, "correction": prop["correction"]["id"],
                                                      "decision": "approve"}))
        after = value(await s.call_tool("run", {"experiment": eid}))
        pk = value(await s.call_tool("result_table", {"experiment": eid, "table": "peaks",
                                                      "where": {"injection": 20, "compound": "SUB"}}))
        assert pk["rows"][0]["status"] == "excluded"
        moved = value(await s.call_tool("compare_runs", {"before": run["run"], "after": after["run"]}))
        assert moved["numbers_changed"] > 0 and any(t["table"] == "concentrations" for t in moved["tables"])
        bad = await s.call_tool("correction_propose", {"experiment": eid, "kind": "exclude",
                                                       "fields": {"injection": 3}, "reason": "  "})
        assert "reason" in error_text(bad)

        for fmt in ("html", "xlsx", "docx"):
            out = value(await s.call_tool("export", {"experiment": eid, "format": fmt}))
            assert Path(out["path"]).stat().st_size > 5000, fmt
        out = value(await s.call_tool("export", {"experiment": eid, "format": "html"}))
        html = Path(out["path"]).read_text("utf-8")
        assert "How every number was calculated" in html and "Test: pipetting error" in html
        assert "def activity_assay(" in html and "Specific activity in U/gCDW" in html

        nb = value(await s.call_tool("notebook", {"experiment": eid}))
        assert Path(nb["notebook"]).exists() and (Path(nb["folder"]) / "tables" / "activities.csv").exists()


async def test_drafts_and_acceptance(tmp_path: Path):
    """An agent's change is a draft: it doesn't count, comes back with the checker's warnings and the numbers that
    moved on the reference experiment, and only the user accepts it. The user's own saves count at once."""
    async with server(tmp_path) as s:
        eid = value(await s.call_tool("lab_sample", {"which": "gc"}))["experiment"]
        assert {m["id"]: m for m in value(await s.call_tool("methods", {}))}["gc-activity-assay"]["reference"] == eid
        first = value(await s.call_tool("run", {"experiment": eid}))
        assert first["status"] == "done"
        code = ('def specific_activity(rate_mM_per_min, biomass_gCDW_per_L):\n'
                '    return rate_mM_per_min * 1000 / biomass_gCDW_per_L / 2\n')
        wrote = value(await s.call_tool("method_write", {"method": "gc-activity-assay", "function": "specific_activity",
                                                         "code": code, "change": "Halve the activities"},
                                        meta=AGENT))
        assert wrote["draft"] and not wrote["saved"]
        assert any("no docstring" in m["message"] for m in wrote["messages"])
        assert any(line.startswith("+") and "/ 2" in line for line in wrote["diff"])
        versions = value(await s.call_tool("method_versions", {"method": "gc-activity-assay"}))
        assert [v["version"] for v in versions] == [1]
        d = await wait_for(s, "draft_get", {"method": "gc-activity-assay"},
                           lambda g: (g["draft"].get("comparison") or {}).get("status") in ("done", "failed"))
        comp = d["draft"]["comparison"]
        assert comp["status"] == "done", comp
        act = next(t for t in comp["tables"] if t["table"] == "activities")
        assert act["cells_changed"] >= 2
        change = next(c for c in act["changes"] if c["column"] == "activity_U_per_gCDW")
        assert abs(change["after"] - change["before"] / 2) < 1e-9
        # the real experiment didn't move: its latest run is still the one before the draft
        exp = value(await s.call_tool("experiment_get", {"experiment": eid}))
        assert exp["last_run"]["id"] == first["run"]

        refused = await s.call_tool("draft_decide", {"method": "gc-activity-assay", "decision": "accept"}, meta=AGENT)
        assert "Only the user" in error_text(refused)
        accepted = value(await s.call_tool("draft_decide", {"method": "gc-activity-assay", "decision": "accept"},
                                           meta=USER))
        assert accepted["version"] == 2
        assert value(await s.call_tool("draft_get", {"method": "gc-activity-assay"}))["draft"] is None
        diff = value(await s.call_tool("method_diff", {"method": "gc-activity-assay", "from_version": 1}))
        assert any("/ 2" in line for line in diff)

        # the user's own save is a version at once; errors block saving
        broken = value(await s.call_tool("method_write", {"method": "gc-activity-assay", "source": "def f(:\n",
                                                          "change": "oops"}, meta=USER))
        assert not broken["saved"] and broken["messages"][0]["level"] == "error"
        back = value(await s.call_tool("method_revert", {"method": "gc-activity-assay", "version": 1}, meta=USER))
        assert back["saved"] and back["version"] == 3
        v3 = await wait_for(s, "method_versions", {"method": "gc-activity-assay"},
                            lambda vs: (vs[-1].get("comparison") or {}).get("status") in ("done", "failed"))
        assert v3[-1]["comparison"]["numbers_changed"] > 0  # back from halved activities


async def test_new_kind_of_analysis(tmp_path: Path):
    """What an agent does for a new kind of analysis: a new method (a draft), the user accepts it, an experiment,
    a run; and a run error names the method's own line in plain words."""
    async with server(tmp_path) as s:
        made = value(await s.call_tool("method_create", {"method": "plant-growth", "name": "Plant growth",
                                                         "description": "Heights relative to the control."},
                                       meta=AGENT))
        assert made["draft"] and made["messages"] == []
        listed = {m["id"]: m for m in value(await s.call_tool("methods", {}))}
        assert listed["plant-growth"]["draft_only"]
        value(await s.call_tool("draft_decide", {"method": "plant-growth", "decision": "accept"}, meta=USER))
        exp = value(await s.call_tool("experiment_create", {
            "title": "Plants", "method": "plant-growth", "files": {"table_file": str(SAMPLE)},
            "params": {"value_column": "height_cm", "group_column": "treatment"}}))
        run = value(await s.call_tool("run", {"experiment": exp["experiment"]}))
        assert run["status"] == "done", run
        rel = value(await s.call_tool("result_table", {"experiment": exp["experiment"], "table": "relative"}))
        ctrl = [r["relative"] for r in rel["rows"] if r["treatment"] == "control"]
        assert abs(sum(ctrl) / len(ctrl) - 1) < 1e-9

        value(await s.call_tool("experiment_update", {"experiment": exp["experiment"],
                                                      "params": {"value_column": "height"}}))
        failed = value(await s.call_tool("run", {"experiment": exp["experiment"]}))
        assert failed["status"] == "failed"
        err = failed["error"]
        assert err["function"] == "relative_to_control" and err["line"] > 5
        assert "has no column 'height'" in err["message"] and "height_cm" in err["message"]


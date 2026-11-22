"""Research 0.4 end to end through the MCP server as layr starts it: the sample experiment run and drawn as a flow
with a value on every wire, the inside of a step, a stored table, a changed setting rerun with remembered steps, a
ready-made step added in the flow and tried in a trial run, the palette and code help. Run from plugins/research:
uv run pytest tests/test_flow_server.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

from test_lab import AGENT, USER
from test_server import server, value

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import flow  # noqa: E402


def wires_without_value(g: dict) -> list:
    return [(n["id"], o) for n in g["nodes"] for o in n["outs"] if n["values"].get(o) is None]


async def test_flow_end_to_end(tmp_path: Path):
    async with server(tmp_path) as s:
        exp = value(await s.call_tool("lab_sample", {"which": "gc"}))
        eid = exp["experiment"]
        first = value(await s.call_tool("run", {"experiment": eid}))
        assert first["status"] == "done", first

        g = value(await s.call_tool("flow_get", {"experiment": eid}))
        assert len(g["nodes"]) == 25 and g["run"]["id"] == first["run"] and g["stale"] is False
        assert not wires_without_value(g), wires_without_value(g)
        assert len(g["quality"]) == 10 and g["quality"][-1] == "flags"
        assert g["experiment"]["id"] == eid and g["method"]["id"] == "gc-activity-assay"

        inside = value(await s.call_tool("function_inside", {"experiment": eid, "function": "area_ratios"}))
        assert inside["statements"] and inside["count"] >= 1 and inside["call"]["args"]
        peaks = next(n for n in g["nodes"] if n["id"] == "peaks")["values"]["peaks"]
        rows = value(await s.call_tool("value_get", {"run": first["run"], "file": peaks["file"], "limit": 5}))
        assert rows["total"] == peaks["rows"] and len(rows["rows"]) == 5 and rows["columns"] == peaks["columns"]

        # a changed setting: only the steps that use it are calculated again, the numbers equal a fresh run's
        value(await s.call_tool("experiment_update", {"experiment": eid, "params": {"biomass_gcdw_per_l": 0.6}},
                                meta=USER))
        rerun = value(await s.call_tool("run", {"experiment": eid}))
        statuses = [x["status"] for x in rerun["steps"]]
        assert rerun["status"] == "done" and statuses.count("cached") >= len(statuses) // 2, statuses
        assert "done" in statuses  # the steps that read the biomass
        fresh = value(await s.call_tool("run", {"experiment": eid, "fresh": True}))
        assert fresh["fresh"] is True and "cached" not in [x["status"] for x in fresh["steps"]]
        a = value(await s.call_tool("result_table", {"run": rerun["run"], "table": "activities"}))
        b = value(await s.call_tool("result_table", {"run": fresh["run"], "table": "activities"}))
        assert a["rows"] == b["rows"]
        g2 = value(await s.call_tool("flow_get", {"experiment": eid, "run": rerun["run"]}))
        assert not wires_without_value(g2) and "cached" in {n["status"] for n in g2["nodes"]}

        # a ready-made step added in the flow, then a trial run of that unsaved source
        palette = value(await s.call_tool("blocks", {}))["blocks"]
        assert "pick_rows" in {x["function"] for x in palette}
        edit = value(await s.call_tool("flow_edit", {"experiment": eid, "ops": [
            {"op": "add_step", "function": "pick_rows", "out": "sub_rows",
             "args": {"table": "concentrations", "column": {"value": "compound"}, "equals": {"value": "SUB"}}}]}))
        assert edit["ok"], edit["check"]
        assert "sub_rows = pick_rows(concentrations" in edit["source"]
        trial = value(await s.call_tool("run", {"experiment": eid, "source": edit["source"],
                                                "settings": {"biomass_gcdw_per_l": 0.7}}))
        assert trial["status"] == "done" and trial["trial"] is True, trial
        assert trial["settings"]["biomass_gcdw_per_l"] == 0.7
        after = value(await s.call_tool("experiment_get", {"experiment": eid}))
        assert after["last_run"]["id"] == fresh["run"]  # a trial isn't the latest run
        assert after["params"]["biomass_gcdw_per_l"] == 0.6  # nor are its settings saved
        tg = value(await s.call_tool("flow_get", {"source": edit["source"], "run": trial["run"]}))
        new = next(n for n in tg["nodes"] if n["id"] == "sub_rows")
        assert new["status"] == "done" and new["values"]["sub_rows"]["kind"] == "table"
        assert new["values"]["sub_rows"]["rows"] > 0 and tg["stale"] is False
        ti = value(await s.call_tool("function_inside", {"run": trial["run"], "function": "activity_assay"}))
        assert ti["statements"]
        runs = value(await s.call_tool("runs", {"experiment": eid}))
        assert trial["run"] not in {r["id"] for r in runs}

        # an AI draft of which she keeps only part: accepted with the merged text, the draft is gone
        base = value(await s.call_tool("method_get", {"method": "gc-activity-assay"}))
        two = flow.edit_all(base["source"], [{"op": "set_default", "setting": "min_r2", "value": 0.99},
                                             {"op": "set_default", "setting": "cal_max_conc_mM", "value": 3.0}])
        wrote = value(await s.call_tool("method_write", {"method": "gc-activity-assay", "change": "Two defaults",
                                                         "source": two}, meta=AGENT))
        assert wrote["draft"] is True
        part = flow.edit(base["source"], {"op": "set_default", "setting": "min_r2", "value": 0.99})
        got = value(await s.call_tool("draft_decide", {"method": "gc-activity-assay", "decision": "accept",
                                                       "source": part}, meta=USER))
        assert got["accepted"] is True and got["merged"] is True and got["version"] == base["version"] + 1
        assert value(await s.call_tool("draft_get", {"method": "gc-activity-assay"}))["draft"] is None
        now = value(await s.call_tool("method_get", {"method": "gc-activity-assay"}))
        assert now["source"] == part and now["versions"][-1]["by"].startswith("you (Research panel), with part")

        done = value(await s.call_tool("code_complete", {"source": edit["source"], "line": 1, "column": 0}))
        assert "items" in done
        src = "import pandas as pd\n\npd.\n"
        done = value(await s.call_tool("code_complete", {"source": src, "line": 3, "column": 3}))
        assert any(i["label"] == "DataFrame" for i in done["items"])


async def test_a_step_that_gives_a_number_in_a_trial_run(tmp_path: Path):
    """A new step that returns a number (not a table), tried in a trial run: the run reports it, and the trial's
    calls are explained from the trial itself (explain_function and function_inside count the same calls)."""
    async with server(tmp_path) as s:
        eid = value(await s.call_tool("lab_sample", {"which": "gc"}))["experiment"]
        value(await s.call_tool("run", {"experiment": eid}))
        edit = value(await s.call_tool("flow_edit", {"experiment": eid, "ops": [
            {"op": "new_function", "name": "count_rows", "params": ["table"], "sentence": "How many rows.",
             "body": "return len(table)"},
            {"op": "add_step", "function": "count_rows", "args": {"table": "concentrations"}, "out": "n_rows"}]}))
        assert edit["ok"], edit["check"]
        trial = value(await s.call_tool("run", {"experiment": eid, "source": edit["source"]}))
        assert trial["status"] == "done", trial.get("error")
        step = next(x for x in trial["steps"] if x["function"] == "count_rows")
        assert step["status"] == "done" and step["table"] is None
        for fn in ("count_rows", "area_ratios"):
            explained = value(await s.call_tool("explain_function", {"function": fn, "run": trial["run"],
                                                                     "source": edit["source"]}))
            inside = value(await s.call_tool("function_inside", {"function": fn, "run": trial["run"]}))
            assert explained["count"] == inside["count"] == 1, fn
        tables = value(await s.call_tool("explain", {"run": trial["run"]}))
        assert tables["steps"]


async def test_with_a_hub_a_remembered_run_comes_back_at_once_and_no_tool_waits(tmp_path: Path):
    """Each event once opened its own connection to the hub, a quarter of a second of the event loop each: a run of
    25 steps kept every tool waiting for 7 s. Now one connection stays open; the run's events still arrive."""
    import asyncio
    import json
    import socket
    import subprocess
    import time

    s0 = socket.socket()
    s0.bind(("127.0.0.1", 0))
    port = s0.getsockname()[1]
    s0.close()
    events = tmp_path / "events.jsonl"
    hub = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fake_hub.py"), str(port), str(events)])
    try:
        for _ in range(100):  # until the stand-in hub listens
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        async with server(tmp_path / "home", f"http://127.0.0.1:{port}/mcp") as s:
            eid = value(await s.call_tool("lab_sample", {"which": "gc"}))["experiment"]
            src = value(await s.call_tool("method_get", {"method": "gc-activity-assay"}))["source"]
            value(await s.call_tool("run", {"experiment": eid}))
            for args in ({"experiment": eid, "source": src}, {"experiment": eid}):
                t0 = time.perf_counter()
                running = asyncio.ensure_future(s.call_tool("run", args))
                await asyncio.sleep(0.1)
                t1 = time.perf_counter()
                value(await s.call_tool("method_check", {"source": src}))
                waited = time.perf_counter() - t1
                r = value(await running)
                took = time.perf_counter() - t0
                assert r["cache"]["misses"] == 0, r["cache"]
                assert took < 2.5 and waited < 1.0, (args, took, waited)
            for _ in range(50):
                got = [json.loads(x) for x in events.read_text("utf-8").splitlines()] if events.exists() else []
                if sum(1 for e in got if e["data"].get("finished")) >= 3:
                    break
                await asyncio.sleep(0.1)
            finished = [e["data"] for e in got if e["data"].get("finished")]
            assert len(finished) == 3 and [bool(e.get("trial")) for e in finished] == [False, True, False]
    finally:
        hub.kill()
        hub.wait()


async def test_every_example_is_listed_made_and_runs(tmp_path: Path):
    """The examples come from their registry (lab/examples/): each one is made through the server and its run is
    done; an unknown example is refused with the ones there are."""
    from test_server import error_text

    async with server(tmp_path) as s:
        listed = value(await s.call_tool("examples", {}))["examples"]
        assert [e["id"] for e in listed] == ["survey", "gc", "compare"]
        assert all(e["title"] and e["description"] and e["method"] for e in listed)
        for e in listed:
            made = value(await s.call_tool("lab_sample", {"which": e["id"]}))
            assert made["method"] == e["method"]
            run = value(await s.call_tool("run", {"experiment": made["experiment"]}))
            assert run["status"] == "done", (e["id"], run.get("error"))
        unknown = await s.call_tool("lab_sample", {"which": "nope"})
        assert "Examples: survey, gc, compare" in error_text(unknown)

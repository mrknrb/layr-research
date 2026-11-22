"""Experiments that read other experiments: a recipe input marked `[result]` gets the tables other experiments made,
stacked into one; picks are checked (no loop of any length), the run records what it read, a reader is out of date
when a source's numbers change (not after an identical rerun), remembered steps follow the content, deleting a source
is refused or forced, the flow draws the input with its table, provenance ends at the source, the exported folder
runs, and the comparison example works through the server. Run from plugins/research:
uv run pytest tests/test_sources.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import checker, explain, flow, methodfile, sources, standalone  # noqa: E402
from lab.errors import LabError  # noqa: E402
from lab.examples.survey import write_survey  # noqa: E402

READER = '''"""Read results

Doubles the scores of other experiments.
"""

import pandas as pd


def double(value):
    """Twice the value."""
    return value * 2


def doubled(results):
    """Every score doubled."""
    out = results.copy()
    out["doubled"] = double(out["score"])
    return out


def count_rows(results):
    """How many rows each experiment gave."""
    rows = []
    for name, part in results.groupby("experiment", sort=False):
        rows.append({"experiment": name, "rows": len(part)})
    return pd.DataFrame(rows)


def read_results(results):
    """Read results.

    Inputs:
        results: Scores of other experiments [result: scores]

    Returns:
        doubled: Doubled scores
        counts: Rows per experiment
    """
    doubled_scores = doubled(results)
    counts = count_rows(results)
    return {"doubled": doubled_scores, "counts": counts}
'''

ANY = READER.replace("[result: scores]", "[result]")


# ---------------------------------------------------------------------------------------------------- declaring


def test_a_result_input_is_declared_by_its_bracket():
    m = methodfile.parse(READER)
    assert methodfile.result_inputs(m) == {"results": "scores"}
    assert methodfile.inputs(m) == [{"id": "results", "kind": "result", "label": "Scores of other experiments",
                                     "result": "scores", "optional": False}]
    assert methodfile.settings(m) == []  # an input, not a setting, whatever its name
    assert methodfile.result_inputs(methodfile.parse(ANY)) == {"results": None}
    assert checker.check(READER)["messages"] == []
    summary = checker.check(READER)["summary"]
    assert summary["inputs"][0]["kind"] == "result"


def test_the_checker_explains_mistakes_in_plain_words():
    bad = READER.replace("results: Scores of other experiments [result: scores]",
                         "results: Scores [reslut]\n        other_file: A file [result]\n"
                         "        named: Named [result: my scores]\n        loose: Loose")
    bad = bad.replace("def read_results(results):", "def read_results(results, other_file, named, loose=1):")
    text = " ".join(m["message"] for m in checker.check(bad)["messages"])
    assert "`[reslut]`, which is neither file types" in text
    assert "a name ending in _file always means a raw file: rename it (e.g. `other`)" in text
    assert "one word like `scores`" in text
    assert "`loose`, but it is neither a raw file" in text
    lines = {m["line"] for m in checker.check(bad)["messages"]}
    assert len(lines) == 4  # each at its own docstring line
    missing = READER.replace("        results: Scores", "        ghost: G [result]\n        results: Scores")
    assert "has no parameter `ghost`" in checker.check(missing)["messages"][0]["message"]


# ---------------------------------------------------------------------------------------------------- the store


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """A store with the reader methods and three survey experiments (two waves and a third), all run once."""
    from lab.runner import Pool, Runner
    from lab.store import Store, new_id, now

    root = tmp_path_factory.mktemp("sources")
    store = Store(root / "lab")
    store.save_method("reader", READER, "test", "made")
    store.save_method("any-reader", ANY, "test", "made")
    runner = Runner(store, Pool())

    def survey(title: str, seed: int, shift: float = 0.0) -> dict:
        path = root / f"{title}.csv"
        write_survey(path, seed=seed, n=60, shift=shift)
        e = {"id": new_id("e"), "title": title, "method": "survey-scores",
             "inputs": {"answers_file": store.add_file(path)}, "params": {}, "corrections": [], "created": now()}
        store.save_experiment(e)
        run = runner.run(e, store.get_method("survey-scores"))
        assert run["status"] == "done", run.get("error")
        return store.get_experiment(e["id"])

    def reader(title: str, method: str = "reader", picks: dict | None = None) -> dict:
        e = {"id": new_id("e"), "title": title, "method": method, "inputs": {}, "params": {}, "corrections": [],
             "created": now()}
        inputs = methodfile.inputs(methodfile.parse(store.get_method(method)["source"]))
        e["sources"] = sources.check(store, e, inputs, picks)
        store.save_experiment(e)
        return e

    def run(e: dict, **kw) -> dict:
        e = store.get_experiment(e["id"])
        return runner.run(e, store.get_method(e["method"]), **kw)

    out = {"store": store, "runner": runner, "reader": reader, "run": run,
           "a": survey("Survey", 3), "b": survey("Survey", 4, 0.5), "c": survey("Third", 5)}
    try:
        yield out
    finally:
        runner.pool.close()


def inputs_of(store, method: str) -> list[dict]:
    return methodfile.inputs(methodfile.parse(store.get_method(method)["source"]))


def test_picks_are_checked_in_plain_words(lab):
    store, a = lab["store"], lab["a"]
    e = lab["reader"]("Checks")
    ins = inputs_of(store, "reader")

    def refused(given: dict) -> str:
        with pytest.raises(LabError) as err:
            sources.check(store, e, ins, given)
        return str(err.value)

    assert "there is no experiment e00000000" in refused({"results": [{"experiment": "e00000000"}]})
    assert "can't read its own results" in refused({"results": [{"experiment": e["id"]}]})
    assert "has no table `nope`. Its tables: scores, reliability" in refused(
        {"results": [{"experiment": a["id"], "result": "nope"}]})
    assert "returns `alpha` as a single value, not a table" in refused(
        {"results": [{"experiment": a["id"], "result": "alpha"}]})
    assert "is picked twice" in refused({"results": [{"experiment": a["id"]}, {"experiment": a["id"]}]})
    assert "no input `answers` that takes results" in refused({"answers": [{"experiment": a["id"]}]})
    # the suggested result is used when none is given; a step's table can be picked too
    got = sources.check(store, e, ins, {"results": [a["id"], {"experiment": lab["b"]["id"],
                                                               "result": "step:scale_scores"}]})
    assert got == {"results": [{"experiment": a["id"], "result": "scores"},
                               {"experiment": lab["b"]["id"], "result": "step:scale_scores"}]}
    assert sources.check(store, {**e, "sources": got}, ins, {"results": None}) == {}


def test_no_loops_of_any_length(lab):
    store = lab["store"]
    ins = inputs_of(store, "any-reader")
    x = lab["reader"]("X", "any-reader")
    y = lab["reader"]("Y", "any-reader", {"results": [{"experiment": x["id"], "result": "doubled"}]})
    with pytest.raises(LabError, match=r"That would make a loop: 'X' reads 'Y', 'Y' reads 'X'"):
        sources.check(store, x, ins, {"results": [{"experiment": y["id"], "result": "doubled"}]})
    z = lab["reader"]("Z", "any-reader", {"results": [{"experiment": y["id"], "result": "doubled"}]})
    with pytest.raises(LabError, match=r"loop: 'X' reads 'Z', 'Z' reads 'Y', 'Y' reads 'X'"):
        sources.check(store, x, ins, {"results": [{"experiment": z["id"], "result": "doubled"}]})


# ---------------------------------------------------------------------------------------------------- running


def test_one_source_is_stacked_with_the_two_columns(lab):
    store = lab["store"]
    e = lab["reader"]("One", picks={"results": [{"experiment": lab["c"]["id"]}]})
    run = lab["run"](e)
    assert run["status"] == "done", run.get("error")
    rec = run["sources"]["results"]
    assert rec["columns"][:2] == ["experiment", "experiment_id"] and rec["file"] == "sources/results.csv"
    src = rec["from"][0]
    assert src["experiment"] == lab["c"]["id"] and src["title"] == "Third" and src["label"] == "Third"
    assert src["run"] == lab["c"]["last_run"]["id"] and src["result"] == "scores" and len(src["fingerprint"]) == 16
    scores = store.run_table(lab["c"]["last_run"]["id"], "scores")
    assert src["rows"] == rec["rows"] == len(scores)
    doubled = store.run_table(run["id"], "doubled")
    assert list(doubled.columns[:2]) == ["experiment", "experiment_id"] and set(doubled.experiment) == {"Third"}
    assert doubled.doubled.dropna().equals((doubled.score * 2).dropna())


def test_three_sources_with_other_columns_and_the_same_title(lab):
    store = lab["store"]
    picks = {"results": [{"experiment": lab["a"]["id"]}, {"experiment": lab["b"]["id"]},
                         {"experiment": lab["c"]["id"], "result": "group_means"}]}
    e = lab["reader"]("Three", picks=picks)
    run = lab["run"](e)
    assert run["status"] == "done", run.get("error")
    counts = store.run_table(run["id"], "counts")
    assert list(counts.experiment) == ["Survey (1)", "Survey (2)", "Third"]  # equal titles told apart
    stacked = pd.read_csv(store.run_dir(run["id"]) / run["sources"]["results"]["file"])
    assert {"score", "respondent", "mean_score", "sd"} <= set(stacked.columns)  # every table's columns
    third = stacked[stacked.experiment == "Third"]
    assert third.score.isna().all() and third.mean_score.notna().all()
    assert [f["label"] for f in run["sources"]["results"]["from"]] == ["Survey (1)", "Survey (2)", "Third"]


def test_out_of_date_and_remembered_steps(lab):
    store, a = lab["store"], lab["a"]
    e = lab["reader"]("Watcher", picks={"results": [{"experiment": a["id"]}]})
    first = lab["run"](e)
    assert first["status"] == "done"

    def outdated() -> list:
        everyone = {x["id"]: x for x in store.list_experiments()}
        return sources.outdated(store, everyone[e["id"]], everyone)

    assert outdated() == []
    again = lab["run"](e)  # nothing changed: the steps come back remembered
    assert {s["status"] for s in again["steps"]} == {"cached"}, again["steps"]
    # the source run again with the same data: the same numbers, so not out of date
    assert lab["run"](a)["status"] == "done"
    assert outdated() == []
    # the source's numbers change
    store.save_experiment({**store.get_experiment(a["id"]), "params": {"min_answers": 6}})
    assert lab["run"](a)["status"] == "done"
    got = outdated()
    assert len(got) == 1 and got[0]["input"] == "results" and got[0]["experiment"] == a["id"]
    assert got[0]["title"] == "Survey" and "newer run" in got[0]["reason"] and "other numbers in scores" in \
        got[0]["reason"]
    after = lab["run"](e)  # recalculated from the new content
    assert {s["status"] for s in after["steps"]} == {"done"}
    assert after["sources"]["results"]["from"][0]["run"] == store.get_experiment(a["id"])["last_run"]["id"]
    assert outdated() == []
    # a changed pick is out of date too
    store.save_experiment({**store.get_experiment(e["id"]), "sources": {"results": [
        {"experiment": a["id"], "result": "scores"}, {"experiment": lab["c"]["id"], "result": "scores"}]}})
    assert [x["reason"].split(" ")[0] for x in outdated()] == ["Picked"]
    # a trial run reads them as well, and stays apart
    trial = lab["run"](e, trial=True)
    assert trial["status"] == "done" and len(trial["sources"]["results"]["from"]) == 2
    assert outdated()  # still: the trial isn't the latest run


def test_a_source_that_cant_be_read_stops_the_run(lab):
    store = lab["store"]
    never = {"id": "e0000abcd", "title": "Never run", "method": "survey-scores", "inputs": {}, "params": {},
             "corrections": [], "created": "x"}
    store.save_experiment(never)
    e = lab["reader"]("Blocked", picks={"results": [{"experiment": never["id"]}]})
    with pytest.raises(LabError, match="'Never run' hasn't been run yet, so `results` has nothing to read: run "
                                       "'Never run' first"):
        lab["run"](e)
    empty = lab["reader"]("Nothing picked")
    with pytest.raises(LabError, match="has no results picked"):
        lab["run"](empty)


# ---------------------------------------------------------------------------------------------------- around the run


def test_flow_draws_the_input_and_edits_it(lab):
    store = lab["store"]
    e = lab["reader"]("Flow", picks={"results": [{"experiment": lab["a"]["id"]}, {"experiment": lab["c"]["id"]}]})
    run = lab["run"](e)
    with_source = {**run, "source": READER}
    g = flow.graph(READER, with_source, store.get_experiment(e["id"]))
    item = next(i for i in g["inputs"] if i["id"] == "results")
    assert item["kind"] == "result" and item["value"] == "2 experiments · scores" and item["result"] == "scores"
    assert item["table"]["rows"] == run["sources"]["results"]["rows"] and item["table"]["file"]
    # the panel shows the table it read, page by page, as any table of the run (value_get)
    from lab import values

    rows = values.read(store, run["id"], item["table"]["file"], offset=0, limit=5)
    assert rows["total"] == item["table"]["rows"] and rows["columns"][:2] == ["experiment", "experiment_id"]
    assert len(rows["rows"]) == 5
    assert item["used_by"] == ["doubled_scores", "counts"] and len(item["read"]) == 2
    port = next(p for p in g["nodes"][0]["ports"] if p["name"] == "results")
    assert port["source"] == "input" and port["type"] == "table" and port["value"]["kind"] == "table"
    assert port["columns_read"] == ["score"] and port["missing"] == []

    added = flow.edit(READER, {"op": "add_input", "name": "earlier", "kind": "result", "result": "counts",
                               "label": "Earlier counts"})
    assert "earlier: Earlier counts [result: counts]" in added
    assert "def read_results(results, earlier):" in added
    assert methodfile.result_inputs(methodfile.parse(added)) == {"results": "scores", "earlier": "counts"}
    assert checker.check(added)["ok"]
    removed = flow.edit(added, {"op": "remove_input", "input": "earlier"})
    assert removed == READER
    with pytest.raises(LabError, match="give a result input another name"):
        flow.edit(READER, {"op": "add_input", "name": "x_file", "kind": "result"})

    # a step that reads a column the delivered table doesn't have is marked
    other = READER.replace('out["score"]', 'out["points"]')
    g2 = flow.graph(other, {**run, "source": other}, store.get_experiment(e["id"]))
    port = next(p for p in g2["nodes"][0]["ports"] if p["name"] == "results")
    assert port["missing"] == ["points"]


def test_provenance_ends_at_the_source(lab):
    store, a = lab["store"], lab["a"]
    e = lab["reader"]("Where from", picks={"results": [{"experiment": a["id"]}]})
    run = lab["run"](e)
    doubled = store.run_table(run["id"], "doubled").dropna(subset=["score"])
    row = doubled[doubled.score % 1 != 0].iloc[0]
    prov = explain.provenance(store, run, "doubled", {"respondent": int(row.respondent)}, "doubled")
    assert prov["chain"][0]["function"] == "double"
    last = prov["chain"][-1]
    assert last["kind"] == "source" and last["experiment"] == a["id"] and last["result"] == "scores"
    assert last["run"] == store.get_experiment(a["id"])["last_run"]["id"] and last["column"] == "score"
    assert last["where"] == {"respondent": int(row.respondent)} and last["value"] == pytest.approx(row.score)
    assert "came in from the experiment 'Survey'" in last["sentence"]


def test_the_exported_folder_runs_with_the_table_it_read(lab, tmp_path: Path):
    store = lab["store"]
    e = lab["reader"]("Export", picks={"results": [{"experiment": lab["a"]["id"]}, {"experiment": lab["c"]["id"]}]})
    run = lab["run"](e)
    folder = standalone.export_folder(store, store.get_method("reader"), tmp_path / "export",
                                      store.get_experiment(e["id"]), run)
    info = json.loads((folder / "inputs.json").read_text("utf-8"))["results"]
    assert info["kind"] == "result" and info["name"] == "results.csv" and len(info["from"]) == 2
    assert (folder / "data" / "results.csv").is_file()
    out = subprocess.run([sys.executable, "-I", str(folder / "run.py"), "--out", str(tmp_path / "results")],
                         cwd=folder, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    pd.testing.assert_frame_equal(pd.read_csv(tmp_path / "results" / "doubled.csv"),
                                  store.run_table(run["id"], "doubled"))


def test_the_report_lists_what_it_read(lab):
    from lab import export

    store = lab["store"]
    e = lab["reader"]("Report", picks={"results": [{"experiment": lab["c"]["id"]}]})
    run = lab["run"](e)
    page = export.to_html(export.model(store, store.get_experiment(e["id"]), run))
    assert "Results of other experiments" in page and "Third" in page and lab["c"]["last_run"]["id"] in page
    about = pd.read_excel(export.write(store, store.get_experiment(e["id"]), run, "xlsx"), sheet_name="about")
    assert any(str(v).startswith("Third · scores (run ") for v in about["value"])


# ---------------------------------------------------------------------------------------------------- the server


async def test_the_comparison_example_through_the_server(tmp_path: Path):
    from test_lab import USER
    from test_server import error_text, server, value

    async with server(tmp_path) as s:
        listed = [x["id"] for x in value(await s.call_tool("examples", {}))["examples"]]
        assert "compare" in listed
        made = value(await s.call_tool("lab_sample", {"which": "compare"}))
        cid, (spring, autumn) = made["experiment"], made["made"]
        assert made["method"] == "compare-experiments"

        method = value(await s.call_tool("method_get", {"method": "compare-experiments"}))
        assert method["check"] == [] and method["summary"]["inputs"] == [
            {"id": "results", "kind": "result", "label": "The table to compare, from each experiment picked",
             "result": None, "optional": False}]

        listing = {x["experiment"]: x for x in value(await s.call_tool("experiments", {}))}
        assert "scores" in listing[spring]["results"] and "step:scale_scores" in listing[spring]["step_results"]
        assert listing[cid]["reads"] == [{"input": "results", "experiment": spring, "result": "scores"},
                                         {"input": "results", "experiment": autumn, "result": "scores"}]
        assert listing[spring]["read_by"] == [{"experiment": cid, "title": made["title"], "input": "results"}]
        assert listing[cid]["outdated"] == [] and listing[cid]["results"] == []

        run = value(await s.call_tool("run", {"experiment": cid}))
        assert run["status"] == "done", run.get("error")
        assert [f["title"] for f in run["sources"]["results"]["from"]] == ["Example: survey in spring",
                                                                         "Example: survey in autumn"]
        summary = value(await s.call_tool("result_table", {"experiment": cid, "table": "summary"}))
        assert summary["columns"] == ["experiment", "group", "n", "mean", "sd", "se"] and len(summary["rows"]) == 6
        diffs = value(await s.call_tool("result_table", {"experiment": cid, "table": "differences"}))
        autumn_diffs = [r["difference"] for r in diffs["rows"] if r["experiment"].endswith("autumn")]
        assert len(autumn_diffs) == 3 and all(d > 0 for d in autumn_diffs)  # the autumn wave scores higher
        flags = value(await s.call_tool("result_table", {"experiment": cid, "table": "flags"}))
        assert flags["total"] == 0

        got = value(await s.call_tool("experiment_get", {"experiment": cid}))
        picked = got["sources"]["results"]
        assert [p["title"] for p in picked] == ["Example: survey in spring", "Example: survey in autumn"]
        assert picked[0]["has_result"] and picked[0]["last_run"]["status"] == "done" and not picked[0]["missing"]
        g = value(await s.call_tool("flow_get", {"experiment": cid}))
        item = next(i for i in g["inputs"] if i["id"] == "results")
        assert item["kind"] == "result" and item["value"] == "2 experiments · scores" and item["table"]["rows"] > 200

        # the spring survey changes: the comparison is out of date, nothing reruns by itself
        value(await s.call_tool("experiment_update", {"experiment": spring, "params": {"min_answers": 6}}, meta=USER))
        assert value(await s.call_tool("run", {"experiment": spring}))["status"] == "done"
        got = value(await s.call_tool("experiment_get", {"experiment": cid}))
        assert [o["experiment"] for o in got["outdated"]] == [spring] and got["last_run"]["id"] == run["run"]

        # picking only one experiment: a flag says so
        value(await s.call_tool("experiment_update", {"experiment": cid, "sources": {"results": [
            {"experiment": autumn, "result": "scores"}]}}, meta=USER))
        one = value(await s.call_tool("run", {"experiment": cid}))
        flags = value(await s.call_tool("result_table", {"run": one["run"], "table": "flags"}))
        assert [f["category"] for f in flags["rows"]] == ["one experiment"]
        loop = await s.call_tool("experiment_update", {"experiment": autumn, "sources": {"results": [
            {"experiment": cid, "result": "summary"}]}})
        assert "no input `results` that takes results" in error_text(loop)  # survey-scores reads raw files

        # deleting a source others read is refused, naming them; forced, the reader shows it as missing
        refused = await s.call_tool("experiment_delete", {"experiment": autumn})
        assert "Other experiments read the results of 'Example: survey in autumn'" in error_text(refused)
        assert made["title"] in error_text(refused)
        gone = value(await s.call_tool("experiment_delete", {"experiment": autumn, "force": True}))
        assert gone["readers"] == [{"experiment": cid, "title": made["title"], "input": "results"}]
        got = value(await s.call_tool("experiment_get", {"experiment": cid}))
        assert got["sources"]["results"][0]["missing"] and "was deleted" in got["outdated"][0]["reason"]
        failed = await s.call_tool("run", {"experiment": cid})
        assert "was deleted: pick another one or remove it" in error_text(failed)

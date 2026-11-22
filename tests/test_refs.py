"""References to things in Research (lab/ref_tools.py): the link's encoding, and what reference_get and
reference_make give for every kind against real runs of the examples. Run from plugins/research:
uv run pytest tests/test_refs.py -q
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import ref_tools as R  # noqa: E402
from lab.errors import LabError  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------------------------------- the link

HOSTILE = {
    "experiment": "e0123abcd",
    "result": "step:odd (name) & more = 50% 'quoted' *star* !bang",
    "row": {"name": 'He said "hi" (twice) & left = 100% ünïcödé 日本', "n": 17, "ok": True, "x": 2.5},
    "column": "value (a+b) %",
    "run": "r0123abcd",
    "was": "3,2 (about) & \"so\" = ±1%",
}


def test_round_trip_with_hostile_values():
    ref = R.build_ref(**HOSTILE)
    link = R.encode_link(ref)
    tail = link.removeprefix(R.PREFIX)
    assert link.startswith("layr://research/open/")
    assert not re.search(r"[()!'* \"&=+]", tail), tail  # nothing that ends a Markdown link or splits a query
    assert re.fullmatch(r"[A-Za-z0-9%._~-]+", tail)
    assert R.decode_link(link) == ref
    back = R.parse_ref(R.decode_link(link))
    for k, v in HOSTILE.items():
        assert getattr(back, k) == v, k
    assert R.parse_ref(link) == back  # a whole link is read too
    assert R.build_ref(**{k: getattr(back, k) for k in R.KEYS}) == ref  # the same reference gives the same text


def test_the_row_is_compact_json_with_sorted_keys():
    ref = R.build_ref(experiment="e0123abcd", result="t", row={"b": 1, "a": "x y"}, column="c")
    row = dict(urllib.parse.parse_qsl(ref))["row"]
    assert row == '{"a":"x y","b":1}'
    assert list(dict(urllib.parse.parse_qsl(ref))) == ["experiment", "result", "row", "column"]


def test_the_contract_example_link_reads_back():
    link = ("layr://research/open/experiment%3De85045b5f%26result%3Dgroup_means%26row%3D%257B%2522group%2522%253A"
            "%2522B%2522%257D%26column%3Dmean_score%26run%3Dr104bf6d3%26was%3D3.42")
    ref = R.parse_ref(R.decode_link(link))
    assert (ref.experiment, ref.result, ref.row, ref.column, ref.run, ref.was) == (
        "e85045b5f", "group_means", {"group": "B"}, "mean_score", "r104bf6d3", "3.42")
    assert ref.kind == "number"
    assert R.encode_link(R.build_ref(**{k: getattr(ref, k) for k in R.KEYS})) == link


def test_a_query_string_as_urlsearchparams_writes_it():
    # URLSearchParams writes a space as + and leaves * as it is; it reads both back
    ref = R.parse_ref("?experiment=e0123abcd&result=my+table*&row=%7B%22a%22%3A%22b+c%22%7D&column=x")
    assert ref.result == "my table*" and ref.row == {"a": "b c"}


def test_kinds_by_keys():
    def kind(q: str) -> str:
        return R.parse_ref(q).kind

    assert kind("experiment=e0123abcd") == "experiment"
    assert kind("experiment=e0123abcd&result=scores") == "result"
    assert kind("experiment=e0123abcd&result=alpha&was=0.9") == "number"
    assert kind('experiment=e0123abcd&result=s&row={"a":1}&column=b') == "number"
    assert kind("experiment=e0123abcd&function=group_means") == "step"
    assert kind("method=survey-scores&function=sqrt") == "function"
    assert kind("method=survey-scores") == "method"


@pytest.mark.parametrize("bad", [
    "", "?", "foo=1", "experiment=e0123abcd&experiment=e0123abce", "experiment=nope", "method=Not_An_Id",
    "experiment=e0123abcd&method=survey-scores", "result=scores", "experiment=e0123abcd&row={\"a\":1}",
    "experiment=e0123abcd&result=s&column=c", "experiment=e0123abcd&result=s&row={\"a\":1}",
    "experiment=e0123abcd&result=s&row={bad&column=c", "experiment=e0123abcd&result=s&row=[1]&column=c",
    "experiment=e0123abcd&result=s&row={\"a\":[1]}&column=c", "experiment=e0123abcd&result=s&row={}&column=c",
    "experiment=e0123abcd&result=s&row={\"a\":null}&column=c", "method=survey-scores&result=s",
    "experiment=e0123abcd&result=s&function=f", "experiment=e0123abcd&run=x1", "experiment=e0123abcd&function=a b",
    "experiment=e0123abcd&result=",
])
def test_malformed_references_are_plain_errors(bad):
    with pytest.raises(LabError):
        R.parse_ref(bad)


def test_decode_link_wants_a_research_link():
    with pytest.raises(LabError):
        R.decode_link("layr://wiki/page/Home")


def test_numbers_are_written_like_the_panel():
    cases = {3.4487012: "3.449", 3.0: "3", 17: "17", 0.00012346: "1.235e-4", 1234567.8: "1.235e+6", 123456.7: "123500",
             0.001: "0.001", -2.5: "-2.5", 0.1 + 0.2: "0.3", float("nan"): "", None: "", True: "true", "B": "B",
             1e6: "1000000", 2.5e-7: "2.500e-7"}
    for v, text in cases.items():
        assert R.fmt(v) == text, (v, R.fmt(v))


def test_same_text():
    assert R.same_text("3.449", "3.449")
    assert R.same_text("3.4487", "3.449") and R.same_text("3.449000", "3.449")
    assert not R.same_text("3.2", "3.449") and not R.same_text("urban", "rural")
    assert R.same_text(" urban", "urban")


def test_link_text_rules():
    t = R.link_text("mean [score]\n3.4", "A [big]\nsurvey")
    assert t == "mean (score) 3.4 (A (big) survey)"
    long = R.link_text("mean score 3.42 · group B", "An experiment with a title much longer than anyone would "
                                                    "ever want to read in one link")
    assert len(long) <= R.LINK_TEXT and long.startswith("mean score 3.42 · group B (An experiment") and \
        long.endswith("…)")
    assert len(R.link_text("x" * 200)) <= R.LINK_TEXT
    assert len(R.link_text("y" * 100, "short")) <= R.LINK_TEXT


def test_chips_like_the_panel():
    assert R.chip({"kind": "table", "rows": 12, "columns": list("abcde")}) == "table 12 × 5"
    assert R.chip(0.123456) == "0.1235" and R.chip("urban") == '"urban"' and R.chip(None) == "None"
    assert R.chip({"kind": "list", "length": 1}) == "1 item" and R.chip({"kind": "dict", "length": 3}) == "3 entries"
    assert R.chip({"kind": "column", "length": 9}) == "column of 9" and R.chip(True) == "True"


def test_flag_counts():
    assert R.flag_counts({"warning": 3, "error": 1, "info": 1}) == "1 error, 3 warnings, 1 note"
    assert R.flag_counts({}) == "none"


def test_manifest_declares_the_link_and_the_read_group():
    manifest = json.loads((PLUGIN / "layr.json").read_text("utf-8"))
    assert {"path": "open", "panel": "experiments", "param": "ref"} in manifest["links"]
    read = next(g for g in manifest["access"]["groups"] if g["id"] == "read")
    assert {"reference_get", "reference_make"} <= set(read["tools"])


# ---------------------------------------------------------------------------------------------------- real runs


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """The survey example (titled "Attitude survey") and the GC example, each run once (one calculation process)."""
    from lab import examples
    from lab.runner import Pool, Runner
    from lab.store import Store, new_id, now

    store = Store(tmp_path_factory.mktemp("refs") / "lab")
    runner = Runner(store, Pool())
    files = {}

    def make(which: str, title: str, params: dict | None = None) -> dict:
        ex = examples.get(which)
        if which not in files:
            files[which] = examples.stored_files(store, ex)
        e = {"id": new_id("e"), "title": title, "method": ex.METHOD, "inputs": files[which],
             "params": dict(params or ex.SETTINGS), "corrections": [], "created": now()}
        store.save_experiment(e)
        return e

    def run(e: dict, source: str | None = None) -> dict:
        m = store.get_method(e["method"])
        if source is not None:
            m = {**m, "source": source}
        return runner.run(store.get_experiment(e["id"]), m)

    try:
        survey = make("survey", "Attitude survey")
        survey_run = run(survey)
        assert survey_run["status"] == "done", survey_run.get("error")
        other = make("gc", "Second example")
        other_run = run(other)
        assert other_run["status"] == "done", other_run.get("error")
        yield {"store": store, "make": make, "run": run, "survey": survey, "survey_run": survey_run,
               "other": other, "other_run": other_run}
    finally:
        runner.pool.close()


def get(store, **keys) -> dict:
    out = R.reference_view(store, R.parse_ref(R.build_ref(**keys)))
    return {k: v for k, v in out.items() if not k.startswith("_")}


def test_a_number_in_a_table(lab):
    store, eid, run = lab["store"], lab["survey"]["id"], lab["survey_run"]
    df = store.run_table(run["id"], "group_means")
    urban = float(df[df.group == "urban"].mean_score.iloc[0])
    got = get(store, experiment=eid, result="group_means", row={"group": "urban"}, column="mean_score")
    assert got["ok"] and got["kind"] == "number" and got["title"] == "mean score"
    assert got["subtitle"] == "group urban · Attitude survey"
    assert got["body"] == {"type": "number", "text": R.fmt(urban), "value": urban, "unit": None,
                           "label": "mean score"}
    assert got["run"] == {"id": run["id"], "finished": run["finished"]}
    assert got["stale"] is False and got["changed"] is None
    assert "run%3D" not in got["link"] and "was%3D" not in got["link"]
    assert R.parse_ref(got["link"]).row == {"group": "urban"}


def test_a_number_with_a_unit(lab):
    store, eid, run = lab["store"], lab["other"]["id"], lab["other_run"]
    df = store.run_table(run["id"], "activities")
    first = df.iloc[0]
    got = get(store, experiment=eid, result="activities", row={"compound": first["compound"]},
              column="activity_U_per_gCDW")
    assert got["ok"] and got["body"]["unit"] == "U/gCDW" and got["body"]["label"] == "activity"
    assert got["body"]["value"] == pytest.approx(float(first["activity_U_per_gCDW"]))


def test_changed(lab):
    store, eid = lab["store"], lab["survey"]["id"]
    keys = {"experiment": eid, "result": "group_means", "row": {"group": "urban"}, "column": "mean_score"}
    now = get(store, **keys)["body"]["text"]
    assert get(store, **keys, was="1.5")["changed"] == {"was": "1.5", "now": now}
    assert get(store, **keys, was=now)["changed"] is None
    value = get(store, **keys)["body"]["value"]
    assert get(store, **keys, was=f"{value:.6f}")["changed"] is None  # the same number written longer
    alpha = get(store, experiment=eid, result="alpha", was="0.1")
    assert alpha["kind"] == "number" and alpha["changed"]["was"] == "0.1"


def test_a_single_value_and_a_text(lab):
    store, eid, run = lab["store"], lab["survey"]["id"], lab["survey_run"]
    alpha = get(store, experiment=eid, result="alpha")
    assert alpha["ok"] and alpha["kind"] == "number" and alpha["title"] == "Cronbach's alpha"
    assert alpha["body"]["value"] == pytest.approx(run["outputs"]["alpha"]["value"])
    text = get(store, experiment=eid, result="model_report")
    assert text["kind"] == "result" and text["body"]["type"] == "text"
    assert len(text["body"]["text"]) <= R.TEXT_CHARS and text["body"]["more"] is True
    assert "OLS" in text["body"]["text"]


def test_a_table_at_most_8_by_8(lab):
    store = lab["store"]
    found = None
    for exp, run in ((lab["survey"], lab["survey_run"]), (lab["other"], lab["other_run"])):
        for name in store.table_names(run):
            df = store.run_table(run["id"], name)
            if len(df) > 8 and len(df.columns) > 8:
                found = (exp["id"], name, df)
                break
        if found:
            break
    assert found, "no table of more than 8 rows and columns in the examples"
    eid, name, df = found
    got = get(store, experiment=eid, result=name)
    body = got["body"]
    assert got["kind"] == "result" and body["type"] == "table"
    assert body["columns"] == [str(c) for c in df.columns[:8]] and len(body["rows"]) == 8
    assert all(len(r) == 8 for r in body["rows"]) and body["total"] == len(df)
    assert body["total_columns"] == len(df.columns)
    scores = get(store, experiment=lab["survey"]["id"], result="scores")
    assert scores["title"] == "Scores per respondent" and scores["body"]["total"] == 160
    step = get(store, experiment=lab["survey"]["id"], result="step:group_means")
    assert step["ok"] and step["title"] == "Group means table" and step["body"]["total"] == 3


def test_a_figure(lab):
    store, eid, run = lab["store"], lab["other"]["id"], lab["other_run"]
    name = next(n for n, o in run["outputs"].items() if o["kind"] == "figure")
    got = get(store, experiment=eid, result=name)
    assert got["ok"] and got["kind"] == "result"
    body = got["body"]
    assert body["type"] == "image" and body["mime"] == "image/png" and body["alt"]
    png = base64.b64decode(body["data"])
    assert png.startswith(b"\x89PNG")
    assert len(png) < 400_000, f"the figure is {len(png)} bytes"


def test_a_step_with_its_values(lab):
    store, eid = lab["store"], lab["survey"]["id"]
    got = get(store, experiment=eid, function="group_means")
    assert got["ok"] and got["kind"] == "step" and got["title"] == "Group means"
    assert got["subtitle"] == "step · Attitude survey"
    body = got["body"]
    assert body["type"] == "code" and body["language"] == "python" and body["code"].startswith("def group_means(")
    assert body["sentence"] == "The mean score of each group, with its spread."
    values = {v["name"]: v["text"] for v in body["values"]}
    assert values["returned"] == "table 3 × 5" and values["part"].startswith("table ")
    assert 0 < len(body["values"]) <= R.VALUES
    formula = get(store, experiment=eid, function="cronbach_alpha")
    assert formula["subtitle"].startswith("formula · ") and formula["body"]["formulas"]
    assert r"\frac" in formula["body"]["formulas"][0]
    fv = {v["name"]: v["text"] for v in formula["body"]["values"]}
    assert fv["k"] == "6" and set(fv) == {"k", "sum_item_variances", "total_variance", "returned"}


def test_a_function_and_a_method(lab):
    store = lab["store"]
    fn = get(store, method="survey-scores", function="cronbach_alpha")
    assert fn["ok"] and fn["kind"] == "function" and fn["run"] is None and fn["stale"] is False
    assert fn["subtitle"] == "formula · Survey scale scores" and "values" not in fn["body"]
    assert fn["body"]["formulas"] and fn["body"]["code"].startswith("def cronbach_alpha(")
    m = get(store, method="survey-scores")
    assert m["ok"] and m["kind"] == "method" and m["title"] == "Survey scale scores" and m["run"] is None
    facts = {i["label"]: i["value"] for i in m["body"]["items"]}
    assert facts["Version"] == "1" and facts["Steps"] == "10" and facts["Settings"] == "4"
    assert facts["Used by"].endswith("experiments") or facts["Used by"] == "1 experiment"
    assert facts["Description"].startswith("Scale scores")


def test_an_experiment(lab):
    store, eid, run = lab["store"], lab["survey"]["id"], lab["survey_run"]
    got = get(store, experiment=eid)
    assert got["ok"] and got["kind"] == "experiment" and got["title"] == "Attitude survey"
    assert got["subtitle"] == "Survey scale scores" and got["run"]["id"] == run["id"]
    facts = {i["label"]: i["value"] for i in got["body"]["items"]}
    assert facts["Method"] == "Survey scale scores" and facts["Files"].endswith(".csv")
    assert facts["Last run"].endswith("· calculated") and facts["Proposed corrections waiting"] == "0"
    assert re.search(r"\d+ warnings?", facts["Quality flags"])


def test_stale_after_a_setting_change_without_a_run(lab):
    store = lab["store"]
    e = lab["make"]("survey", "Stale one")
    first = lab["run"](e)
    keys = {"experiment": e["id"], "result": "group_means", "row": {"group": "urban"}, "column": "mean_score"}
    assert get(store, **keys)["stale"] is False
    e = store.get_experiment(e["id"])
    e["params"] = {"min_answers": 6}
    store.save_experiment(e)
    got = get(store, **keys)
    assert got["ok"] and got["stale"] is True and got["run"]["id"] == first["id"]
    assert get(store, experiment=e["id"])["stale"] is True
    e["params"] = {}
    store.save_experiment(e)
    assert get(store, **keys)["stale"] is False
    e["corrections"] = [{"id": "c1", "kind": "drop_respondent", "respondent": 3, "reason": "test",
                         "status": "approved"}]
    store.save_experiment(e)
    assert get(store, **keys)["stale"] is True


def test_every_way_a_thing_can_be_gone(lab):
    store = lab["store"]
    survey = lab["survey"]
    eid = survey["id"]

    def missing(**keys) -> dict:
        got = get(store, **keys)
        assert got["ok"] is False and got["missing"] and got["title"], got
        return got

    # the experiment deleted
    gone = lab["make"]("survey", "To delete")
    store.delete_experiment(gone["id"])
    got = missing(experiment=gone["id"], result="group_means", row={"group": "urban"}, column="mean_score")
    assert got["missing"] == "This experiment no longer exists." and got["title"] == "mean score"
    assert got["kind"] == "number"
    assert missing(experiment=gone["id"])["missing"] == "This experiment no longer exists."

    # never run (and the experiment itself is still shown)
    fresh = lab["make"]("survey", "Never run")
    assert "hasn't been calculated yet" in missing(experiment=fresh["id"], result="scores")["missing"]
    assert "hasn't been calculated yet" in missing(experiment=fresh["id"], function="group_means")["missing"]
    shown = get(store, experiment=fresh["id"])
    assert shown["ok"] and shown["run"] is None and shown["stale"] is False
    assert {"label": "Last run", "value": "not calculated yet"} in shown["body"]["items"]

    # its only run failed
    broken = lab["make"]("survey", "Broken")
    source = store.get_method("survey-scores")["source"]
    assert lab["run"](broken, source.replace("raw = read_table(answers_file)", "raw = read_table(answers_file)\n"
                                             "    raw = raw[\"no_such_column\"]"))["status"] == "failed"
    assert "its last run failed" in missing(experiment=broken["id"], result="scores")["missing"]

    # a later run of a changed method: alpha no longer returned, group_means renamed, mean_score renamed
    changed = lab["make"]("survey", "Changed method")
    keys = {"experiment": changed["id"], "result": "group_means", "row": {"group": "urban"}, "column": "mean_score"}
    lab["run"](changed)
    assert get(store, **keys)["ok"] and get(store, experiment=changed["id"], result="alpha")["ok"]
    new = (source.replace('"alpha": alpha, ', "").replace("def group_means(", "def means_by_group(")
           .replace("means = group_means(scores)", "means = means_by_group(scores)")
           .replace('"mean_score": part.score.mean()', '"mean": part.score.mean()'))
    assert lab["run"](changed, new)["status"] == "done"
    assert missing(experiment=changed["id"], result="alpha")["missing"] == \
        "The experiment's latest run no longer returns “alpha”."
    assert "no longer has the column mean_score" in missing(**keys)["missing"]
    got = missing(experiment=changed["id"], function="group_means")
    assert "no longer has a function group_means" in got["missing"] and got["kind"] == "step"
    assert "made no table" in missing(experiment=changed["id"], result="step:group_means")["missing"]

    # a row not found, no longer one row, its column gone
    assert "any more" in missing(experiment=eid, result="group_means", row={"group": "nowhere"},
                                 column="mean_score")["missing"]
    got = missing(experiment=eid, result="scores", row={"group": "urban"}, column="score")
    assert "rows of “scores” now have group urban" in got["missing"]
    assert "no longer has the column grp" in missing(experiment=eid, result="group_means", row={"grp": "urban"},
                                                     column="mean_score")["missing"]
    assert "no longer a table" in missing(experiment=eid, result="alpha", row={"a": 1}, column="b")["missing"]

    # a function removed from a method, a method deleted
    assert "no longer has a function nope" in missing(method="survey-scores", function="nope")["missing"]
    store.save_method("my-scores", source, "you", "a copy")
    assert get(store, method="my-scores")["ok"]
    store.delete_method("my-scores")
    got = missing(method="my-scores")
    assert got["missing"] == "This method no longer exists." and got["title"] == "my-scores"
    assert missing(method="my-scores", function="sqrt")["missing"] == "This method no longer exists."


def test_make_then_get_for_every_kind(lab):
    store, eid, oid = lab["store"], lab["survey"]["id"], lab["other"]["id"]
    figure = next(n for n, o in lab["other_run"]["outputs"].items() if o["kind"] == "figure")
    cases = [
        ({"experiment": eid}, "experiment"),
        ({"experiment": eid, "result": "scores"}, "result"),
        ({"experiment": eid, "result": "model_report"}, "result"),
        ({"experiment": oid, "result": figure}, "result"),
        ({"experiment": eid, "result": "group_means", "row": {"group": "urban"}, "column": "mean_score"}, "number"),
        ({"experiment": eid, "result": "alpha"}, "number"),
        ({"experiment": eid, "function": "group_means"}, "step"),
        ({"method": "survey-scores", "function": "cronbach_alpha"}, "function"),
        ({"method": "survey-scores"}, "method"),
    ]
    for keys, kind in cases:
        made = R.make_reference(store, **keys)
        assert set(made) == {"ref", "link", "text", "markdown"}
        assert made["link"] == R.encode_link(made["ref"]) and made["markdown"] == f"[{made['text']}]({made['link']})"
        assert "[" not in made["text"] and "]" not in made["text"] and "\n" not in made["text"]
        assert len(made["text"]) <= R.LINK_TEXT
        assert not re.search(r"[()]", made["link"])
        got = R.reference_view(store, R.parse_ref(R.decode_link(made["link"])))
        assert got["ok"] and got["kind"] == kind and got["changed"] is None, (keys, got)
        ref = R.parse_ref(made["ref"])
        if kind == "number":
            assert ref.run == lab["survey_run"]["id"] and ref.was == got["body"]["text"]
        else:
            assert ref.run is None and ref.was is None
    number = R.make_reference(store, **cases[4][0])
    text = number["text"]
    assert text.startswith("mean score ") and text.endswith(" · group urban (Attitude survey)")
    assert R.make_reference(store, **cases[5][0])["text"].startswith("Cronbach's alpha 0.")


def test_make_refuses_what_does_not_exist(lab):
    store, eid = lab["store"], lab["survey"]["id"]
    with pytest.raises(LabError, match="give more columns"):
        R.make_reference(store, experiment=eid, result="scores", row={"group": "urban"}, column="score")
    with pytest.raises(LabError, match="No row of “group_means” has group nowhere."):
        R.make_reference(store, experiment=eid, result="group_means", row={"group": "nowhere"}, column="sd")
    with pytest.raises(LabError, match="no longer exists"):
        R.make_reference(store, experiment="e00000000")
    with pytest.raises(LabError, match="both the row and the column"):
        R.make_reference(store, experiment=eid, result="scores", row={"respondent": 1})
    one = R.make_reference(store, experiment=eid, result="scores", row={"respondent": 1}, column="score")
    assert R.parse_ref(one["ref"]).row == {"respondent": 1}


def test_markdown_text_of_an_awkward_title(lab):
    store = lab["store"]
    e = lab["make"]("survey", "A [very] long\ntitle (with parentheses) that goes on and on about the attitude survey")
    lab["run"](e)
    made = R.make_reference(store, experiment=e["id"], result="group_means", row={"group": "urban"},
                            column="mean_score")
    text = made["text"]
    assert "[" not in text and "]" not in text and "\n" not in text and len(text) <= R.LINK_TEXT
    m = re.fullmatch(r"\[([^\[\]]*)\]\((layr://research/open/[^()\s]+)\)", made["markdown"])
    assert m and m.group(2) == made["link"]


# ---------------------------------------------------------------------------------------------------- the server


async def test_through_the_server(tmp_path: Path):
    from test_server import error_text, server, value

    async with server(tmp_path) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert {"reference_get", "reference_make"} <= set(tools)
        assert "URLSearchParams" in tools["reference_make"].description
        exp = value(await s.call_tool("lab_sample", {"which": "survey"}))
        run = value(await s.call_tool("run", {"experiment": exp["experiment"]}))
        assert run["status"] == "done", run
        made = value(await s.call_tool("reference_make", {"experiment": exp["experiment"], "result": "group_means",
                                                          "row": {"group": "rural"}, "column": "mean_score"}))
        assert made["markdown"].startswith("[mean score ") and "· group rural (" in made["text"]
        got = value(await s.call_tool("reference_get", {"ref": R.decode_link(made["link"])}))
        assert got["ok"] and got["kind"] == "number" and got["run"]["id"] == run["run"] and got["changed"] is None
        assert not any(k.startswith("_") for k in got)
        bad = await s.call_tool("reference_get", {"ref": "experiment=nope"})
        assert "not an experiment id" in error_text(bad)
        gone = value(await s.call_tool("reference_get", {"ref": "experiment=e00000000&result=scores"}))
        assert gone == {"ok": False, "kind": "result", "title": "Scores", "subtitle": None,
                        "link": R.encode_link("experiment=e00000000&result=scores"),
                        "missing": "This experiment no longer exists."}

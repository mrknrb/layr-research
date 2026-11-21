"""What a method declares besides its code (lab/pages.py): pages made of cards, kinds of correction, extra packages;
the survey example end to end through the server (a non-GC study: values, text results, its own corrections and
pages); generic quality flags and survey file formats. Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import checker, methodfile, pages  # noqa: E402
from test_lab import AGENT, USER  # noqa: E402
from test_server import error_text, server, value  # noqa: E402
from toolbox import combine_flags, flag, flags_table, read_table  # noqa: E402

METHODS = Path(__file__).resolve().parent.parent / "backend" / "lab" / "methods"
SMALL = '''"""Small

A small method.
"""

import pandas as pd

DATABASE = ["out"]


def double(table):
    """Twice every value."""
    return table.assign(value=table.value * 2)


def recipe(table_file):
    """Small.

    Inputs:
        table_file: A table [.csv]

    Returns:
        out: Doubled
    """
    table = pd.read_csv(table_file)
    out = double(table)
    return {"out": out}
'''


# ---------------------------------------------------------------------------------------------------- writing


def test_write_adds_replaces_and_removes_only_that_assignment():
    value_ = [{"title": "Overview", "cards": [{"card": "table", "table": "out", "title": "Doubled values"}]}]
    added = pages.write(SMALL, "PAGES", value_)
    assert added.count("PAGES = ") == 1 and pages.COMMENTS["PAGES"] in added
    assert added.index("PAGES") > added.index("DATABASE") and added.index("PAGES") < added.index("def double")
    assert methodfile.parse(added).constants["PAGES"] == value_
    assert [ln for ln in added.splitlines() if not ln.startswith(("PAGES", "#"))] == \
        SMALL.splitlines()[:8] + [""] + SMALL.splitlines()[8:]  # nothing else changed
    longer = value_ + [{"title": "Second", "cards": [{"card": "text", "text": "x" * 150}]}]
    replaced = pages.write(added, "PAGES", longer)
    assert methodfile.parse(replaced).constants["PAGES"] == longer
    assert all(len(ln) <= 120 or '"x' in ln for ln in replaced.splitlines())
    assert replaced.count(pages.COMMENTS["PAGES"]) == 1
    removed = pages.write(replaced, "PAGES", [])
    assert "PAGES" not in removed and pages.COMMENTS["PAGES"] not in removed
    assert removed.splitlines()[:9] == SMALL.splitlines()[:9]
    ast.parse(removed)


def test_write_into_a_file_without_constants_and_same_calculation():
    src = '"""M\n\nA method.\n"""\n\n\ndef recipe(x=1):\n    """R."""\n    return {"x": x}\n'
    out = pages.write(src, "REQUIREMENTS", ["pingouin>=0.5"])
    assert methodfile.parse(out).constants["REQUIREMENTS"] == ["pingouin>=0.5"]
    assert pages.same_calculation(SMALL, pages.write(SMALL, "PAGES", [{"title": "A", "cards": []}]))
    assert pages.same_calculation(SMALL, pages.write(SMALL, "CORRECTIONS", {"drop": {"fields": {"row": "integer"}}}))
    assert not pages.same_calculation(SMALL, pages.write(SMALL, "REQUIREMENTS", ["pingouin"]))
    assert not pages.same_calculation(SMALL, SMALL.replace("* 2", "* 3"))
    with pytest.raises(ValueError):
        pages.write(SMALL, "RECIPE", "x")


# ---------------------------------------------------------------------------------------------------- corrections


def test_correction_kinds_are_checked_and_typed():
    kinds = pages.normalize_kinds({
        "drop": {"label": "Leave out a respondent", "fields": {"respondent": "integer"}},
        "recode": {"label": "Recode", "fields": {"respondent": "integer", "item": "text", "answer": "optional number"},
                   "key": ["respondent", "item"]},
        "window": {"fields": {"start_s": "number", "end_s": "number", "sample": "text"}},
    })
    assert kinds["drop"]["key"] == ["respondent"] and kinds["window"]["key"] == ["sample"]
    assert pages.check_correction(kinds, "drop", {"respondent": "17"}) == {"respondent": 17}
    assert pages.check_correction(kinds, "recode", {"respondent": 3, "item": "q2", "answer": ""}) == \
        {"respondent": 3, "item": "q2"}
    with pytest.raises(ValueError, match="needs respondent"):
        pages.check_correction(kinds, "drop", {})
    with pytest.raises(ValueError, match="whole number"):
        pages.check_correction(kinds, "drop", {"respondent": "seventeen"})
    with pytest.raises(ValueError, match="end_s must come after start_s"):
        pages.check_correction(kinds, "window", {"start_s": 5, "end_s": 2, "sample": "a"})
    with pytest.raises(ValueError, match="no kind of correction 'nope'"):
        pages.check_correction(kinds, "nope", {})
    with pytest.raises(ValueError, match="takes no corrections"):
        pages.check_correction({}, "drop", {"respondent": 1})
    a = {"kind": "recode", "respondent": 3, "item": "q2", "answer": 4.0}
    assert pages.same_target(kinds, a, {**a, "answer": 1.0}) and not pages.same_target(kinds, a, {**a, "item": "q3"})
    assert pages.describe(kinds, a) == "Recode: respondent 3, item q2, answer 4.0"
    legacy = pages.correction_kinds({}, takes_corrections=True)
    assert set(legacy) == {"rename", "exclude", "manual_integration"}
    assert pages.correction_kinds({}, takes_corrections=False) == {}


def test_checker_reads_the_declarations():
    bad = pages.write(SMALL, "PAGES", [{"title": "A", "cards": [{"card": "graph"}, {"card": "table", "table": "nope"}]}])
    bad = pages.write(bad, "REQUIREMENTS", ["https://example.org/x.whl"])
    msgs = [m["message"] for m in checker.check(bad)["messages"]]
    assert any("unknown kind ('graph')" in m for m in msgs) and any("`nope`" in m for m in msgs)
    assert any("REQUIREMENTS" in m for m in msgs)
    needs = pages.write(SMALL.replace("import pandas as pd", "import pandas as pd\nimport pingouin_xyz"),
                        "REQUIREMENTS", ["pingouin_xyz"])
    res = checker.check(needs)
    assert res["ok"], res["messages"]  # not installed yet: a warning, not an error
    assert any("isn't installed yet" in m["message"] for m in res["messages"])
    missing = checker.check(SMALL.replace("import pandas as pd", "import pandas as pd\nimport pingouin_xyz"))
    assert not missing["ok"] and "REQUIREMENTS" in missing["messages"][0]["message"]
    stats = checker.check(SMALL.replace("import pandas as pd", "import pandas as pd\nimport statsmodels.api as sm"))
    assert not [m for m in stats["messages"] if "statsmodels" in m["message"]]


def test_builtin_methods_declare_their_pages_and_corrections():
    for name in ("gc-activity-assay", "survey-scores"):
        src = (METHODS / f"{name}.py").read_text("utf-8")
        res = checker.check(src)
        assert res["ok"] and res["messages"] == [], (name, res["messages"])
        s = res["summary"]
        assert s["pages"] and s["corrections"] and s["takes_corrections"]
    gc = methodfile.summary(methodfile.parse((METHODS / "gc-activity-assay.py").read_text("utf-8")))
    review = gc["pages"][0]["cards"][0]
    assert review["card"] == "review" and review["curve"] == {"file": "gaml_file"}
    assert gc["corrections"]["manual_integration"]["key"] == ["injection", "compound"]


# ---------------------------------------------------------------------------------------------------- toolbox


def test_flags_name_any_item_and_survey_formats(tmp_path: Path):
    gc = flags_table([flag("warning", "peak", "wide", 3, "SUB")])
    assert list(gc.columns) == ["level", "category", "injection", "compound", "message"]
    assert list(flags_table([]).columns) == list(gc.columns)
    survey = flags_table([flag("warning", "answers", "missing", respondent=17)])
    assert list(survey.columns) == ["level", "category", "respondent", "message"]
    both = combine_flags(survey, flags_table([flag("error", "answers", "empty", respondent=4)]))
    assert list(both.columns) == ["level", "category", "respondent", "message"] and both.level[0] == "error"
    pyreadstat = pytest.importorskip("pyreadstat")
    df = pd.DataFrame({"respondent": [1.0, 2.0], "q1": [1.0, 5.0]})
    pyreadstat.write_sav(df, str(tmp_path / "s.sav"), variable_value_labels={"q1": {1.0: "disagree", 5.0: "agree"}})
    got = read_table(tmp_path / "s.sav")
    assert list(got.q1) == ["disagree", "agree"]
    df.to_stata(tmp_path / "s.dta", write_index=False)
    assert list(read_table(tmp_path / "s.dta").q1) == [1.0, 5.0]


# ---------------------------------------------------------------------------------------------------- end to end


async def test_survey_example_pages_corrections_and_text_results(tmp_path: Path):
    async with server(tmp_path) as s:
        exp = value(await s.call_tool("lab_sample", {"which": "survey"}))
        eid = exp["experiment"]
        run = value(await s.call_tool("run", {"experiment": eid}))
        assert run["status"] == "done", run
        outs = run["outputs"]
        assert outs["alpha"]["kind"] == "value" and 0.7 < outs["alpha"]["value"] < 0.95
        assert outs["model_report"]["kind"] == "text" and "OLS Regression Results" in outs["model_report"]["preview"]
        text = value(await s.call_tool("result_value", {"experiment": eid, "output": "model_report"}))
        assert "R-squared" in text["text"]
        model = value(await s.call_tool("result_table", {"experiment": eid, "table": "model"}))
        assert {r["term"] for r in model["rows"]} >= {"Intercept", "age"}
        flags = value(await s.call_tool("result_table", {"experiment": eid, "table": "flags"}))
        assert flags["columns"] == ["level", "category", "respondent", "message"]
        assert {r["respondent"] for r in flags["rows"]} == {24, 98, 59}

        got = value(await s.call_tool("experiment_get", {"experiment": eid}))
        assert [p["title"] for p in got["method_pages"]] == ["Overview", "Respondents"]
        assert set(got["method_corrections"]) == {"drop_respondent", "recode_answer"}

        # a correction of the method's own kind, then the respondent is gone
        refused = await s.call_tool("correction_propose", {"experiment": eid, "kind": "manual_integration",
                                                           "reason": "x", "fields": {"injection": 1}}, meta=USER)
        assert "no kind of correction 'manual_integration'" in error_text(refused)
        c = value(await s.call_tool("correction_propose", {"experiment": eid, "kind": "drop_respondent",
                                                           "reason": "Gave 3 to every item", "fields":
                                                           {"respondent": 59}}, meta=USER))
        assert c["correction"]["respondent"] == 59 and c["correction"]["status"] == "approved"
        proposed = value(await s.call_tool("correction_propose", {"experiment": eid, "kind": "recode_answer",
                                                                  "reason": "typed 55", "fields": {
                                                                      "respondent": 5, "item": "q1", "answer": 5}},
                                           meta=AGENT))
        assert proposed["correction"]["status"] == "proposed"
        got = value(await s.call_tool("experiment_get", {"experiment": eid}))
        assert got["corrections"][0]["what"] == "Leave out a respondent: respondent 59"
        run2 = value(await s.call_tool("run", {"experiment": eid}))
        scores = value(await s.call_tool("result_table", {"experiment": eid, "table": "scores", "limit": 500}))
        assert run2["status"] == "done" and 59 not in {r["respondent"] for r in scores["rows"]}
        assert scores["total"] == 159
        for fmt in ("html", "xlsx", "docx"):  # reports name the correction in words, and keep the texts
            rep = value(await s.call_tool("export", {"experiment": eid, "format": fmt}))
            if fmt == "html":
                page = Path(rep["path"]).read_text("utf-8")
                assert "Leave out a respondent: respondent 59" in page and "OLS Regression Results" in page

        # the user builds a page: a new version that calculates the same, so no comparison is started
        new_pages = got["method_pages"] + [{"title": "Reliability", "cards": [
            {"card": "table", "table": "reliability"}, {"card": "number", "value": "alpha"}]}]
        saved = value(await s.call_tool("method_declare", {"method": "survey-scores", "what": "pages",
                                                           "value": new_pages, "change": "A reliability page"},
                                        meta=USER))
        assert saved["saved"] and saved["reference"] is None and saved["version"] == 2
        changed = [ln for ln in saved["diff"] if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
        assert changed and all(ln.startswith("+") for ln in changed), changed  # only the new page's lines
        assert any("Reliability" in ln for ln in changed)
        got = value(await s.call_tool("experiment_get", {"experiment": eid}))
        assert [p["title"] for p in got["method_pages"]][-1] == "Reliability"
        # an agent's change of the kinds is a draft
        draft = value(await s.call_tool("method_declare", {"method": "survey-scores", "what": "corrections",
                                                           "value": {"drop_respondent": {"fields": {
                                                               "respondent": "integer"}}}}, meta=AGENT))
        assert draft["draft"] and not draft["saved"]
        bad = await s.call_tool("method_declare", {"method": "survey-scores", "what": "requirements",
                                                   "value": ["--index-url http://x"]}, meta=USER)
        assert "Not a package name" in error_text(bad)

        # a curve from a table of the run, for a review card
        cv = value(await s.call_tool("curve", {"experiment": eid, "item": "urban", "table": "scores",
                                               "item_column": "group", "x": "age", "y": "score"}))
        assert len(cv["x"]) > 20 and cv["x"] == sorted(cv["x"])


async def test_gc_corrections_still_work_through_the_declared_kinds(tmp_path: Path):
    async with server(tmp_path) as s:
        exp = value(await s.call_tool("lab_sample", {}))
        eid = exp["experiment"]
        c = value(await s.call_tool("correction_propose", {"experiment": eid, "kind": "manual_integration",
                                                           "injection": 20, "compound": "PROD", "start_min": 6.44,
                                                           "end_min": 6.5, "reason": "valley"}, meta=USER))
        assert c["correction"]["injection"] == 20 and c["correction"]["start_min"] == 6.44
        again = value(await s.call_tool("correction_propose", {"experiment": eid, "kind": "manual_integration",
                                                               "fields": {"injection": 20, "compound": "PROD",
                                                                          "start_min": 6.45, "end_min": 6.5},
                                                               "reason": "better valley"}, meta=USER))
        got = value(await s.call_tool("experiment_get", {"experiment": eid}))
        status = {x["id"]: x["status"] for x in got["corrections"]}
        assert status[c["correction"]["id"]] == "replaced" and status[again["correction"]["id"]] == "approved"
        wrong = await s.call_tool("correction_propose", {"experiment": eid, "kind": "manual_integration",
                                                         "injection": 20, "compound": "PROD", "start_min": 6.5,
                                                         "end_min": 6.4, "reason": "x"}, meta=USER)
        assert "end_min must come after start_min" in error_text(wrong)
        run = value(await s.call_tool("run", {"experiment": eid}))
        assert run["status"] == "done"
        cv = value(await s.call_tool("curve", {"experiment": eid, "item": 20, "file": "gaml_file", "start": 6.3,
                                               "end": 6.6, "points": 300}))
        assert 100 < len(cv["x"]) <= 320 and cv["name"].startswith("Act.Assay")

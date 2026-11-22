"""Unit tests of reading, checking, drawing and recording methods. Run from plugins/research:
uv run pytest tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import checker, compare, latex, methodfile  # noqa: E402
from lab.errors import LabError  # noqa: E402
from lab.export import tex_image  # noqa: E402
from lab.recorder import Recorder, load  # noqa: E402
from lab.tools import copy_from_toolbox, replace_function  # noqa: E402

GC = Path(__file__).resolve().parent.parent / "backend" / "lab" / "methods" / "gc-activity-assay.py"

SMALL = '''"""Small method

Doubles things.
"""

import pandas as pd

LIMITS = (1, 5)


def doubled_mM(conc_mM, factor):
    """Twice the concentration, times a factor."""
    twice = 2 * conc_mM
    return twice * factor


def double_all(table, factor):
    """Double every concentration."""
    out = table.copy()
    out["double_mM"] = doubled_mM(out.conc_mM, factor)
    rows = []
    for compound, group in out.groupby("compound"):
        rows.append({"compound": compound, "total_mM": doubled_mM(group.conc_mM.sum(), 1)})
    return pd.DataFrame(rows)


def recipe(data_file, corrections, factor=1.5, limits=LIMITS, label=None):
    """Double it.

    Settings:
        factor: Factor. How much more.
        limits: Limits [mM]. Low and high.
    Returns:
        totals: Totals
    """
    totals = double_all(data_file, factor)
    return {"totals": totals}
'''


# ---------------------------------------------------------------------------------------------------- reading


def test_method_file_is_read_without_running():
    m = methodfile.parse(SMALL)
    assert m.name == "Small method" and m.description == "Doubles things." and m.recipe == "recipe"
    kinds = {f.name: f.kind for f in m.functions}
    assert kinds == {"doubled_mM": "formula", "double_all": "step", "recipe": "recipe"}
    settings = {s["id"]: s for s in methodfile.settings(m)}
    assert list(settings) == ["factor", "limits", "label"]
    assert settings["factor"] == {"id": "factor", "type": "number", "label": "Factor", "default": 1.5,
                                  "group": "Settings", "required": False, "help": "How much more."}
    assert settings["limits"]["default"] == [1, 5] and settings["limits"]["unit"] == "mM"
    assert settings["label"]["group"] == "Other settings" and settings["label"]["type"] == "text"
    assert methodfile.inputs(m) == [{"id": "data_file", "kind": "file", "label": "data file", "types": [],
                                     "optional": False}]
    m2 = methodfile.parse(SMALL + '\nRECIPE = "double_all"\n')
    assert m2.recipe == "double_all"


def test_gc_method_settings_keep_their_names():
    m = methodfile.parse(GC.read_text("utf-8"))
    settings = {s["id"]: s for s in methodfile.settings(m)}
    old = ["channel", "sample_patterns", "compounds", "internal_standard", "rt_tolerance_min", "rt_drift_correction",
           "smoothing_points", "max_half_width_min", "lod_snr", "loq_snr", "cal_min_conc_mM", "cal_max_conc_mM",
           "cal_series", "force_zero_intercept", "concentration_factor", "rate_window_min", "biomass_gcdw_per_l",
           "biomass_by_substrate", "is_area_tolerance_pct", "shoulder_fwhm_ratio", "min_r2", "max_residual_pct",
           "max_replicate_cv_pct",
           "cv_min_conc_mM", "mass_balance_range_pct"]
    assert list(settings) == old
    assert settings["sample_patterns"]["type"] == "rows" and settings["compounds"]["type"] == "rows"
    assert [c["id"] for c in settings["sample_patterns"]["columns"]] == ["role", "regex"]
    assert settings["compounds"]["columns"][1] == {"id": "rt_min", "label": "rt", "type": "number", "optional": False,
                                                   "unit": "min"}
    assert settings["compounds"]["columns"][2]["optional"]
    assert settings["biomass_gcdw_per_l"]["type"] == "number" and settings["biomass_gcdw_per_l"]["unit"] == "gCDW/L"
    assert settings["biomass_by_substrate"]["type"] == "rows" and settings["biomass_by_substrate"]["default"] == []
    assert settings["cal_max_conc_mM"]["type"] == "number" and settings["rt_tolerance_min"]["unit"] == "min"
    assert settings["compounds"]["required"] and settings["rate_window_min"]["default"] == [0, 30]
    assert settings["rt_tolerance_min"]["group"] == "Integration"
    assert methodfile.inputs(m)[0]["types"] == [".gaml"]


# ---------------------------------------------------------------------------------------------------- checking


def test_checker_passes_the_gc_method():
    result = checker.check(GC.read_text("utf-8"))
    assert result["ok"] and result["messages"] == []


@pytest.mark.parametrize("code, expect", [
    ("def f(x):\n    return x * 2\n\ndef r(a):\n    '''R.'''\n    b = f(a)\n    return {'b': b}\n", "no docstring"),
    ("def f(x):\n    '''F.'''\n    return x.mean() / 2\n\ndef r(a):\n    '''R.'''\n    b = f(a)\n    return {'b': b}\n",
     "not arithmetic"),
    ("def s(t):\n    '''S.'''\n    t['a'] = 1\n    return t\n\n"
     "def r(a):\n    '''R.'''\n    b = s(a)\n    return {'b': b}\n", "changes `t`, which it was given"),
    ("def s(t):\n    '''S.'''\n    u = t\n    u.drop(columns=['a'], inplace=True)\n    return u\n\n"
     "def r(a):\n    '''R.'''\n    b = s(a)\n    return {'b': b}\n", "changes `t`"),
    ("def s(t):\n    '''S.'''\n    return t\n\ndef r(a):\n    '''R.'''\n    b = s(a) + 1\n    return {'b': b}\n",
     "should only call steps"),
    ("def s(t):\n    '''S.'''\n    return t\n\ndef r(a):\n    '''R.'''\n    for i in a:\n        pass\n    return {}\n",
     "should only call steps"),
    ("import os\n\ndef r(a):\n    '''R.'''\n    return {}\n", "`os` is imported"),
    ("import pandas as pd\n\ndef s(t):\n    '''S.'''\n    return pd.read_csv('x.csv')\n\n"
     "def r(a):\n    '''R.'''\n    b = s(a)\n    return {'b': b}\n", "reads or writes files"),
    ("def s(t):\n    '''S.'''\n    return open(t).read()\n\n"
     "def r(a):\n    '''R.'''\n    b = s(a)\n    return {'b': b}\n", "reads or writes files"),
    ("def s(t):\n    '''S.'''\n    return t\n\ndef r(a):\n    '''R.'''\n    s = s(a)\n    return {'s': s}\n",
     "name of a function and also of a result"),
    ("def s(t):\n    '''S.'''\n" + "".join(f"    x{i} = {i}\n" for i in range(35)) + "    return t\n\n"
     "def r(a):\n    '''R.'''\n    b = s(a)\n    return {'b': b}\n", "lines long"),
])
def test_checker_warns_in_plain_words(code, expect):
    result = checker.check(code)
    assert result["ok"], result["messages"]  # warnings never block
    found = [m for m in result["messages"] if expect in m["message"]]
    assert found and found[0]["level"] == "warning" and found[0]["line"], result["messages"]


@pytest.mark.parametrize("code, expect", [
    ("def f(:\n    pass\n", "can't read line 1"),
    ("import no_such_module_here\n\ndef r(a):\n    '''R.'''\n    return {}\n", "no module called"),
    ("from toolbox import fit_curves\n\ndef r(a):\n    '''R.'''\n    return {}\n", "The toolbox has no `fit_curves`"),
    ("X = 1\n", "has no functions"),
    ("RECIPE = 'gone'\n\ndef r(a):\n    '''R.'''\n    return {}\n", "no function of that name"),
])
def test_checker_errors_block_saving(code, expect):
    result = checker.check(code)
    assert not result["ok"]
    assert any(expect in m["message"] and m["level"] == "error" for m in result["messages"]), result["messages"]


# ---------------------------------------------------------------------------------------------------- drawing


def test_formula_drawn_from_function_body():
    m = methodfile.parse(SMALL)
    fn = m.function("doubled_mM")
    lines = latex.draw_function(fn.node)
    assert lines == [r"\mathrm{twice} = 2 \cdot \mathrm{conc}_{[\mathrm{mM}]}",
                     r"\mathrm{doubled}_{[\mathrm{mM}]} = \mathrm{twice} \cdot \mathrm{factor}"]
    with_numbers = latex.draw_function(fn.node, {"conc_mM": 0.25, "factor": -2}, -1.0, with_result=True)
    assert with_numbers == [r"\mathrm{twice} = 2 \cdot 0.25 = 0.5",
                            r"\mathrm{doubled}_{[\mathrm{mM}]} = 0.5 \cdot \left(-2\right) = -1"]


def test_names_with_units_and_docstring_formulas():
    assert latex.var("rate_mM_per_min") == r"\mathrm{rate}_{[\mathrm{mM/min}]}"
    assert latex.var("biomass_gcdw_per_l") == r"\mathrm{biomass}_{[\mathrm{gCDW/L}]}"
    assert latex.var("residual_pct") == r"\mathrm{residual}_{[\mathrm{\%}]}"
    assert latex.var("x_mean") == r"\overline{x}" and latex.var("t_0") == r"t_{\mathrm{0}}"
    assert latex.var("sigma") == r"\sigma" and latex.var("is_area") == r"\mathrm{is\ area}"
    drawn = latex.draw_formula_lines(["slope = sum((x - x_mean) * (y - y_mean)) / sum((x - x_mean) ** 2)",
                                      "$a = b$", "not python at all ("])
    assert drawn[0]["latex"].startswith(r"\mathrm{slope} = \frac{\sum")
    assert drawn[1]["latex"] == "a = b" and drawn[2]["latex"].startswith(r"\mathrm{not")


def test_every_formula_renders_in_reports():
    """Everything drawn from the GC method and the toolbox is LaTeX both KaTeX and matplotlib's mathtext draw."""
    from lab import explain

    m = methodfile.parse(GC.read_text("utf-8"))
    drawn = [f["latex"] for fn in m.functions for f in explain.function_formulas(fn)]
    drawn += [f["latex"] for t in explain.toolbox_docs() for f in t["formulas"]]
    assert len(drawn) > 20
    for tex in drawn:
        assert tex_image(tex) is not None, tex


# ---------------------------------------------------------------------------------------------------- recording


def test_recorder_records_steps_formulas_and_columns(tmp_path: Path):
    m = methodfile.parse(SMALL)
    kinds = {f.name: f.kind for f in m.functions}
    steps = []
    rec = Recorder(limit=2, on_step=lambda entry, result, raw: steps.append((entry["function"], result)))
    module = load(SMALL, tmp_path / "method.py", rec, kinds, "recipe")
    table = pd.DataFrame({"compound": ["A", "A", "B"], "conc_mM": [0.5, 1.0, 2.0]}, index=[10, 11, 12])
    out = module.recipe(table, [], factor=3)
    assert list(out["totals"].total_mM) == [3.0, 4.0]
    report = rec.report()
    calls = report["calls"]
    assert report["counts"] == {"recipe": 1, "double_all": 1, "doubled_mM": 3}
    assert report["kept"]["doubled_mM"] == 2  # capped; the count still says 3
    recipe, step = calls[0], calls[1]
    assert recipe["function"] == "recipe" and recipe["inputs"]["factor"] == 3 and recipe["inputs"]["label"] is None
    assert step["function"] == "double_all" and step["step"] and step["parent"] == recipe["id"]
    assert step["inputs"]["table"] == {"kind": "table", "rows": 3, "columns": ["compound", "conc_mM"]}
    column_call = calls[2]
    assert column_call["function"] == "doubled_mM" and not column_call["step"] and column_call["depth"] == 2
    assert column_call["inputs"]["conc_mM"]["index"] == [10, 11, 12]
    assert column_call["result"]["values"] == [3.0, 6.0, 12.0]
    assert calls[3]["inputs"] == {"conc_mM": 1.5, "factor": 1} and calls[3]["result"] == 3.0
    assert [s[0] for s in steps] == ["double_all"] and isinstance(steps[0][1], pd.DataFrame)
    json.dumps(report)  # everything recorded is plain JSON


def test_recorder_records_errors(tmp_path: Path):
    rec = Recorder()
    src = SMALL.replace("doubled_mM(out.conc_mM, factor)", 'doubled_mM(out["missing_column"], factor)')
    module = load(src, tmp_path / "method.py", rec, {}, "recipe")
    with pytest.raises(KeyError):
        module.recipe(pd.DataFrame({"compound": ["A"], "conc_mM": [1.0]}), [])
    failed = [c for c in rec.report()["calls"] if c.get("error")]
    assert [c["function"] for c in failed] == ["recipe", "double_all"]
    assert all("missing_column" in c["error"] for c in failed)


# ---------------------------------------------------------------------------------------------------- editing


def test_replace_and_add_one_function():
    new = replace_function(SMALL, "doubled_mM", 'def doubled_mM(conc_mM, factor):\n    """Thrice."""\n'
                                                "    return 3 * conc_mM * factor\n")
    assert "return 3 * conc_mM * factor" in new and "twice" not in new
    added = replace_function(SMALL, "halved", 'def halved(x):\n    """Half."""\n    return x / 2\n')
    m = methodfile.parse(added)
    assert [f.name for f in m.functions] == ["doubled_mM", "double_all", "halved", "recipe"]
    with pytest.raises(LabError):
        replace_function(SMALL, "doubled_mM", "def other(x):\n    return x\n")


def test_copy_a_toolbox_function_into_a_method():
    new = copy_from_toolbox(SMALL, "fit_line")
    m = methodfile.parse(new)
    assert "fit_line" in [f.name for f in m.functions]
    assert "from toolbox.fit import Line" in new and "import numpy as np" in new
    assert checker.check(new)["ok"]


# ---------------------------------------------------------------------------------------------------- comparing


def test_compare_tables_pairs_rows_and_lists_moved_numbers():
    a = pd.DataFrame({"compound": ["A", "B", "C"], "conc_mM": [1.0, 2.0, 3.0], "flag": ["", "", "<LOQ"]})
    b = pd.DataFrame({"compound": ["B", "A", "D"], "conc_mM": [2.0, 1.5, 4.0], "flag": ["", "", ""]})
    r = compare.compare_tables(a, b)
    assert r["paired_by"] == ["compound"] and r["rows_added"] == 1 and r["rows_removed"] == 1
    assert r["cells_changed"] == 1 and r["moved"]
    change = next(c for c in r["changes"] if c.get("column") == "conc_mM")
    assert change == {"key": {"compound": "A"}, "column": "conc_mM", "before": 1.0, "after": 1.5, "change_pct": 50.0}
    same = compare.compare_tables(a, a.copy())
    assert not same["moved"] and same["cells_changed"] == 0


def test_compare_pairs_by_the_columns_that_tell_rows_apart():
    """Texts, whole numbers and yes/no name rows, the most telling first, only while they tell more rows apart;
    decimal numbers never do; without such columns rows are paired by position."""
    a = pd.DataFrame({"note": ["x", "x", "y", "y"], "culture": ["A1", "A2", "A1", "A2"], "day": [1, 1, 2, 2],
                      "ok": [True, True, True, True], "od600": [0.1, 0.2, 0.3, 0.4]})
    b = a.copy()
    b.loc[3, "od600"] = 0.5
    r = compare.compare_tables(a, b)
    assert r["paired_by"] == ["note", "culture"] and r["cells_changed"] == 1  # `ok` tells nothing apart, so unused
    assert r["changes"][0]["key"] == {"note": "y", "culture": "A2"}
    floats = pd.DataFrame({"time_h": [0.0, 1.0, 2.0], "od600": [0.1, 0.2, 0.4]})
    moved = floats.assign(od600=[0.1, 0.25, 0.4])
    r = compare.compare_tables(floats, moved)
    assert r["paired_by"] == "position" and r["changes"][0]["key"] == {"row": 2}
    twice = pd.DataFrame({"group": ["a", "a"], "value": [1.0, 2.0]})  # can't tell the two rows apart
    assert compare.compare_tables(twice, twice.assign(value=[1.0, 3.0]))["paired_by"] == "position"


# ---------------------------------------------------------------------------------------------------- settings


ROWS = '''"""Rows

A rows setting.
"""


def recipe(table_file, strains=(("WT", 1.0),), groups=({"group": "a", "dilution": 2, "use": True},)):
    """Recipe.

    Settings:
        strains (rows: strain text, dilution number): Strains.
        groups (rows: group text, optional dilution number, optional n integer, use boolean): Groups.

    Returns:
        out: Out
    """
    return {"out": table_file}
'''


def test_rows_settings_declare_their_columns_and_check_values():
    m = methodfile.parse(ROWS)
    settings = {s["id"]: s for s in methodfile.settings(m)}
    groups = settings["groups"]
    assert groups["type"] == "rows"
    assert groups["columns"] == [
        {"id": "group", "label": "group", "type": "text", "optional": False},
        {"id": "dilution", "label": "dilution", "type": "number", "optional": True},
        {"id": "n", "label": "n", "type": "integer", "optional": True},
        {"id": "use", "label": "use", "type": "boolean", "optional": False}]
    assert methodfile.check_value(groups, [{"group": "b", "n": "3", "use": "yes", "dilution": ""}]) == \
        [{"group": "b", "n": 3, "use": True}]
    for bad, says in [({"group": "b"}, "needs its use"), ({"group": "b", "use": True, "n": 2.5}, "whole number"),
                      ({"group": "b", "use": True, "colour": "red"}, "no column colour"),
                      ("b", "list of rows")]:
        with pytest.raises(ValueError, match=says):
            methodfile.check_value(groups, [bad] if isinstance(bad, dict) else bad)
    # a default that doesn't fit its columns (a tuple of tuples, not row dicts) is a warning; one that fits isn't
    messages = [x["message"] for x in checker.check(ROWS)["messages"]]
    assert any("default of `strains`" in x for x in messages) and not any("`groups`" in x for x in messages)
    bad_spec = ROWS.replace("(rows: strain text, dilution number)", "(rows: strain colour)")
    assert any("`strain colour` isn't a column" in x["message"] for x in checker.check(bad_spec)["messages"])


def test_toolbox_signatures_are_shown_as_written():
    import toolbox
    from lab import codeintel, explain

    assert explain.toolbox_view("fit_line")["signature"] == "fit_line(x, y) -> Line"
    peak = explain.toolbox_view("PeakSettings")["signature"]
    assert "'" not in peak and "ForwardRef" not in peak and "smoothing_points: int = 11" in peak
    assert "ForwardRef" not in codeintel._signature_text("Line", toolbox.Line)

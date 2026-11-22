"""The ready-made steps (toolbox/blocks.py): each one, its errors in plain words, the palette read from them, and a
growth-rate method made only of blocks and two formulas, run by the real runner and explained like any other.
Run from plugins/research: uv run pytest tests/test_blocks.py -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import checker, methodfile  # noqa: E402
from lab import explain as explainer  # noqa: E402
from lab.runner import Pool, Runner  # noqa: E402
from lab.store import Store, new_id, now  # noqa: E402
from toolbox import (add_column, fit_lines, join_tables, pick_columns, pick_rows, plot_bars, plot_lines,  # noqa: E402
                     rename_column, sort_rows, summarize)
from toolbox.blocks import blocks  # noqa: E402

RATES = {"A": 0.40, "B": 0.44, "C": 0.25, "D": 0.27}  # growth rate per hour of each culture
STRAINS = {"A": "WT", "B": "WT", "C": "dpta", "D": "dpta"}
BLANK = 0.05


def plate() -> pd.DataFrame:
    """A made-up plate reader table: OD600 every hour for 8 h, growing exactly exponentially above the blank."""
    rows = [{"culture": c, "strain": STRAINS[c], "time_h": float(t), "od600": BLANK + 0.02 * math.exp(mu * t)}
            for c, mu in RATES.items() for t in range(9)]
    return pd.DataFrame(rows)


@pytest.fixture()
def od() -> pd.DataFrame:
    return plate()


def ln_od(od600, blank_od600):
    """The natural logarithm of the optical density after the blank is taken off."""
    return np.log(od600 - blank_od600)


# ---------------------------------------------------------------------------------------------------- each block


def test_pick_rows_equals_one_of_between_contains(od):
    before = od.copy()
    assert set(pick_rows(od, "strain", "WT").culture) == {"A", "B"}
    assert set(pick_rows(od, "culture", one_of=["A", "D"]).culture) == {"A", "D"}
    window = pick_rows(od, "time_h", between=[2, 6])
    assert window.time_h.min() == 2 and window.time_h.max() == 6 and len(window) == 4 * 5
    assert list(window.index) == list(range(len(window)))  # numbered afresh
    assert set(pick_rows(od, "strain", contains="pt").strain) == {"dpta"}
    assert set(pick_rows(od, "strain", "WT", leave_out=True).strain) == {"dpta"}
    pd.testing.assert_frame_equal(od, before)  # the table it was given is unchanged


def test_pick_rows_errors(od):
    with pytest.raises(ValueError, match=r"no column 'role'\. Its columns are: culture, strain, time_h, od600"):
        pick_rows(od, "role", "sample")
    with pytest.raises(ValueError, match="Give exactly one of"):
        pick_rows(od, "strain")
    with pytest.raises(ValueError, match="you gave equals and between"):
        pick_rows(od, "time_h", 2, between=[1, 3])
    with pytest.raises(ValueError, match="between needs two numbers"):
        pick_rows(od, "time_h", between=[1])
    with pytest.raises(ValueError, match="must be a table; it is list"):
        pick_rows([1, 2], "x", 1)


def test_pick_columns_rename_and_sort(od):
    small = pick_columns(od, ["od600", "culture"])
    assert list(small.columns) == ["od600", "culture"]
    assert list(pick_columns(od, "culture").columns) == ["culture"]
    with pytest.raises(ValueError, match="no column 'od'"):
        pick_columns(od, ["culture", "od"])
    renamed = rename_column(od, "od600", "od600_raw")
    assert "od600_raw" in renamed.columns and "od600" in od.columns
    with pytest.raises(ValueError, match="already has a column 'strain'"):
        rename_column(od, "culture", "strain")
    ordered = sort_rows(od, ["strain", "od600"], descending=True)
    assert ordered.strain.iloc[0] == "dpta" and ordered.od600.iloc[0] == od[od.strain == "dpta"].od600.max()
    assert sort_rows(od, "time_h").time_h.is_monotonic_increasing
    with pytest.raises(ValueError, match="no column 'when'"):
        sort_rows(od, "when")


def test_add_column_takes_columns_by_name_and_constants_by_keyword(od):
    before = od.copy()
    out = add_column(od, ln_od, blank_od600=BLANK)
    assert "ln_od" in out.columns and "ln_od" not in od.columns
    assert out.ln_od.iloc[1] == pytest.approx(math.log(0.02) + RATES["A"])
    named = add_column(od, ln_od, name="ln_od600", blank_od600=BLANK)
    assert "ln_od600" in named.columns
    pd.testing.assert_frame_equal(od, before)


def test_add_column_errors(od):
    with pytest.raises(ValueError, match=r"needs `blank_od600`.*its columns are: culture, strain, time_h, od600.*"
                                         r"add_column\(table, ln_od, blank_od600=\.\.\.\)"):
        add_column(od, ln_od)
    with pytest.raises(ValueError, match="takes no input called `blank`"):
        add_column(od, ln_od, blank_od600=BLANK, blank=1)
    with pytest.raises(ValueError, match="`od600` is a column of the table and also given as a number"):
        add_column(od, ln_od, od600=1, blank_od600=BLANK)
    with pytest.raises(ValueError, match="needs a formula"):
        add_column(od, "ln_od")


def test_summarize_by_group_and_other_statistics(od):
    s = summarize(od, "od600", by="strain")
    assert list(s.columns) == ["strain", "n", "mean", "sd"] and list(s.strain) == ["WT", "dpta"]
    wt = od[od.strain == "WT"].od600
    assert s.n.iloc[0] == 18 and s["mean"].iloc[0] == pytest.approx(wt.mean())
    assert s.sd.iloc[0] == pytest.approx(wt.std(ddof=1))
    both = summarize(od, ["od600", "time_h"], by=["strain", "culture"], statistics=["mean", "se", "max", "cv_pct"])
    assert "od600_se" in both.columns and "time_h_max" in both.columns and len(both) == 4
    whole = summarize(od, "od600", statistics="median")
    assert list(whole.columns) == ["median"] and whole["median"].iloc[0] == pytest.approx(od.od600.median())
    one = summarize(od.head(1), "od600")
    assert one.n.iloc[0] == 1 and math.isnan(one.sd.iloc[0])
    with pytest.raises(ValueError, match="Unknown statistic 'average'. Known: n, mean, sd"):
        summarize(od, "od600", statistics=["average"])
    with pytest.raises(ValueError, match="no column 'group'"):
        summarize(od, "od600", by="group")


def test_join_tables_keeps_the_first_tables_rows(od):
    info = pd.DataFrame({"culture": ["A", "B", "C"], "medium": ["M9", "M9", "LB"], "strain": ["x", "x", "y"]})
    joined = join_tables(od, info, "culture")
    assert len(joined) == len(od) and "medium" in joined.columns and "strain_2" in joined.columns
    assert joined[joined.culture == "D"].medium.isna().all()
    assert len(join_tables(od, info, "culture", keep="matching")) == 27
    with pytest.raises(ValueError, match="second table has no column 'strain'|keep must be one of"):
        join_tables(od, info.drop(columns="strain"), ["culture", "strain"])
    with pytest.raises(ValueError, match="keep must be one of first, matching, all"):
        join_tables(od, info, "culture", keep="left")


def test_fit_lines_gives_one_line_per_group(od):
    logged = add_column(od, ln_od, blank_od600=BLANK)
    fits = fit_lines(logged, "time_h", "ln_od", by=["culture", "strain"])
    assert list(fits.columns) == ["culture", "strain", "slope", "intercept", "se", "r2", "s_yx", "n"]
    for _, row in fits.iterrows():
        assert row.slope == pytest.approx(RATES[row.culture]) and row.r2 == pytest.approx(1.0) and row.n == 9
    gaps = logged.copy()
    gaps.loc[0, "ln_od"] = np.nan
    assert fit_lines(gaps, "time_h", "ln_od", by="culture").n.iloc[0] == 8  # the empty row is left out
    zero = fit_lines(pd.DataFrame({"x": [1.0, 2.0], "y": [2.0, 4.0]}), "x", "y", through_zero=True)
    assert zero.slope.iloc[0] == pytest.approx(2.0) and zero.intercept.iloc[0] == 0
    with pytest.raises(ValueError, match="no column 'ln_od'"):
        fit_lines(od, "time_h", "ln_od")


def test_charts_return_a_figure(od):
    s = summarize(od, "od600", by=["strain", "time_h"])
    fig = plot_lines(s, "time_h", "mean", by="strain", error="sd", title="Growth")
    assert type(fig).__name__ == "Figure" and len(fig.axes[0].lines) >= 2
    bars = plot_bars(summarize(od, "od600", by="strain"), "strain", "mean", error="sd")
    assert type(bars).__name__ == "Figure" and len(bars.axes[0].patches) == 2
    grouped = plot_bars(s[s.time_h.isin([0, 8])], "time_h", "mean", by="strain")
    assert len(grouped.axes[0].patches) == 4
    with pytest.raises(ValueError, match="no column 'sd'"):
        plot_lines(od, "time_h", "od600", error="sd")
    with pytest.raises(ValueError, match="empty: nothing to plot"):
        plot_bars(od.head(0), "strain", "od600")


def test_every_block_is_in_the_toolbox_and_has_a_plain_sentence():
    import importlib

    for b in blocks():
        module = importlib.import_module(b["module"])
        fn = getattr(module, b["function"])
        assert b["function"] in module.__all__ and b["module"].split(".")[0] == "toolbox"
        assert b["sentence"] and b["sentence"].endswith(".") and len(b["sentence"]) <= 120, b["function"]
        assert explainer.toolbox_view(b["function"])["sentence"] == b["sentence"]
        assert fn.__doc__


# ---------------------------------------------------------------------------------------------------- the palette


def test_palette_is_read_from_the_functions():
    palette = {b["function"]: b for b in blocks()}
    assert {"read_table", "read_gaml", "pick_rows", "pick_columns", "sort_rows", "add_column", "summarize",
            "join_tables", "fit_lines", "plot_lines", "plot_bars"} <= set(palette)
    assert palette["read_table"]["takes"] == ["file"] and palette["read_table"]["gives"] == "table"
    assert palette["read_gaml"]["gives"] == "injections"
    assert palette["join_tables"]["takes"] == ["table", "table"]
    assert palette["plot_bars"]["gives"] == "figure"
    fields = {f["name"]: f for f in palette["pick_rows"]["fields"]}
    assert fields["table"]["kind"] == "table" and not fields["table"]["optional"]
    assert fields["column"] == {"name": "column", "kind": "column", "label": "Column", "optional": False,
                                "default": None, "keyword": False, "of": ["table"]}
    assert fields["leave_out"]["kind"] == "boolean" and fields["leave_out"]["default"] is False
    assert palette["pick_rows"]["choose_one"] == ["equals", "one_of", "between", "contains"]
    fit = {f["name"]: f for f in palette["fit_lines"]["fields"]}
    assert fit["x"]["kind"] == "column" and fit["by"]["kind"] == "columns" and fit["by"]["optional"]
    keep = {f["name"]: f for f in palette["join_tables"]["fields"]}["keep"]
    assert keep["kind"] == "choice" and keep["default"] == "first" and len(keep["choices"]) == 3
    assert {f["name"]: f for f in palette["join_tables"]["fields"]}["on"]["of"] == ["first", "second"]
    stats = {f["name"]: f for f in palette["summarize"]["fields"]}["statistics"]
    assert stats["default"] == ["n", "mean", "sd"] and stats["multiple"]
    assert {f["name"]: f["kind"] for f in palette["add_column"]["fields"]} == {"table": "table", "formula": "formula",
                                                                              "name": "text"}
    assert palette["add_column"]["constants"]
    for b in palette.values():
        assert b["example"].split(" = ")[1].startswith(b["function"] + "("), b["function"]
        assert b["import"] == f"from {b['module']} import {b['function']}"
    assert palette["read_table"]["module"] == "toolbox" and palette["read_gaml"]["module"] == "toolbox.gc"


def test_the_general_toolbox_knows_no_add_on():
    """The add-ons are found by their folders; the general toolbox never imports one (a fresh Python shows it)."""
    import subprocess

    code = ("import sys, toolbox; from toolbox import registry; "
            "assert not [m for m in sys.modules if m.startswith('toolbox.') and m.count('.') == 1 "
            "and m in registry.addons()], sorted(sys.modules); "
            "assert registry.addons() == ['toolbox.gc']; "
            "assert [r['function'] for r in registry.readers()][:1] == ['read_table']")
    backend = Path(__file__).resolve().parent.parent / "backend"
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------------------------------- in a method

METHOD = '''"""Growth rate from OD600, from ready-made steps

Specific growth rates from the exponential phase of each culture, and their mean for each strain.
"""

import numpy as np

from toolbox import (add_column, fit_lines, pick_rows, plot_bars, read_table, rename_column, sort_rows,
                     summarize)


def ln_od(od600, blank_od600):
    """The natural logarithm of the optical density after the blank is taken off."""
    return np.log(od600 - blank_od600)


def doubling_time_h(growth_rate_per_h):
    """The time a culture takes to double, from its specific growth rate."""
    return np.log(2) / growth_rate_per_h


def growth(plate_file, window_h=(2, 6), blank_od600=0.05):
    """Growth rate from OD600.

    Inputs:
        plate_file: Plate reader export (CSV or Excel) [.csv, .xlsx]

    Settings:
        window_h: Exponential window. Start and end of the exponential phase.
        blank_od600: Blank. The OD600 of the medium alone.

    Returns:
        rates: Growth rates
        strains: Growth rate per strain
        chart: Growth rate per strain
    """
    od = read_table(plate_file)
    logged = add_column(od, ln_od, blank_od600=blank_od600)
    exponential = pick_rows(logged, "time_h", between=window_h)
    fits = fit_lines(exponential, "time_h", "ln_od", by=["culture", "strain"])
    named = rename_column(fits, "slope", "growth_rate_per_h")
    rates = add_column(named, doubling_time_h)
    per_strain = summarize(rates, "growth_rate_per_h", by="strain")
    strains = sort_rows(per_strain, "mean", descending=True)
    chart = plot_bars(strains, "strain", "mean", error="sd")
    return {"rates": rates, "strains": strains, "chart": chart}
'''


@pytest.fixture(scope="module")
def grown(tmp_path_factory):
    """The growth method run once by the real runner on the made-up plate."""
    tmp = tmp_path_factory.mktemp("blocks")
    csv = tmp / "plate.csv"
    plate().to_csv(csv, index=False)
    store = Store(tmp / "lab")
    f = store.add_file(csv)
    exp = {"id": new_id("e"), "title": "growth", "method": "growth", "inputs": {"plate_file": f}, "params": {},
           "corrections": [], "created": now()}
    store.save_experiment(exp)
    runner = Runner(store, Pool())
    try:
        run = runner.run(exp, {"id": "growth", "version": 1, "name": "Growth", "source": METHOD})
    finally:
        runner.pool.close()
    return store, run


def test_a_method_from_blocks_passes_the_style_checker():
    result = checker.check(METHOD)
    assert result["ok"]
    assert [m for m in result["messages"] if m["level"] in ("error", "warning")] == []
    kinds = {f["name"]: f["kind"] for f in result["summary"]["outline"]}
    assert kinds == {"ln_od": "formula", "doubling_time_h": "formula", "growth": "recipe"}


def test_a_method_from_blocks_runs_and_gives_the_right_numbers(grown):
    store, run = grown
    assert run["status"] == "done", run.get("error")
    assert [s["function"] for s in run["steps"]] == ["read_table", "add_column", "pick_rows", "fit_lines",
                                                     "rename_column", "add_column", "summarize", "sort_rows",
                                                     "plot_bars"]
    assert all(s["kind"] == "toolbox" and s["status"] == "done" for s in run["steps"])
    rates = store.run_table(run["id"], "rates")
    for _, row in rates.iterrows():
        assert row.growth_rate_per_h == pytest.approx(RATES[row.culture])
        assert row.doubling_time_h == pytest.approx(math.log(2) / RATES[row.culture])
    strains = store.run_table(run["id"], "strains")
    assert list(strains.strain) == ["WT", "dpta"] and strains["mean"].iloc[0] == pytest.approx(0.42)
    assert run["outputs"]["chart"]["kind"] == "figure"


def test_a_formula_used_through_add_column_is_recorded_and_explained(grown):
    store, run = grown
    calls = explainer.calls_of(store, run["id"])
    method = methodfile.parse(METHOD)
    view = explainer.function_view(method, "doubling_time_h", calls, run)
    assert view["kind"] == "formula" and view["count"] == 1 and view["formulas"]
    example = view["example"]
    assert example["inputs"]["growth_rate_per_h"] == pytest.approx(RATES["A"])
    assert example["result"] == pytest.approx(math.log(2) / RATES["A"])
    assert example["latex"] and "0.4" in example["latex"][0]  # drawn with this run's numbers
    ln = explainer.function_view(method, "ln_od", calls, run)
    assert ln["count"] == 1 and ln["example"]["inputs"]["blank_od600"] == BLANK


def test_explain_says_what_each_block_did(grown):
    store, run = grown
    explained = explainer.explain_run(store, run)
    steps = explained["steps"]
    assert steps[1]["function"] == "add_column" and steps[1]["kind"] == "toolbox"
    assert steps[1]["sentence"].startswith("Add a column calculated by a formula")
    used = {u["function"]: u for u in steps[1]["used"]}
    assert used["ln_od"]["kind"] == "formula" and used["ln_od"]["formulas"]
    assert used["ln_od"]["sentence"] == "The natural logarithm of the optical density after the blank is taken off."
    assert steps[3]["function"] == "fit_lines" and "straight line" in steps[3]["sentence"]
    assert steps[3]["output"]["columns"][:3] == ["culture", "strain", "slope"]
    block = explainer.toolbox_view("pick_rows", explainer.calls_of(store, run["id"]))
    assert block["count"] == 1 and block["sample"][0]["inputs"]["column"] == "time_h"
    assert block["signature"].startswith("pick_rows(table")


def test_where_a_number_from_a_formula_in_add_column_comes_from(grown):
    store, run = grown
    trace = explainer.provenance(store, run, "rates", {"culture": "B"}, column="doubling_time_h")
    first = trace["chain"][0]
    assert first["function"] == "doubling_time_h" and first["kind"] == "formula"
    assert first["inputs"]["growth_rate_per_h"] == pytest.approx(RATES["B"])
    assert first["latex_numbers"]


def test_a_block_copied_into_the_method_passes_the_checker():
    from lab.tools import copy_from_toolbox

    for name in ("add_column", "fit_lines", "summarize", "pick_rows", "plot_bars"):
        copied = copy_from_toolbox(METHOD, name)
        assert f"def {name}(" in copied
        assert [m for m in checker.check(copied)["messages"] if m["level"] in ("error", "warning")] == [], name

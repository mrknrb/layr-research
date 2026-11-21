"""Help while writing a method (lab/codeintel.py, lab/code_tools.py): completion with known columns first, hover,
signatures, definitions, rename, format, and code Python can't read yet never failing a call.
Run from plugins/research: uv run pytest tests/test_code.py -q
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from lab import checker, code_tools, codeintel  # noqa: E402
from lab.errors import LabError  # noqa: E402

METHODS = Path(__file__).resolve().parent.parent / "backend" / "lab" / "methods"
GC = (METHODS / "gc-activity-assay.py").read_text("utf-8")
PEAKS = ["injection", "compound", "area", "rt_min", "status", "role", "area ratio"]


def where(text: str, source: str = GC, after: str | None = None) -> tuple[int, int]:
    """The line (from 1) of the first line containing `text`, and the column just after `after` in it (or its
    start)."""
    lines = source.split("\n")
    n = next(i for i, ln in enumerate(lines, 1) if text in ln)
    return n, (lines[n - 1].index(after) + len(after)) if after else lines[n - 1].index(text)


def append(line: str) -> tuple[str, int, int]:
    """The GC method with one more line typed at its end, and the cursor at the end of that line."""
    source = GC.rstrip("\n") + "\n" + line
    return source, len(source.split("\n")), len(line)


@pytest.fixture(scope="module", autouse=True)
def warm():
    t0 = time.perf_counter()
    codeintel.warm(wait=True)
    print(f"\nwarm: {codeintel.WARM} in {round((time.perf_counter() - t0) * 1000)} ms")


# ---------------------------------------------------------------------------------------------------- completion


def test_peaks_dot_completes_a_table_without_columns():
    line, col = where("standard = peaks[peaks.compound == internal_standard]", after="peaks[peaks.")
    t0 = time.perf_counter()
    out = codeintel.complete(GC, line, col)
    print(f"peaks. in the GC method: {round((time.perf_counter() - t0) * 1000)} ms")
    labels = [i["label"] for i in out["items"]]
    assert {"groupby", "merge", "copy", "sort_values"} <= set(labels)
    assert out["from"] == col and out["prefix"] == ""
    assert len(out["items"]) <= codeintel.LIMIT
    assert all(not i["label"].startswith("_") for i in out["items"])
    first_rare = next(n for n, i in enumerate(out["items"]) if i["label"] not in codeintel.COMMON)
    assert all(i["label"] not in codeintel.COMMON for i in out["items"][first_rare:])  # the common ones first
    groupby = next(i for i in out["items"] if i["label"] == "groupby")
    assert groupby["kind"] == "function" and groupby["group"] == "library" and groupby["detail"]


def test_peaks_dot_puts_known_columns_first():
    line, col = where("standard = peaks[peaks.compound == internal_standard]", after="peaks[peaks.")
    t0 = time.perf_counter()
    out = codeintel.complete(GC, line, col, {"peaks": PEAKS})
    print(f"peaks. with columns: {round((time.perf_counter() - t0) * 1000)} ms")
    items = out["items"]
    assert [i["label"] for i in items[:6]] == PEAKS[:6]  # "area ratio" can't follow a dot
    assert items[0] == {"label": "injection", "insert": "injection", "kind": "column", "detail": "column of peaks",
                        "group": "column"}
    assert "groupby" in [i["label"] for i in items[6:]]
    typed = codeintel.complete(GC, line, col + 2, {"peaks": PEAKS})  # peaks.co|
    assert typed["prefix"] == "co" and typed["from"] == col and typed["items"][0]["label"] == "compound"
    assert "copy" in [i["label"] for i in typed["items"]]


def test_inside_brackets_only_columns():
    source, line, col = append('    x = peaks["ar')
    out = codeintel.complete(source, line, col, {"peaks": PEAKS})
    assert [i["label"] for i in out["items"]] == ["area", "area ratio"]
    assert out["items"][0]["insert"] == 'area"]' and out["from"] == col - 2
    assert "can't be read by Python yet" in out["note"]
    closed = codeintel.complete(GC.rstrip("\n") + "\n    x = peaks['']", line, len("    x = peaks['"),
                                {"peaks": PEAKS})
    assert closed["items"][0]["insert"] == "injection"  # the quote and bracket are there already
    unknown = codeintel.complete(source, line, col, {})
    assert unknown["items"] == [] and "known after a run" in unknown["note"]


def test_own_functions_and_the_toolbox_come_first():
    source, line, col = append("    x = area_")
    out = codeintel.complete(source, line, col)
    own = {i["label"]: i for i in out["items"]}
    assert own["area_ratios"]["group"] == "own" and own["area_ratios"]["kind"] == "function"
    assert own["area_ratios"]["detail"] == "area_ratios(peaks, internal_standard)"
    assert own["area_ratios"]["info"] == "Divide each compound's peak area by the internal standard's area in the " \
                                         "same injection."
    source, line, col = append("    x = fit_l")
    out = codeintel.complete(source, line, col)
    assert out["items"][0]["label"] == "fit_line" and out["items"][0]["group"] == "toolbox"
    assert out["items"][0]["detail"] == "fit_line(x, y) -> Line"
    assert out["items"][0]["info"].startswith("Fit the straight line")
    source, line, col = append("    x = s")
    groups = [i["group"] for i in codeintel.complete(source, line, col)["items"]]
    assert groups == sorted(groups, key=codeintel.GROUPS.index)


# ---------------------------------------------------------------------------------------------------- hover, signature


def test_hover_a_method_function_a_toolbox_function_a_column_and_a_table():
    line, col = where("ratios = area_ratios(peaks", after="ratios = area_")
    h = codeintel.hover(GC, line, col)
    assert h["name"] == "area_ratios" and h["function_kind"] == "step" and h["module"] == "this method"
    assert h["signature"] == "area_ratios(peaks, internal_standard)" and h["sentence"].startswith("Divide each")
    line, col = where("line = fit_line(", after="line = fit_")
    h = codeintel.hover(GC, line, col)
    assert h["module"] == "toolbox.fit" and h["signature"] == "fit_line(x, y) -> Line"
    line, col = where("standard = peaks[peaks.compound == internal_standard]", after="peaks[peaks.com")
    assert codeintel.hover(GC, line, col, {"peaks": PEAKS})["kind"] == "column"
    line, col = where("standard = peaks[peaks.compound == internal_standard]", after="standard = pe")
    h = codeintel.hover(GC, line, col)
    assert h["type"] == "DataFrame" and h["sentence"] == "A table (pandas DataFrame)."
    line, col = where("    return peaks.merge(samples", after="    ret")
    assert codeintel.hover(GC, line, col)["kind"] == "keyword"
    assert codeintel.hover("x = 1\n\ny = 2\n", 2, 0) == {}


def test_signature_of_a_toolbox_function_and_an_own_one():
    source, line, col = append("    x = fit_line(a, ")
    s = codeintel.signature(source, line, col)["signatures"][0]
    assert s["name"] == "fit_line" and s["label"] == "fit_line(x, y) -> Line" and s["index"] == 1
    assert [p["name"] for p in s["params"]] == ["x", "y"] and s["sentence"].startswith("Fit the straight line")
    source, line, col = append("    x = area_ratios(")
    s = codeintel.signature(source, line, col)["signatures"][0]
    assert s["label"] == "area_ratios(peaks, internal_standard)" and s["index"] == 0
    assert s["sentence"].startswith("Divide each")
    source, line, col = append("    x = summarize(t, ")
    s = codeintel.signature(source.replace("from toolbox import ", "from toolbox import summarize, ", 1), line,
                            col)["signatures"][0]
    assert s["name"] == "summarize" and s["index"] == 1 and s["sentence"].startswith("For each group of rows")


def test_definition_in_the_method_and_in_the_toolbox():
    line, col = where("ratios = area_ratios(peaks", after="ratios = area_")
    d = codeintel.definition(GC, line, col)["definitions"][0]
    assert d["in_file"] and d["line"] == where("def area_ratios(")[0] and d["module"] == "this method"
    line, col = where("line = fit_line(", after="line = fit_")
    d = codeintel.definition(GC, line, col)["definitions"][0]
    assert not d["in_file"] and d["module"] == "toolbox.fit" and d["toolbox"] == "fit_line"


# ---------------------------------------------------------------------------------------------------- rename, format


def test_rename_a_function_everywhere():
    line, col = where("ratios = area_ratios(peaks", after="ratios = area_")
    out = codeintel.rename(GC, line, col, "ratios_to_standard")
    assert out["old_name"] == "area_ratios" and "area_ratios(" not in out["source"]
    assert out["source"].count("ratios_to_standard(") == 2 and len(out["lines"]) == 2
    assert checker.check(out["source"])["ok"] and "warning" not in out
    assert len(out["source"].split("\n")) == len(GC.split("\n"))


def test_rename_a_recipe_setting_warns_and_wrong_renames_are_refused():
    line, col = where("    ratios = area_ratios(peaks, internal_standard)", after="ratios = area_ratios(peaks, int")
    out = codeintel.rename(GC, line, col, "standard_compound")
    assert "experiments keep their settings by name" in out["warning"] and "standard_compound=" in out["source"]
    with pytest.raises(LabError, match="comes from the toolbox"):
        codeintel.rename(GC, *where("line = fit_line(", after="line = fit_"), "fit")
    with pytest.raises(LabError, match="can't be a name in Python"):
        codeintel.rename(GC, line, col, "2nd")
    with pytest.raises(LabError, match="already used"):
        codeintel.rename(GC, *where("ratios = area_ratios(peaks", after="ratios = area_"), "peaks")
    with pytest.raises(LabError, match="fix that first"):
        codeintel.rename(GC + "\ndef broken(:\n", line, col, "x2")


def test_rename_warns_for_a_recipe_input():
    source = '"""M"""\n\n\ndef recipe(table_file, window_h=2):\n    """R."""\n    return {"w": window_h}\n'
    out = codeintel.rename(source, 6, 18, "window_min")
    assert "window_min=2" in out["source"] and '"w": window_min' in out["source"]
    assert "experiments keep their settings by name" in out["warning"]


def test_format_with_ruff_at_120_columns():
    messy = "import numpy as np\ndef f( x ):\n    '''A formula.'''\n    return np.log( x )*2\n"
    out = codeintel.format_source(messy)
    assert out["changed"] and out["source"] == ('import numpy as np\n\n\ndef f(x):\n    """A formula."""\n'
                                                '    return np.log(x) * 2\n')
    assert codeintel.format_source(out["source"]) == {"source": out["source"], "changed": False}
    long = "x = [" + ", ".join(str(i) for i in range(40)) + "]\n"
    assert max(len(ln) for ln in codeintel.format_source(long)["source"].splitlines()) <= 120
    broken = codeintel.format_source("def f(:\n    pass\n")
    assert not broken["changed"] and broken["source"] == "def f(:\n    pass\n" and "line 1" in broken["error"]


# ---------------------------------------------------------------------------------------------------- broken code


def test_code_python_cant_read_never_fails_a_call():
    broken = GC.replace("def area_ratios(peaks, internal_standard):", "def area_ratios(peaks, internal_standard)")
    line, col = where("standard = peaks[peaks.compound == internal_standard]", broken, after="peaks[peaks.")
    out = codeintel.complete(broken, line, col, {"peaks": PEAKS})
    assert "can't be read by Python yet" in out["note"] and out["items"][0]["kind"] == "column"
    assert "note" in codeintel.hover(broken, line, col)
    assert "note" in codeintel.signature(broken, line, col)
    assert "note" in codeintel.definition(broken, line, col)
    assert codeintel.complete("", 1, 0)["items"] is not None
    assert codeintel.complete("x = (", 99, 99)["from"] == 5  # a position past the end is moved inside


# ---------------------------------------------------------------------------------------------------- the tools


def test_tools_registered_on_a_server():
    mcp = FastMCP("test")
    code_tools.register(mcp, None, None)

    async def go():
        names = {t.name for t in await mcp.list_tools()}
        assert names == {"blocks", "code_complete", "code_hover", "code_signature", "code_definition", "code_rename",
                         "code_format"}
        _, palette = await mcp.call_tool("blocks", {})
        assert "pick_rows" in [b["function"] for b in palette["blocks"]]
        line, col = where("standard = peaks[peaks.compound == internal_standard]", after="peaks[peaks.")
        _, done = await mcp.call_tool("code_complete", {"source": GC, "line": line, "column": col,
                                                        "columns": {"peaks": PEAKS}})
        assert done["items"][0]["label"] == "injection"
        _, fmt = await mcp.call_tool("code_format", {"source": "x=1\n"})
        assert fmt == {"source": "x = 1\n", "changed": True}

    asyncio.run(go())

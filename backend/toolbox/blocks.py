"""Ready-made steps: a table in, a table out (or a figure). Each can be used as it is in a recipe, or copied into a
method and changed.

    rows = pick_rows(od, "role", "sample")
    od = add_column(od, ln_od, blank_od600=blank_od600)
    rates = fit_lines(exponential, "time_h", "ln_od", by="culture")
    summary = summarize(rates, "slope", by="strain")

Columns are named by their text ("time_h"). None of these change the table they are given: each makes a new one.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from .fit import fit_line, fit_line_through_zero
from .plots import COLORS

STATISTICS = ["n", "mean", "sd", "se", "median", "min", "max", "sum", "cv_pct"]
KEEP = {"first": "left", "matching": "inner", "all": "outer"}


# ---------------------------------------------------------------------------------------------------- checks


def _need_table(table, what: str = "table") -> None:
    if not isinstance(table, pd.DataFrame):
        raise ValueError(f"The {what} must be a table; it is {type(table).__name__}.")


def _need_columns(table: pd.DataFrame, columns, what: str = "table") -> list[str]:
    """The column names as a list, after checking the table has every one of them."""
    names = [columns] if isinstance(columns, str) else list(columns or [])
    missing = [c for c in names if c not in table.columns]
    if missing:
        which = ", ".join(f"'{c}'" for c in missing)
        has = ", ".join(map(str, table.columns)) or "none"
        raise ValueError(f"The {what} has no column {which}. Its columns are: {has}.")
    return names


def _inputs_of(formula: Callable) -> list[tuple[str, bool]]:
    """A formula's inputs in order: (name, whether it has a default value)."""
    params = inspect.signature(formula).parameters.values()
    return [(p.name, p.default is not p.empty) for p in params if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]


# ---------------------------------------------------------------------------------------------------- rows and columns


def pick_rows(table: pd.DataFrame, column: str, equals=None, *, one_of: list | None = None,
              between: list | None = None, contains: str | None = None, leave_out: bool = False) -> pd.DataFrame:
    """Keep the rows whose value in one column equals a value, is one of several, lies between two, or contains a text.

    Give one of the four: `pick_rows(cultures, "role", "sample")`, `pick_rows(od, "strain", one_of=["WT", "dpta"])`,
    `pick_rows(od, "time_h", between=[2, 6])` (both ends count) or `pick_rows(runs, "name", contains="blank")`.
    With `leave_out=True` those rows are left out and the others kept.
    """
    _need_table(table)
    _need_columns(table, column)
    given = [k for k, v in {"equals": equals, "one_of": one_of, "between": between, "contains": contains}.items()
             if v is not None]
    if len(given) != 1:
        raise ValueError("Give exactly one of: a value it equals, one_of, between or contains"
                         + (f" (you gave {' and '.join(given)})." if given else "."))
    values = table[column]
    if equals is not None:
        chosen = values == equals
    elif one_of is not None:
        chosen = values.isin([one_of] if isinstance(one_of, str) else list(one_of))
    elif between is not None:
        if len(between) != 2:
            raise ValueError(f"between needs two numbers, the lowest and the highest; it is {between}.")
        low, high = between
        chosen = values.between(low, high)
    else:
        chosen = values.astype(str).str.contains(str(contains), regex=False, na=False)
    if leave_out:
        chosen = ~chosen
    return table[chosen].reset_index(drop=True)


def pick_columns(table: pd.DataFrame, columns: str | list[str]) -> pd.DataFrame:
    """Keep only some columns of a table, in the order given."""
    _need_table(table)
    names = _need_columns(table, columns)
    return table[names].copy()


def rename_column(table: pd.DataFrame, column: str, new_name: str) -> pd.DataFrame:
    """Give one column of a table a new name (with its unit, e.g. growth_rate_per_h)."""
    _need_table(table)
    _need_columns(table, column)
    if new_name in table.columns and new_name != column:
        raise ValueError(f"The table already has a column '{new_name}': pick another name.")
    return table.rename(columns={column: new_name})


def sort_rows(table: pd.DataFrame, by: str | list[str], descending: bool = False) -> pd.DataFrame:
    """Sort the rows by one or more columns (the first decides, the next breaks ties), smallest first."""
    _need_table(table)
    names = _need_columns(table, by)
    return table.sort_values(names, ascending=not descending, kind="stable").reset_index(drop=True)


def add_column(table: pd.DataFrame, formula: Callable, name: str | None = None, **constants) -> pd.DataFrame:
    """Add a column calculated by a formula of the method, row by row: its inputs are columns of the same name.

    `add_column(od, ln_od, blank_od600=0.05)` with `def ln_od(od600, blank_od600)` takes `od600` from the table's
    column and `blank_od600` from the number given. The new column is named after the formula (`ln_od`), or `name`.
    """
    _need_table(table)
    if not callable(formula):
        raise ValueError(f"add_column needs a formula (a function of the method); it was given {formula!r}.")
    title = getattr(formula, "__name__", "formula")
    inputs = {}
    missing = []
    for input_name, has_default in _inputs_of(formula):
        if input_name in constants and input_name in table.columns:
            raise ValueError(f"`{input_name}` is a column of the table and also given as a number: give only one.")
        if input_name in constants:
            inputs[input_name] = constants[input_name]
        elif input_name in table.columns:
            inputs[input_name] = table[input_name]
        elif not has_default:
            missing.append(input_name)
    if missing:
        has = ", ".join(map(str, table.columns)) or "none"
        raise ValueError(f"The formula `{title}` needs {', '.join(f'`{m}`' for m in missing)}: the table has no "
                         f"column of that name (its columns are: {has}) and no number was given for it "
                         f"(e.g. `add_column(table, {title}, {missing[0]}=...)`).")
    unknown = [k for k in constants if k not in inputs]
    if unknown:
        raise ValueError(f"The formula `{title}` takes no input called {', '.join(f'`{u}`' for u in unknown)}.")
    new = table.copy()
    new[name or title] = formula(**inputs)
    return new


def join_tables(first: pd.DataFrame, second: pd.DataFrame, on: str | list[str], keep: str = "first") -> pd.DataFrame:
    """Put two tables side by side, matching rows by the columns they share (e.g. the same culture).

    keep: "first" keeps every row of the first table (empty where the second has no match), "matching" only rows
    found in both, "all" every row of either. A column both tables have, besides `on`, gets `_2` in the second's copy.
    """
    _need_table(first, "first table")
    _need_table(second, "second table")
    names = _need_columns(first, on, "first table")
    _need_columns(second, names, "second table")
    if keep not in KEEP:
        raise ValueError(f"keep must be one of {', '.join(KEEP)}; it is {keep!r}.")
    return first.merge(second, on=names, how=KEEP[keep], suffixes=("", "_2"))


# ---------------------------------------------------------------------------------------------------- groups


def _groups(table: pd.DataFrame, by) -> list[tuple[dict, pd.DataFrame]]:
    """Each group's identifying values (a dict) and its rows; the whole table as one group without `by`."""
    names = _need_columns(table, by) if by else []
    if not names:
        return [({}, table)]
    out = []
    for key, rows in table.groupby(names, sort=False, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        out.append((dict(zip(names, key)), rows))
    return out


def _statistic(values: pd.Series, name: str) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna()
    n = len(values)
    if name == "n":
        return n
    if n == 0:
        return float("nan")
    if name == "mean":
        return float(values.mean())
    if name == "sd":
        return float(values.std(ddof=1)) if n > 1 else float("nan")
    if name == "se":
        return float(values.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    if name == "cv_pct":
        mean = values.mean()
        return float(100 * values.std(ddof=1) / mean) if n > 1 and mean != 0 else float("nan")
    return float(getattr(values, name)())


def summarize(table: pd.DataFrame, values: str | list[str], by: str | list[str] | None = None,
              statistics: list[str] = ("n", "mean", "sd")) -> pd.DataFrame:
    """For each group of rows, the number of values, their mean and standard deviation (or other statistics).

    `summarize(rates, "slope", by="strain")` gives one row per strain with n, mean and sd. Statistics by name: n,
    mean, sd, se (standard error), median, min, max, sum, cv_pct (sd as a percentage of the mean). Missing values are
    left out; sd needs two values. With several value columns the new columns are named `slope_mean`, `r2_mean`, …
    """
    _need_table(table)
    columns = _need_columns(table, values)
    stats = [statistics] if isinstance(statistics, str) else list(statistics)
    unknown = [s for s in stats if s not in STATISTICS]
    if unknown:
        raise ValueError(f"Unknown statistic {', '.join(map(repr, unknown))}. Known: {', '.join(STATISTICS)}.")
    rows = []
    for key, group in _groups(table, by):
        row = dict(key)
        for column in columns:
            for stat in stats:
                row[stat if len(columns) == 1 else f"{column}_{stat}"] = _statistic(group[column], stat)
        rows.append(row)
    return pd.DataFrame(rows)


def fit_lines(table: pd.DataFrame, x: str, y: str, by: str | list[str] | None = None,
              through_zero: bool = False) -> pd.DataFrame:
    """Fit a straight line y = slope · x + intercept through each group's points (with `fit_line`).

    One row per group: slope, intercept, se (standard error of the slope), r2, s_yx (scatter around the line) and n
    (points used). Rows with an empty x or y are left out. With `through_zero=True` the line goes through the origin
    (`fit_line_through_zero`).
    """
    _need_table(table)
    _need_columns(table, [x, y])
    fit = fit_line_through_zero if through_zero else fit_line
    rows = []
    for key, group in _groups(table, by):
        xs = pd.to_numeric(group[x], errors="coerce")
        ys = pd.to_numeric(group[y], errors="coerce")
        usable = xs.notna() & ys.notna()
        line = fit(xs[usable], ys[usable])
        rows.append({**key, **line._asdict()})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------------- figures


def _figure(title: str | None, x_label: str, y_label: str):
    fig = Figure(figsize=(6, 4), dpi=130)
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if title:
        ax.set_title(title, fontsize=10)
    ax.grid(axis="y", color="#e6e8ec", lw=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    return fig, ax


def plot_lines(table: pd.DataFrame, x: str, y: str, by: str | None = None, error: str | None = None,
               title: str | None = None) -> Figure:
    """Draw y against x as points joined by lines, one line (and colour) per group, with error bars when given.

    `plot_lines(summary, "time_h", "mean", by="strain", error="sd")`: one line per strain, ± sd as error bars.
    """
    _need_table(table)
    _need_columns(table, [x, y] + ([by] if by else []) + ([error] if error else []))
    if table.empty:
        raise ValueError("The table is empty: nothing to plot.")
    fig, ax = _figure(title, x, y)
    for i, (key, rows) in enumerate(_groups(table, by)):
        rows = rows.sort_values(x)
        yerr = rows[error].fillna(0) if error else None
        ax.errorbar(rows[x], rows[y], yerr=yerr, marker="o", ms=4, capsize=3 if error else 0, lw=1.6,
                    color=COLORS[i % len(COLORS)], label=str(key[by]) if by else None)
    if by:
        ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    return fig


def plot_bars(table: pd.DataFrame, x: str, y: str, by: str | None = None, error: str | None = None,
              title: str | None = None) -> Figure:
    """Draw one bar per row (its x is the label, its y the height), side by side per group, with error bars.

    `plot_bars(summary, "strain", "mean", error="sd")`: a bar per strain, ± sd as error bars.
    """
    _need_table(table)
    _need_columns(table, [x, y] + ([by] if by else []) + ([error] if error else []))
    if table.empty:
        raise ValueError("The table is empty: nothing to plot.")
    fig, ax = _figure(title, x, y)
    labels = list(dict.fromkeys(table[x].astype(str)))
    groups = _groups(table, by)
    width = 0.8 / len(groups)
    for i, (key, rows) in enumerate(groups):
        where = np.array([labels.index(v) for v in rows[x].astype(str)]) + (i - (len(groups) - 1) / 2) * width
        yerr = rows[error].fillna(0) if error else None
        ax.bar(where, rows[y], width=width, yerr=yerr, capsize=3 if error else 0, color=COLORS[i % len(COLORS)],
               label=str(key[by]) if by else None)
    ax.set_xticks(range(len(labels)), labels)
    if by:
        ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------------------------------- the palette

# What the palette shows for each ready-made step beyond its signature: a title, which fields are columns (and of
# which table), labels and choices. Everything else (defaults, optional, numbers, texts) is read from the function.
FORMS: dict[str, dict] = {
    "read_table": {"title": "Read a table", "takes": ["file"], "fields": {
        "table_file": {"kind": "file", "label": "Table file (CSV, TSV or Excel)"},
        "sheet": {"label": "Excel sheet"}, "separator": {"label": "Separator"}, "decimal": {"label": "Decimal mark"}},
        "example": 'od = read_table(plate_file)'},
    "pick_rows": {"title": "Pick rows", "fields": {
        "column": {"kind": "column", "label": "Column"}, "equals": {"label": "Equals"},
        "one_of": {"kind": "list", "label": "Is one of"}, "between": {"kind": "range", "label": "Lies between"},
        "contains": {"label": "Contains"}, "leave_out": {"label": "Leave these rows out instead"}},
        "choose_one": ["equals", "one_of", "between", "contains"],
        "example": 'samples = pick_rows(cultures, "role", "sample")'},
    "pick_columns": {"title": "Pick columns", "fields": {"columns": {"kind": "columns", "label": "Columns"}},
                     "example": 'small = pick_columns(rates, ["culture", "slope"])'},
    "rename_column": {"title": "Rename a column", "fields": {
        "column": {"kind": "column", "label": "Column"}, "new_name": {"label": "New name"}},
        "example": 'rates = rename_column(fits, "slope", "growth_rate_per_h")'},
    "sort_rows": {"title": "Sort rows", "fields": {
        "by": {"kind": "columns", "label": "Sort by"}, "descending": {"label": "Largest first"}},
        "example": 'ordered = sort_rows(rates, "slope", descending=True)'},
    "add_column": {"title": "Add a column from a formula", "fields": {
        "formula": {"kind": "formula", "label": "Formula"}, "name": {"label": "New column's name"}},
        "constants": "The formula's inputs that are not columns, by name (numbers or settings).",
        "example": "od = add_column(od, ln_od, blank_od600=blank_od600)"},
    "join_tables": {"title": "Join two tables", "fields": {
        "on": {"kind": "columns", "of": ["first", "second"], "label": "Match rows by"},
        "keep": {"kind": "choice", "label": "Keep", "choices": [
            {"value": "first", "label": "Every row of the first table"},
            {"value": "matching", "label": "Only rows found in both"},
            {"value": "all", "label": "Every row of either"}]}},
        "example": 'rates = join_tables(rates, cultures, "culture")'},
    "summarize": {"title": "Summarize by group", "fields": {
        "values": {"kind": "columns", "label": "Values"}, "by": {"kind": "columns", "label": "For each"},
        "statistics": {"kind": "choice", "multiple": True, "label": "Statistics",
                       "choices": [{"value": s, "label": s} for s in STATISTICS]}},
        "example": 'summary = summarize(rates, "slope", by="strain")'},
    "fit_lines": {"title": "Fit a line per group", "fields": {
        "x": {"kind": "column", "label": "x"}, "y": {"kind": "column", "label": "y"},
        "by": {"kind": "columns", "label": "For each"}, "through_zero": {"label": "Through the origin"}},
        "example": 'fits = fit_lines(exponential, "time_h", "ln_od", by="culture")'},
    "plot_lines": {"title": "Line chart", "fields": {
        "x": {"kind": "column", "label": "x"}, "y": {"kind": "column", "label": "y"},
        "by": {"kind": "column", "label": "A line for each"}, "error": {"kind": "column", "label": "Error bars"},
        "title": {"label": "Title"}},
        "example": 'chart = plot_lines(summary, "time_h", "mean", by="strain", error="sd")'},
    "plot_bars": {"title": "Bar chart", "fields": {
        "x": {"kind": "column", "label": "Bars"}, "y": {"kind": "column", "label": "Height"},
        "by": {"kind": "column", "label": "Side by side for each"}, "error": {"kind": "column", "label": "Error bars"},
        "title": {"label": "Title"}},
        "example": 'chart = plot_bars(summary, "strain", "mean", error="sd")'},
}


def _type_word(annotation) -> str:
    """table, figure, formula, number, text, yes/no, list or data, from a parameter's or result's annotation."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return "data"
    if annotation is pd.DataFrame:
        return "table"
    if annotation is Figure:
        return "figure"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    if annotation is str:
        return "text"
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin is list or annotation is list:
        return "list"
    if origin in (Callable, typing.get_origin(Callable)) or annotation is Callable:
        return "formula"
    if args and len(args) == 1:
        return _type_word(args[0])
    if args and str in args:
        return "text"
    return "data"


FIELD_KINDS = {"table": "table", "formula": "formula", "number": "number", "text": "text", "boolean": "boolean",
               "list": "list", "data": "text"}


def blocks() -> list[dict]:
    """The palette of ready-made steps, read from the functions themselves: {function, title, sentence, takes,
    gives, fields: [{name, kind, label, default, choices, optional, keyword, of}], example, import, module (where it
    is imported from: toolbox or an add-on), choose_one (fields of which exactly one is given), constants (what extra
    keyword arguments mean)}. Add-ons add their own (their FORMS)."""
    import toolbox

    from .registry import load_addons

    out = []
    sources = [(toolbox, FORMS)] + [(m, getattr(m, "FORMS", {})) for m in load_addons()]
    for module, forms in sources:
        for name, form in forms.items():
            fn = getattr(module, name, None)
            if fn is not None:
                out.append(_block(name, form, fn, module.__name__))
    return out


def _block(name: str, form: dict, fn: Callable, module: str) -> dict:
    """One palette entry (see `blocks`)."""
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    doc = inspect.getdoc(fn) or ""
    tables = [p for p in sig.parameters if _type_word(hints.get(p, inspect.Parameter.empty)) == "table"]
    fields = []
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        declared = form["fields"].get(p.name, {})
        kind = declared.get("kind") or FIELD_KINDS[_type_word(hints.get(p.name, inspect.Parameter.empty))]
        field = {"name": p.name, "kind": kind,
                 "label": declared.get("label") or p.name.replace("_", " ").capitalize(),
                 "optional": p.default is not p.empty,
                 "default": None if p.default is p.empty else
                 (list(p.default) if isinstance(p.default, tuple) else p.default),
                 "keyword": p.kind == p.KEYWORD_ONLY}
        if kind in ("column", "columns"):
            field["of"] = declared.get("of") or (tables[:1] if tables else [])
        if declared.get("choices"):
            field["choices"] = declared["choices"]
        if declared.get("multiple"):
            field["multiple"] = True
        fields.append(field)
    item = {"function": name, "title": form["title"], "sentence": doc.splitlines()[0] if doc else "",
            "takes": form.get("takes") or ["table" for _ in tables],
            "gives": form.get("gives") or _type_word(hints.get("return", inspect.Parameter.empty)),
            "fields": fields, "example": form.get("example", ""), "import": f"from {module} import {name}",
            "module": module}
    if form.get("choose_one"):
        item["choose_one"] = form["choose_one"]
    if form.get("constants"):
        item["constants"] = form["constants"]
    return item

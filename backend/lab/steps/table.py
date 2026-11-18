"""General table and statistics step types: read a table, compute a column with a formula, filter, summarize,
compare groups. These make any tabular analysis (plate reader, growth curves, counts) a method too."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from ..core import Ctx, Param, Port, StepError, StepOut, StepType, num, register, sheet, tex_name
from ..formula import Formula, legend


def _cols(df: pd.DataFrame, names: list[str] | None, what: str) -> list[str]:
    out = []
    for n in names or []:
        if n not in df.columns:
            raise StepError(f"No column '{n}' ({what}). Columns: {', '.join(map(str, df.columns))}.")
        out.append(n)
    return out


# ---------------------------------------------------------------------------------------------------- read


def _run_read(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    f = inputs["file"]
    if not isinstance(f, dict) or not f.get("path"):
        raise StepError("The table input needs a file (CSV, TSV or Excel) from the store.")
    path = ctx.store_path(f["path"])
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, sheet_name=p.get("sheet") or 0)
    else:
        text = path.read_text("utf-8-sig", errors="replace")
        sep = p.get("separator") or None
        if sep is None:
            head = text[:4096]
            sep = max([",", ";", "\t", "|"], key=head.count)
        decimal = p.get("decimal") or ("," if sep == ";" else ".")
        df = pd.read_csv(io.StringIO(text), sep=sep, decimal=decimal)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    return StepOut({"table": df}, {"rows": len(df), "columns": list(df.columns), "name": f.get("name")})


register(StepType(
    id="table.read",
    title="Read a table",
    category="Tables",
    sentence="Reads a table (CSV, TSV or Excel) into rows and named columns.",
    inputs=[Port("file", "file", "Table file")],
    outputs=[Port("table", "table", "Table")],
    params=[Param("sheet", "text", "Excel sheet", None, "Name of the sheet; empty: the first."),
            Param("separator", "text", "Separator", None, "Empty: guessed from , ; tab or |."),
            Param("decimal", "text", "Decimal mark", None, "Empty: comma when the separator is ;, else point.")],
    run=_run_read,
    explain=lambda p: sheet(rules=["Values are read as they are in the file; the file itself is never changed and "
                                   "is stored with its checksum."], code="pandas.read_csv / pandas.read_excel"),
    methods=lambda p, f: f"Data were read from {f.get('name') or 'the data file'} ({f.get('rows')} rows).",
))


# ---------------------------------------------------------------------------------------------------- compute


def _run_compute(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    df = inputs["table"].copy()
    col = (p.get("column") or "").strip()
    if not col:
        raise StepError("Name the new column.")
    f = Formula(p.get("formula") or "")
    by = _cols(df, p.get("by"), "group by")
    v = f.evaluate(df, by or None)
    df[col] = v if isinstance(v, pd.Series) else pd.Series(v, index=df.index)
    facts: dict[str, Any] = {"column": col, "rows": len(df)}
    ok = df[df[col].notna()]
    if len(ok):
        i = ok.index[len(ok) // 2]
        row = df.loc[i].to_dict()
        group = df
        if by:
            key = tuple(row[b] for b in by)
            group = df[(df[by].apply(tuple, axis=1)) == key]
        facts["example"] = {"row": int(df.index.get_loc(i)) + 1, "latex": f.latex(row, group),
                            "value": row[col], "group": {b: row[b] for b in by}}
    return StepOut({"table": df}, facts)


def _compute_explain(p: dict) -> dict:
    try:
        f = Formula(p.get("formula") or "")
    except StepError as e:
        return sheet(rules=[str(e)])
    unit = f"\\ \\mathrm{{{p['unit']}}}" if p.get("unit") else ""
    by = p.get("by") or []
    return sheet(
        formulas=[{"label": p.get("description") or f"New column {p.get('column')}",
                   "latex": f"{tex_name(p.get('column') or 'x')} = {f.latex()}{unit}"}],
        symbols=legend(f),
        rules=([f"Means, SDs and other aggregates are taken within each group of {', '.join(by)}."] if by else [])
        + ["Computed for every row; a missing value in a row gives a missing result in that row."],
        code=f"{p.get('column')} = {p.get('formula')}",
    )


register(StepType(
    id="table.compute",
    title="Calculate a column",
    category="Tables",
    sentence="Adds a column calculated with a formula from the other columns.",
    inputs=[Port("table", "table", "Table")],
    outputs=[Port("table", "table", "Table with the new column")],
    params=[Param("column", "text", "New column", ""),
            Param("formula", "formula", "Formula", "", "e.g. od600 / mean(od600[treatment == \"control\"])"),
            Param("by", "columns", "Within groups of", [], "Aggregates (mean, sd ...) per group of these columns."),
            Param("unit", "text", "Unit", ""),
            Param("description", "text", "What it means", "")],
    run=_run_compute,
    explain=_compute_explain,
    methods=lambda p, f: f"{p.get('description') or p.get('column')} was calculated as {p.get('column')} = "
                         f"{p.get('formula')}" + (f" within each {', '.join(p['by'])}" if p.get("by") else "") + ".",
    example=lambda p, f: [] if not f.get("example") else [{
        "label": f"Row {f['example']['row']}" + (" (" + ", ".join(f"{k} {v}" for k, v in f["example"]["group"].items())
                                                + ")" if f["example"]["group"] else ""),
        "latex": f"{tex_name(p.get('column'))} = {f['example']['latex']} = {num(f['example']['value'])}"}],
))


# ---------------------------------------------------------------------------------------------------- filter


def _run_filter(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    df = inputs["table"]
    f = Formula(p.get("condition") or "")
    keep = f.evaluate(df)
    if not isinstance(keep, pd.Series):
        keep = pd.Series(bool(keep), index=df.index)
    out = df[keep.fillna(False).astype(bool)]
    return StepOut({"table": out}, {"before": len(df), "after": len(out)})


register(StepType(
    id="table.filter",
    title="Keep some rows",
    category="Tables",
    sentence="Keeps only the rows where a condition holds.",
    inputs=[Port("table", "table", "Table")],
    outputs=[Port("table", "table", "Rows kept")],
    params=[Param("condition", "formula", "Keep rows where", "", "e.g. role == \"sample\" and time_min <= 30"),
            Param("reason", "text", "Why", "", "Said in the explanation and the Methods text.")],
    run=_run_filter,
    explain=lambda p: sheet(
        formulas=[{"label": "Rows kept", "latex": Formula(p["condition"]).latex()}] if p.get("condition") else [],
        rules=[p.get("reason") or "No reason given."],
        code=f"table[{p.get('condition')}]"),
    methods=lambda p, f: f"Only rows with {p.get('condition')} were kept ({f.get('after')} of {f.get('before')})"
                         + (f": {p['reason']}" if p.get("reason") else "") + ".",
    example=lambda p, f: [{"label": "Rows", "latex": rf"{f.get('before')} \rightarrow {f.get('after')}"}],
))


# ---------------------------------------------------------------------------------------------------- summarize

STATS = {
    "n": ("n", r"n"),
    "mean": ("mean", r"\bar{x} = \frac{1}{n}\sum x_i"),
    "sd": ("SD", r"s = \sqrt{\frac{\sum (x_i - \bar{x})^2}{n-1}}"),
    "sem": ("SEM", r"\mathrm{SEM} = \frac{s}{\sqrt{n}}"),
    "cv": ("CV %", r"\mathrm{CV} = 100\,\frac{s}{\bar{x}}"),
    "median": ("median", r"\mathrm{median}"),
    "min": ("min", r"\min"),
    "max": ("max", r"\max"),
    "ci95": ("95 % CI", r"\bar{x} \pm t_{0.975,\,n-1}\,\frac{s}{\sqrt{n}}"),
}


def _run_summarize(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    df = inputs["table"]
    by = _cols(df, p.get("by"), "group by")
    values = _cols(df, p.get("values"), "values")
    if not values:
        raise StepError("Say which number columns to summarize (values).")
    want = p.get("stats") or ["n", "mean", "sd"]
    bad = [s for s in want if s not in STATS]
    if bad:
        raise StepError(f"Unknown statistic {bad}. Choose from {', '.join(STATS)}.")
    rows = []
    groups = df.groupby(by, sort=False, dropna=False) if by else [((), df)]
    for key, g in groups:
        key = key if isinstance(key, tuple) else (key,)
        for v in values:
            x = pd.to_numeric(g[v], errors="coerce").dropna()
            n, m, s = len(x), x.mean(), x.std(ddof=1)
            row = {**dict(zip(by, key)), "value": v}
            for st in want:
                if st == "n":
                    row["n"] = n
                elif st == "mean":
                    row["mean"] = m
                elif st == "sd":
                    row["sd"] = s
                elif st == "sem":
                    row["sem"] = s / np.sqrt(n) if n else np.nan
                elif st == "cv":
                    row["cv_pct"] = 100 * s / abs(m) if m else np.nan
                elif st == "median":
                    row["median"] = x.median()
                elif st == "min":
                    row["min"] = x.min()
                elif st == "max":
                    row["max"] = x.max()
                elif st == "ci95":
                    h = stats.t.ppf(0.975, n - 1) * s / np.sqrt(n) if n > 1 else np.nan
                    row["ci95_low"], row["ci95_high"] = m - h, m + h
            rows.append(row)
    return StepOut({"table": pd.DataFrame(rows)}, {"groups": len(rows)})


register(StepType(
    id="table.summarize",
    title="Summarize by group",
    category="Tables",
    sentence="Calculates n, mean, SD and other statistics of number columns for each group.",
    inputs=[Port("table", "table", "Table")],
    outputs=[Port("table", "table", "Summary")],
    params=[Param("by", "columns", "Group by", []), Param("values", "columns", "Number columns", []),
            Param("stats", "list", "Statistics", ["n", "mean", "sd"], "Any of " + ", ".join(STATS))],
    run=_run_summarize,
    explain=lambda p: sheet(
        formulas=[{"label": STATS[s][0], "latex": STATS[s][1]} for s in (p.get("stats") or ["n", "mean", "sd"])
                  if s in STATS and s not in ("n", "median", "min", "max")],
        rules=[f"Groups: {', '.join(p.get('by') or []) or 'all rows together'}; missing values are left out."],
        code="table.groupby(by)[values].agg(...)"),
    methods=lambda p, f: f"Values were summarized per {', '.join(p.get('by') or ['dataset'])} "
                         f"({', '.join(STATS[s][0] for s in p.get('stats') or ['n', 'mean', 'sd'] if s in STATS)}).",
))


# ---------------------------------------------------------------------------------------------------- compare


TESTS = {
    "welch": "Welch's t-test",
    "student": "Student's t-test",
    "mannwhitney": "Mann-Whitney U test",
    "anova": "one-way ANOVA with Tukey's HSD",
    "kruskal": "Kruskal-Wallis test",
}


def _compare(g: pd.DataFrame, value: str, group: str, test: str, alpha: float) -> tuple[dict, list[dict]]:
    levels = [lv for lv in dict.fromkeys(g[group].dropna())]
    samples = [pd.to_numeric(g.loc[g[group] == lv, value], errors="coerce").dropna().to_numpy() for lv in levels]
    if len(levels) < 2 or any(len(s) < 2 for s in samples):
        raise StepError(f"Need at least two groups in '{group}' with two values each.")
    chosen = test
    if test == "auto":
        normal = all(len(s) < 3 or stats.shapiro(s).pvalue > 0.05 for s in samples)
        chosen = ("welch" if normal else "mannwhitney") if len(levels) == 2 else ("anova" if normal else "kruskal")
    if chosen in ("welch", "student", "mannwhitney") and len(levels) != 2:
        raise StepError(f"{TESTS[chosen]} compares exactly two groups; '{group}' has {len(levels)}. Use anova or "
                        "kruskal, or filter the rows first.")
    res: dict[str, Any] = {"test": TESTS[chosen], "groups": " vs ".join(map(str, levels)),
                           "n": "/".join(str(len(s)) for s in samples),
                           "means": "/".join(f"{s.mean():.4g}" for s in samples)}
    posthoc: list[dict] = []
    if chosen in ("welch", "student"):
        r = stats.ttest_ind(samples[0], samples[1], equal_var=chosen == "student")
        a, b = samples
        pooled = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
        res.update(statistic="t", value=float(r.statistic), df=float(r.df), p=float(r.pvalue),
                   effect="Cohen's d", effect_size=float((a.mean() - b.mean()) / pooled) if pooled else np.nan)
    elif chosen == "mannwhitney":
        r = stats.mannwhitneyu(samples[0], samples[1], alternative="two-sided")
        n1, n2 = len(samples[0]), len(samples[1])
        res.update(statistic="U", value=float(r.statistic), df=np.nan, p=float(r.pvalue),
                   effect="rank-biserial r", effect_size=float(1 - 2 * r.statistic / (n1 * n2)))
    elif chosen == "anova":
        r = stats.f_oneway(*samples)
        allv = np.concatenate(samples)
        ssb = sum(len(s) * (s.mean() - allv.mean()) ** 2 for s in samples)
        sst = ((allv - allv.mean()) ** 2).sum()
        res.update(statistic="F", value=float(r.statistic), df=f"{len(samples) - 1}, {len(allv) - len(samples)}",
                   p=float(r.pvalue), effect="eta squared", effect_size=float(ssb / sst) if sst else np.nan)
        t = stats.tukey_hsd(*samples)
        for i in range(len(levels)):
            for j in range(i + 1, len(levels)):
                posthoc.append({"group_a": levels[i], "group_b": levels[j],
                                "difference": float(samples[i].mean() - samples[j].mean()),
                                "p_adjusted": float(t.pvalue[i, j])})
    else:
        r = stats.kruskal(*samples)
        res.update(statistic="H", value=float(r.statistic), df=len(samples) - 1, p=float(r.pvalue),
                   effect="", effect_size=np.nan)
    res["significant"] = bool(res["p"] < alpha)
    return res, posthoc


def _run_compare(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    df = inputs["table"]
    value = _cols(df, [p.get("value")], "value")[0] if p.get("value") else None
    group = _cols(df, [p.get("group")], "group")[0] if p.get("group") else None
    if not value or not group:
        raise StepError("Say which column holds the values and which says the group.")
    test = p.get("test") or "auto"
    if test not in TESTS and test != "auto":
        raise StepError(f"Unknown test '{test}'. Tests: auto, {', '.join(TESTS)}.")
    alpha = float(p.get("alpha") or 0.05)
    each = _cols(df, p.get("for_each"), "for each")
    rows, post = [], []
    parts = df.groupby(each, sort=False) if each else [((), df)]
    for key, g in parts:
        key = key if isinstance(key, tuple) else (key,)
        r, ph = _compare(g, value, group, test, alpha)
        rows.append({**dict(zip(each, key)), **r})
        post += [{**dict(zip(each, key)), **x} for x in ph]
    res = pd.DataFrame(rows)
    return StepOut({"results": res, "posthoc": pd.DataFrame(post)},
                   {"first": rows[0] if rows else None, "tests": sorted(set(res.test)) if len(res) else [],
                    "alpha": alpha, "comparisons": len(rows)})


FORMULAS = {
    "welch": r"t = \frac{\bar{x}_1 - \bar{x}_2}{\sqrt{\frac{s_1^2}{n_1} + \frac{s_2^2}{n_2}}}",
    "student": r"t = \frac{\bar{x}_1 - \bar{x}_2}{s_p\sqrt{\frac{1}{n_1} + \frac{1}{n_2}}}, \quad "
               r"s_p^2 = \frac{(n_1 - 1)s_1^2 + (n_2 - 1)s_2^2}{n_1 + n_2 - 2}",
    "mannwhitney": r"U = n_1 n_2 + \frac{n_1(n_1 + 1)}{2} - R_1",
    "anova": r"F = \frac{\sum_j n_j(\bar{x}_j - \bar{x})^2 / (k - 1)}{\sum_j \sum_i (x_{ij} - \bar{x}_j)^2 / (N - k)}",
    "kruskal": r"H = \frac{12}{N(N+1)}\sum_j \frac{R_j^2}{n_j} - 3(N+1)",
}


def _compare_explain(p: dict) -> dict:
    test = p.get("test") or "auto"
    chosen = list(TESTS) if test == "auto" else [test]
    return sheet(
        formulas=[{"label": TESTS[t], "latex": FORMULAS[t]} for t in chosen if t in FORMULAS],
        rules=([
            "Test chosen per comparison: Shapiro-Wilk on each group (p > 0.05 taken as normal); two groups: "
            "Welch's t-test if all look normal, else Mann-Whitney; three or more: one-way ANOVA (with Tukey's HSD) "
            "if all look normal, else Kruskal-Wallis."] if test == "auto" else [])
        + [f"Values: {p.get('value')}; groups: {p.get('group')}"
           + (f"; repeated for each {', '.join(p['for_each'])}" if p.get("for_each") else "") + ".",
           f"Significance level α = {p.get('alpha') or 0.05}; p-values are two-sided. A p-value is not the size of "
           "the effect: read the effect size and the means too."],
        assumptions=["Independent observations (replicates are separate cultures, not repeated injections).",
                     "t-tests and ANOVA: roughly normal values per group; Student's t and ANOVA also assume equal "
                     "variances (Welch's test does not)."],
        code="scipy.stats.ttest_ind / mannwhitneyu / f_oneway + tukey_hsd / kruskal",
    )


register(StepType(
    id="stats.compare_groups",
    title="Compare groups",
    category="Statistics",
    sentence="Tests whether a value differs between groups (t-test, Mann-Whitney, ANOVA or Kruskal-Wallis), with "
             "effect sizes, optionally repeated for each subset.",
    inputs=[Port("table", "table", "Table")],
    outputs=[Port("results", "table", "Test results"), Port("posthoc", "table", "Pairwise comparisons")],
    params=[Param("value", "text", "Value column", None), Param("group", "text", "Group column", None),
            Param("test", "choice", "Test", "auto", choices=["auto", *TESTS]),
            Param("for_each", "columns", "Repeat for each", []),
            Param("alpha", "number", "Significance level", 0.05)],
    run=_run_compare,
    explain=_compare_explain,
    methods=lambda p, f: (
        f"{p.get('value')} was compared between {p.get('group')} groups with "
        f"{' / '.join(f.get('tests') or ['the chosen test'])} (α = {f.get('alpha', 0.05)}, two-sided)."
    ),
    example=lambda p, f: [] if not f.get("first") else [{
        "label": f"{f['first']['test']}: {f['first']['groups']}",
        "latex": rf"{f['first']['statistic']} = {num(f['first']['value'])}, \quad p = {num(f['first']['p'], 3)}"
                 + (rf", \quad {tex_name(f['first']['effect'])} = {num(f['first']['effect_size'], 3)}"
                    if f["first"].get("effect") else "")}],
))

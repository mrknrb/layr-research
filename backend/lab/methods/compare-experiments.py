"""Compare a result across experiments

One table that several experiments made (their scores, rates, measurements...), compared side by side: for each
experiment the number of values, the mean, the standard deviation and the standard error of one column, how far each
experiment's mean is from the first one's, and a bar chart with error bars. With a grouping column, the same within
each group. Pick the experiments and their table in the experiment's inputs.
"""

import numpy as np
import pandas as pd

from toolbox import flag, flags_table, plot_bars

# The experiment's own pages, made of cards (the panel's page builder writes this; it can be edited here).
PAGES = [
    {
        "title": "Overview",
        "cards": [
            {"card": "number", "table": "summary", "column": "experiment", "summary": "count",
             "title": "Rows compared", "width": "half"},
            {"card": "number", "table": "summary", "column": "n", "summary": "sum", "title": "Values in all",
             "width": "half"},
            {"card": "figure", "figure": "chart", "title": "Mean per experiment (error bars: standard error)"},
            {"card": "table", "table": "summary", "title": "Per experiment"},
            {"card": "table", "table": "differences", "title": "Differences from the first experiment"},
            {"card": "table", "table": "flags", "title": "Quality checks"},
        ],
    },
]


# ---------------------------------------------------------------- formulas: one calculation each, on numbers


def standard_error(sd, n):
    """The standard error of a mean, from the standard deviation and the number of values."""
    return sd / n ** 0.5


def difference(mean, first_mean):
    """How far a mean is from the first experiment's mean."""
    return mean - first_mean


def percent_difference(mean, first_mean):
    """The difference from the first experiment's mean, in percent of it."""
    return 100 * (mean - first_mean) / abs(first_mean)


# ---------------------------------------------------------------- steps: tables in, a table out


def summarise(results, column, group_column):
    """For each experiment (and group): how many values the column has, their mean, standard deviation and standard
    error."""
    if column not in results.columns:
        raise ValueError(f"None of the picked tables has a column {column!r}. Their columns: "
                         f"{', '.join(c for c in results.columns if c not in ('experiment', 'experiment_id'))}. "
                         "Pick another column in the settings.")
    keys = ["experiment"] + ([group_column] if group_column else [])
    rows = []
    for key, part in results.groupby(keys, sort=False, dropna=False):
        values = pd.to_numeric(part[column], errors="coerce").dropna()
        sd = values.std(ddof=1) if len(values) > 1 else np.nan
        row = dict(zip(keys, key if isinstance(key, tuple) else (key,)))
        row.update({"n": len(values), "mean": values.mean() if len(values) else np.nan, "sd": sd,
                    "se": standard_error(sd, len(values)) if len(values) > 1 else np.nan})
        rows.append(row)
    return pd.DataFrame(rows, columns=keys + ["n", "mean", "sd", "se"])


def differences_from_first(summary, group_column):
    """Each experiment's mean against the first experiment's mean (within the same group, with a grouping column)."""
    first_experiment = summary["experiment"].iloc[0] if len(summary) else None
    rows = []
    for row in summary.to_dict("records"):
        same = summary[summary["experiment"] == first_experiment]
        if group_column:
            same = same[same[group_column] == row[group_column]]
        first_mean = same["mean"].iloc[0] if len(same) else np.nan
        out = {"experiment": row["experiment"]}
        if group_column:
            out[group_column] = row[group_column]
        out.update({"mean": row["mean"], "first_mean": first_mean, "difference": difference(row["mean"], first_mean),
                    "difference_pct": percent_difference(row["mean"], first_mean) if first_mean else np.nan})
        rows.append(out)
    return pd.DataFrame(rows)


def comparison_chart(summary, column, group_column):
    """A bar for each experiment's mean (side by side per group), with the standard error as error bars."""
    shown = summary.dropna(subset=["mean"])
    if shown.empty:
        raise ValueError(f"No experiment has numbers in the column {column!r}: nothing to draw.")
    return plot_bars(shown, "experiment", "mean", by=group_column, error="se", title=f"Mean {column} per experiment")


def comparison_checks(results, column):
    """Quality checks: only one experiment picked, an experiment without the column, too few values for a spread."""
    flags = []
    experiments = list(dict.fromkeys(results["experiment"]))
    if len(experiments) < 2:
        flags.append(flag("warning", "one experiment", "only one experiment is picked: there is nothing to compare "
                                                       "it with"))
    for name in experiments:
        part = results[results["experiment"] == name]
        values = pd.to_numeric(part[column], errors="coerce").dropna()
        if values.empty:
            flags.append(flag("warning", "missing column", f"has no numbers in the column {column!r}",
                              experiment=name))
        elif len(values) < 2:
            flags.append(flag("info", "few values", "has only one value: no spread or standard error",
                              experiment=name))
    return flags_table(flags)


# ---------------------------------------------------------------- the recipe: the steps in order


def compare_experiments(results, column, group_column=None):
    """Compare a result across experiments.

    Inputs:
        results: The table to compare, from each experiment picked [result]

    Settings:
        column (text): Column to compare. The column with the numbers, the same in every experiment's table.
        group_column (text): Grouping column. Optional: compare within each group of this column too (e.g. a
            treatment or a region).

    Returns:
        summary: Per experiment
        differences: Differences from the first experiment
        chart: Mean per experiment
        flags: Quality flags
    """
    summary = summarise(results, column, group_column)
    differences = differences_from_first(summary, group_column)
    chart = comparison_chart(summary, column, group_column)
    flags = comparison_checks(results, column)
    return {"summary": summary, "differences": differences, "chart": chart, "flags": flags}

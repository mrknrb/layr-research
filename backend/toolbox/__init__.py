"""The toolbox: ready-made functions every method can use. Read them; to change one, copy it into your method.

    from toolbox import read_table, fit_line, summarize

- Reading raw data: `read_table` (CSV, TSV, Excel, SPSS, Stata, SAS, JSON).
- Lines: `fit_line`, `fit_line_through_zero` (each gives a `Line`: slope, intercept, se, r2, s_yx, n).
- Names: `match_name` (a sample name's role and parts, from patterns).
- Quality flags: `flag`, `flags_table`, `combine_flags`.
- Ready-made steps (a table in, a table out): `pick_rows`, `pick_columns`, `rename_column`, `sort_rows`,
  `add_column` (a column from a formula of the method), `join_tables`, `summarize` (n, mean, sd per group),
  `fit_lines` (a straight line per group); charts `plot_lines` and `plot_bars`.

Add-ons for one kind of data or instrument are the toolbox's sub-packages (`from toolbox.<add-on> import ...`); the
plugin finds them by their folders (`registry.addons()`), the toolbox itself never imports them.

A function you define in your method with the same name as a toolbox function is used instead of it.
"""

from . import cache  # noqa: F401  (remembering slow results; the calculation process tells it where)
from .blocks import (add_column, fit_lines, join_tables, pick_columns, pick_rows, plot_bars, plot_lines,
                     rename_column, sort_rows, summarize)
from .fit import Line, fit_line, fit_line_through_zero
from .names import match_name
from .registry import register_curve_reader, register_reader  # noqa: F401
from .tables import combine_flags, flag, flags_table, read_table

__all__ = [
    "read_table",
    "fit_line", "fit_line_through_zero", "Line",
    "match_name",
    "flag", "flags_table", "combine_flags",
    "pick_rows", "pick_columns", "rename_column", "sort_rows", "add_column", "join_tables", "summarize", "fit_lines",
    "plot_lines", "plot_bars",
]
# Not in __all__ (they are for add-ons, not methods): register_reader, register_curve_reader (see registry.py).

"""The toolbox: ready-made functions every method can use. Read them; to change one, copy it into your method.

    from toolbox import read_gaml, integrate_peak, fit_line

- Reading raw data: `read_gaml` (GC sequences), `read_table` (CSV, TSV, Excel), `vendor_peak`.
- Peaks: `integrate_peak` (find and measure a compound's peak), `integrate_by_hand`, `PeakSettings`.
- Lines: `fit_line`, `fit_line_through_zero` (each gives a `Line`: slope, intercept, se, r2, s_yx, n).
- Names: `match_name` (a sample name's role and parts, from patterns).
- Quality flags: `flag`, `flags_table`, `combine_flags`.
- Figures: `plot_time_courses`.

A function you define in your method with the same name as a toolbox function is used instead of it.
"""

from .fit import Line, fit_line, fit_line_through_zero
from .gaml import Injection, read_gaml, vendor_peak
from .names import match_name
from .peaks import PeakSettings, integrate_by_hand, integrate_peak
from .plots import plot_time_courses
from .tables import combine_flags, flag, flags_table, read_table

__all__ = [
    "read_gaml", "vendor_peak", "Injection", "read_table",
    "integrate_peak", "integrate_by_hand", "PeakSettings",
    "fit_line", "fit_line_through_zero", "Line",
    "match_name",
    "flag", "flags_table", "combine_flags",
    "plot_time_courses",
]

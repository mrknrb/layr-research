"""Straight lines through points: calibration lines, initial rates."""

from __future__ import annotations

from typing import NamedTuple

import numpy as np


class Line(NamedTuple):
    """A fitted straight line y = slope · x + intercept, and how well it fits."""

    slope: float
    intercept: float
    se: float  # standard error of the slope
    r2: float  # coefficient of determination, R²
    s_yx: float  # scatter of the points around the line (residual standard deviation)
    n: int  # number of points


NO_LINE = float("nan")


def fit_line(x, y) -> Line:
    """Fit the straight line y = slope · x + intercept through points by least squares.

    It needs at least two different x values; with fewer, every number of the line is NaN (nothing can be fitted).
    With exactly two points the line goes through both and its standard error is 0. When all y values are the same
    (for example every replicate not detected) the slope is 0 and R² and the standard error are NaN, as in SciPy's
    linregress: there is no scatter to measure.

    Formula:
        slope = sum((x - x_mean) * (y - y_mean)) / sum((x - x_mean) ** 2)
        intercept = y_mean - slope * x_mean
        s_yx = sqrt(sum((y - (slope * x + intercept)) ** 2) / (n - 2))
        se = s_yx / sqrt(sum((x - x_mean) ** 2))
        r2 = 1 - sum((y - (slope * x + intercept)) ** 2) / sum((y - y_mean) ** 2)
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if len(np.unique(x)) < 2:
        return Line(NO_LINE, NO_LINE, NO_LINE, NO_LINE, NO_LINE, n)
    x_mean = x.mean()
    y_mean = y.mean()
    sxx = np.sum((x - x_mean) ** 2)
    slope = np.sum((x - x_mean) * (y - y_mean)) / sxx
    intercept = y_mean - slope * x_mean
    residuals = y - (slope * x + intercept)
    ss_res = np.sum(residuals**2)
    ss_tot = np.sum((y - y_mean) ** 2)
    s_yx = np.sqrt(ss_res / max(n - 2, 1))
    if ss_tot == 0:
        se = NO_LINE
    elif n > 2:
        se = s_yx / np.sqrt(sxx)
    else:
        se = 0.0
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else NO_LINE
    return Line(float(slope), float(intercept), float(se), float(r2), float(s_yx), n)


def fit_line_through_zero(x, y) -> Line:
    """Fit the straight line y = slope · x through the origin (intercept 0) by least squares.

    Formula:
        slope = sum(x * y) / sum(x ** 2)
        s_yx = sqrt(sum((y - slope * x) ** 2) / (n - 1))
        se = s_yx / sqrt(sum(x ** 2))
        r2 = 1 - sum((y - slope * x) ** 2) / sum((y - y_mean) ** 2)
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    sxx = np.sum(x**2)
    if n < 1 or sxx == 0:
        return Line(NO_LINE, 0.0, NO_LINE, NO_LINE, NO_LINE, n)
    slope = np.sum(x * y) / sxx
    residuals = y - slope * x
    ss_res = np.sum(residuals**2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    s_yx = np.sqrt(ss_res / max(n - 1, 1))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else NO_LINE
    return Line(float(slope), 0.0, float(s_yx / np.sqrt(sxx)), float(r2), float(s_yx), n)

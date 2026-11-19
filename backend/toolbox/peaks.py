"""Finding and measuring one compound's peak in a chromatogram.

The rules are deliberately simple, so every number can be traced:

1. A peak counts as compound X only if its apex lies within the retention time tolerance of X's expected time and
   its width is plausible compared with X in the standards. Another peak nearby is never taken instead: that keeps
   an impurity from being reported as product.
2. The peak's start and end are found by walking outward from the apex on a smoothed trace until the signal stops
   falling (a valley, or the return to the baseline).
3. The baseline is the straight line between start and end (valley to valley).
4. The area is the trapezoidal integral of (raw signal - baseline), in pA·min (the unit of Chromeleon's reports).
5. Signal-to-noise uses a robust estimate of the high-frequency noise. Below the detection limit (S/N < lod_snr)
   the compound is "n.d." with area 0; below the quantification limit (S/N < loq_snr) it is "<LOQ".
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import NamedTuple

import numpy as np
from scipy.signal import find_peaks, peak_widths, savgol_filter

from . import cache
from .gaml import Injection


class PeakSettings(NamedTuple):
    """How peaks are found and measured (the defaults are gcassay's)."""

    smoothing_points: int = 11  # Savitzky-Golay window, an odd number of points (11 points = 0.2 s at 50 Hz)
    max_half_width_min: float = 0.15  # never integrate further than this from the apex
    lod_snr: float = 3.0  # below this signal-to-noise: not detected
    loq_snr: float = 10.0  # below this: detected but not quantifiable
    search_halfwindow_min: float = 0.5  # the stretch around the expected time used for noise and the search
    fwhm_ratio_range: tuple[float, float] = (0.4, 3.0)  # accepted peak widths, times the width in the standards


@dataclass
class Peak:
    compound: str
    status: str  # "ok", "<LOQ", "n.d.", "manual" (or "excluded", set by a method)
    rt_expected: float
    rt: float = np.nan
    start: float = np.nan
    end: float = np.nan
    area: float = 0.0  # pA·min
    height: float = 0.0  # pA above the baseline
    fwhm: float = np.nan  # width at half height, min
    noise: float = np.nan  # pA
    snr: float = 0.0
    baseline_start_y: float = np.nan
    baseline_end_y: float = np.nan
    note: str = ""


def robust_noise(y: np.ndarray, smooth_points: int = 51) -> float:
    """The high-frequency noise of a trace (pA): a robust standard deviation of the signal around a heavy smooth.

    Formula:
        noise = 1.4826 * median(abs(r - median(r)))
    """
    n = min(smooth_points, (len(y) // 2) * 2 - 1)
    if n < 5:
        return float(np.std(y))
    resid = y - savgol_filter(y, n, 2)
    mad = np.median(np.abs(resid - np.median(resid)))
    return float(max(1.4826 * mad, 1e-9))


def walk_to_peak_end(ys: np.ndarray, apex: int, step: int, limit: int, k: int) -> int:
    """From the apex, walk (step -1: left, +1: right) while the smoothed signal keeps falling; give where it stops.

    It stops at a valley (the signal rises again for the next k points), at `limit` points from the apex, or at the
    end of the trace.
    """
    i = apex
    while True:
        nxt = i + step
        if nxt < 0 or nxt >= len(ys) or abs(nxt - apex) > limit:
            return i
        if ys[nxt] >= ys[i]:
            ahead = ys[nxt : nxt + step * k : step] if step > 0 else ys[max(nxt - k + 1, 0) : nxt + 1]
            if len(ahead) == 0 or ys[i] <= ahead.min():
                return i
        i = nxt


def measure_between(x, y, ys, compound, rt_expected, i0, i1, apex, noise, status, note="") -> Peak:
    """Measure a peak between two points of a trace: area above the straight baseline, height, width, S/N.

    Formula:
        b(t) = y_start + (y_end - y_start) / (t_end - t_start) * (t - t_start)
        area = sum((y_i - b_i + y_next - b_next) / 2 * (t_next - t_i))
        snr = height / noise
    """
    xb = np.array([x[i0], x[i1]])
    yb = np.array([ys[i0], ys[i1]])
    seg = slice(i0, i1 + 1)
    base = np.interp(x[seg], xb, yb)
    area = float(np.trapezoid(y[seg] - base, x[seg]))
    if apex is None:
        apex = i0 + int(np.argmax(ys[seg] - base))
    height = float(ys[apex] - np.interp(x[apex], xb, yb))
    half = height / 2 + np.interp(x[seg], xb, yb)
    above = np.where(ys[seg] >= half)[0]
    fwhm = float(x[i0 + above[-1]] - x[i0 + above[0]]) if len(above) > 1 else np.nan
    return Peak(compound=compound, status=status, rt_expected=rt_expected, rt=float(x[apex]), start=float(x[i0]),
                end=float(x[i1]), area=area, height=height, fwhm=fwhm, noise=noise,
                snr=height / noise if noise > 0 else np.inf, baseline_start_y=float(ys[i0]),
                baseline_end_y=float(ys[i1]), note=note)


def pick_apex(xw, ys, peaks, widths, rt_expected, rt_tolerance_min, fwhm_expected_min, settings) -> int | None:
    """Of the peaks found, the one that counts as the compound: nearest the expected time, within the tolerance,
    and (when the standards' width is known) of a plausible width. None when no peak qualifies."""
    candidates = []
    for p, wd in zip(peaks, widths):
        drt = abs(xw[p] - rt_expected)
        if drt > rt_tolerance_min:
            continue
        low, high = settings.fwhm_ratio_range
        if fwhm_expected_min and not (low * fwhm_expected_min <= wd <= high * fwhm_expected_min):
            continue
        candidates.append((drt, -ys[p], p))
    return min(candidates)[2] if candidates else None


def find_and_measure(x, y, compound, rt_expected, rt_tolerance_min, settings, fwhm_expected_min=None) -> Peak:
    """Find a compound's peak near its expected time and measure it (the rules at the top of this file)."""
    half_window = float(settings.search_halfwindow_min)
    smoothing = int(settings.smoothing_points)
    lod, loq = float(settings.lod_snr), float(settings.loq_snr)
    w = (x >= rt_expected - half_window) & (x <= rt_expected + half_window)
    idx = np.flatnonzero(w)
    xw, yw = x[idx], y[idx]
    ys = savgol_filter(yw, smoothing, 3)
    noise = robust_noise(yw)
    dt = float(np.median(np.diff(xw)))

    peaks, _ = find_peaks(ys, prominence=lod * noise)
    if len(peaks) == 0:
        return Peak(compound, "n.d.", rt_expected, noise=noise, note="no peak above LOD in search window")
    widths = peak_widths(ys, peaks, rel_height=0.5)[0] * dt
    apex = pick_apex(xw, ys, peaks, widths, rt_expected, rt_tolerance_min, fwhm_expected_min, settings)
    if apex is None:
        near = peaks[np.argmin(np.abs(xw[peaks] - rt_expected))]
        return Peak(compound, "n.d.", rt_expected, noise=noise,
                    note=f"nearest peak at {xw[near]:.3f} min is outside the RT/width acceptance window")
    limit = int(float(settings.max_half_width_min) / dt)
    i0 = walk_to_peak_end(ys, apex, -1, limit, smoothing)
    i1 = walk_to_peak_end(ys, apex, +1, limit, smoothing)
    peak = measure_between(xw, yw, ys, compound, rt_expected, i0, i1, apex, noise, "ok")
    if peak.snr < lod:
        peak.status, peak.area, peak.note = "n.d.", 0.0, "S/N below LOD after baseline correction"
    elif peak.snr < loq:
        peak.status = "<LOQ"
    return peak


def measure_by_hand(x, y, compound, rt_expected, start_min, end_min, settings, note) -> Peak:
    """Measure a peak between boundaries a person set (the baseline is drawn between them)."""
    w = (x >= start_min - 0.05) & (x <= end_min + 0.05)
    idx = np.flatnonzero(w)
    xw, yw = x[idx], y[idx]
    ys = savgol_filter(yw, int(settings.smoothing_points), 3)
    i0 = int(np.searchsorted(xw, start_min))
    i1 = int(np.searchsorted(xw, end_min))
    half = float(settings.search_halfwindow_min)
    noise = robust_noise(y[(x >= rt_expected - half) & (x <= rt_expected + half)])
    return measure_between(xw, yw, ys, compound, rt_expected, i0, i1, None, noise, "manual", note)


def _known_width(fwhm_expected_min: float | None) -> float | None:
    if fwhm_expected_min is None or math.isnan(float(fwhm_expected_min)):
        return None
    return float(fwhm_expected_min)


def integrate_peak(injection: Injection, compound: str, rt_expected_min: float, rt_tolerance_min: float,
                   settings: PeakSettings = PeakSettings(), fwhm_expected_min: float | None = None) -> dict:
    """Find a compound's peak at its expected retention time in one injection and measure its area.

    Gives one row: compound, status (ok, <LOQ, n.d.), rt_expected, rt, start, end, area (pA·min), height, fwhm,
    noise, snr, baseline_start_y, baseline_end_y and a note. `fwhm_expected_min` (the width in the standards), when
    known, also rules out peaks of an implausible width. Results are remembered (see toolbox/cache.py).
    """
    width = _known_width(fwhm_expected_min)
    settings = PeakSettings(*settings)

    def measure() -> dict:
        peak = find_and_measure(injection.time_min, injection.signal_pa, compound, rt_expected_min,
                                float(rt_tolerance_min), settings, width)
        return asdict(peak)

    if not injection.key:
        return measure()
    key = cache.fingerprint("auto", injection.number, compound, round(float(rt_expected_min), 9),
                            float(rt_tolerance_min), width, tuple(settings))
    return dict(cache.remember(f"peaks-{injection.key.split(':')[0]}", key, measure))


def integrate_by_hand(injection: Injection, compound: str, rt_expected_min: float, start_min: float, end_min: float,
                      settings: PeakSettings = PeakSettings(), note: str = "") -> dict:
    """Measure a compound's peak between a start and an end set by hand (status "manual"; the note says why)."""
    settings = PeakSettings(*settings)

    def measure() -> dict:
        peak = measure_by_hand(injection.time_min, injection.signal_pa, compound, rt_expected_min, float(start_min),
                               float(end_min), settings, note)
        return asdict(peak)

    if not injection.key:
        return measure()
    key = cache.fingerprint("manual", injection.number, compound, round(float(rt_expected_min), 9), float(start_min),
                            float(end_min), note, tuple(settings))
    return dict(cache.remember(f"peaks-{injection.key.split(':')[0]}", key, measure))

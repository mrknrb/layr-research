"""Peak finding and integration for a single target compound.

Rules (deliberately simple and explainable, so every number can be traced):

1. Identification is RT-locked: a peak is only accepted as compound X if its apex lies
   within `rt_tolerance` of X's reference RT (taken from the calibration standards of
   the same sequence) and its width is plausible compared with X in the standards.
   This is what prevents a neighbouring impurity from being reported as product.
2. Peak start/end are found by walking outward from the apex on a smoothed trace until
   the signal stops falling (a valley or the return to baseline).
3. Baseline is a straight line between start and end (valley-to-valley).
4. Area = trapezoidal integral of (raw signal - baseline), in pA*min (same unit as the
   Chromeleon reports used in the Excel workbooks).
5. Signal-to-noise uses a robust estimate of the local high-frequency noise.
   S/N < lod_snr -> "not detected" (area 0).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import find_peaks, peak_widths, savgol_filter


@dataclass
class IntegrationParams:
    rt_tolerance_min: float = 0.012
    search_halfwindow_min: float = 0.5  # region used for noise + peak search context
    smoothing_points: int = 11  # Savitzky-Golay window (odd); 11 pts = 0.2 s at 50 Hz
    max_half_width_min: float = 0.15  # never integrate further than this from the apex
    lod_snr: float = 3.0
    loq_snr: float = 10.0
    fwhm_ratio_range: tuple[float, float] = (0.4, 3.0)  # vs. FWHM in the standards


@dataclass
class PeakResult:
    compound: str
    status: str  # "ok", "<LOQ", "n.d.", "manual", "excluded"
    rt_expected: float
    rt: float = np.nan
    start: float = np.nan
    end: float = np.nan
    area: float = 0.0  # pA*min
    height: float = 0.0  # pA above baseline
    fwhm: float = np.nan  # min
    noise: float = np.nan  # pA
    snr: float = 0.0
    baseline_start_y: float = np.nan
    baseline_end_y: float = np.nan
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def robust_noise(y: np.ndarray, smooth_points: int = 51) -> float:
    """High-frequency noise (pA): robust SD of the signal around a heavy smooth."""
    n = min(smooth_points, (len(y) // 2) * 2 - 1)
    if n < 5:
        return float(np.std(y))
    resid = y - savgol_filter(y, n, 2)
    mad = np.median(np.abs(resid - np.median(resid)))
    return float(max(1.4826 * mad, 1e-9))


def _walk(ys: np.ndarray, apex: int, step: int, limit: int, k: int) -> int:
    """Walk from the apex until the smoothed signal stops decreasing for k points."""
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


def _integrate_between(x, y, ys, compound, rt_exp, i0, i1, apex, noise, status, note=""):
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
    return PeakResult(
        compound=compound,
        status=status,
        rt_expected=rt_exp,
        rt=float(x[apex]),
        start=float(x[i0]),
        end=float(x[i1]),
        area=area,
        height=height,
        fwhm=fwhm,
        noise=noise,
        snr=height / noise if noise > 0 else np.inf,
        baseline_start_y=float(ys[i0]),
        baseline_end_y=float(ys[i1]),
        note=note,
    )


def integrate_compound(
    x: np.ndarray,
    y: np.ndarray,
    compound: str,
    rt_expected: float,
    params: IntegrationParams,
    fwhm_expected: float | None = None,
    rt_tolerance: float | None = None,
) -> PeakResult:
    tol = params.rt_tolerance_min if rt_tolerance is None else rt_tolerance
    w = (x >= rt_expected - params.search_halfwindow_min) & (x <= rt_expected + params.search_halfwindow_min)
    idx = np.flatnonzero(w)
    xw, yw = x[idx], y[idx]
    ys = savgol_filter(yw, params.smoothing_points, 3)
    noise = robust_noise(yw)
    dt = float(np.median(np.diff(xw)))

    peaks, props = find_peaks(ys, prominence=params.lod_snr * noise)
    if len(peaks) == 0:
        return PeakResult(compound, "n.d.", rt_expected, noise=noise, note="no peak above LOD in search window")
    widths = peak_widths(ys, peaks, rel_height=0.5)[0] * dt

    cand = []
    for p, wd in zip(peaks, widths):
        drt = abs(xw[p] - rt_expected)
        if drt > tol:
            continue
        if fwhm_expected and not (
            params.fwhm_ratio_range[0] * fwhm_expected <= wd <= params.fwhm_ratio_range[1] * fwhm_expected
        ):
            continue
        cand.append((drt, -ys[p], p))
    if not cand:
        near = peaks[np.argmin(np.abs(xw[peaks] - rt_expected))]
        return PeakResult(
            compound,
            "n.d.",
            rt_expected,
            noise=noise,
            note=f"nearest peak at {xw[near]:.3f} min is outside the RT/width acceptance window",
        )
    apex = min(cand)[2]
    limit = int(params.max_half_width_min / dt)
    k = params.smoothing_points
    i0 = _walk(ys, apex, -1, limit, k)
    i1 = _walk(ys, apex, +1, limit, k)
    res = _integrate_between(xw, yw, ys, compound, rt_expected, i0, i1, apex, noise, "ok")
    if res.snr < params.lod_snr:
        res.status, res.area, res.note = "n.d.", 0.0, "S/N below LOD after baseline correction"
    elif res.snr < params.loq_snr:
        res.status = "<LOQ"
    return res


def integrate_manual(
    x: np.ndarray, y: np.ndarray, compound: str, rt_expected: float, start: float, end: float,
    params: IntegrationParams, note: str,
) -> PeakResult:
    """Integrate between user-given boundaries (baseline drawn between them)."""
    w = (x >= start - 0.05) & (x <= end + 0.05)
    idx = np.flatnonzero(w)
    xw, yw = x[idx], y[idx]
    ys = savgol_filter(yw, params.smoothing_points, 3)
    i0 = int(np.searchsorted(xw, start))
    i1 = int(np.searchsorted(xw, end))
    half = params.search_halfwindow_min
    noise = robust_noise(y[(x >= rt_expected - half) & (x <= rt_expected + half)])
    return _integrate_between(xw, yw, ys, compound, rt_expected, i0, i1, None, noise, "manual", note)

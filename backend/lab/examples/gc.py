"""A made-up GC-FID activity assay (a GAML file), for the method gc-activity-assay.

Substrate SUB (3.62 min) is consumed by strain WT (first order, k = 0.015 min⁻¹) and becomes product PROD
(6.47 min) at 80 % yield; octanol (5.13 min) is the internal standard. Standards 0.1-2 mM in two series, two ether
blanks, samples at 0, 5, 15, 30 and 60 min in triplicate. An impurity at 6.53 min sits next to PROD, and the
instrument's own peak table calls it PROD in the early samples, as happens in real runs.
"""

from __future__ import annotations

import base64
from pathlib import Path

import numpy as np

ID = "gc"
TITLE = "A GC activity assay"
DESCRIPTION = "Peaks, calibration, concentrations and initial rates from a made-up GC-FID sequence"
METHOD = "gc-activity-assay"
EXPERIMENT = "Example: made-up GC activity assay"
ORDER = 2
SETTINGS = {
    "compounds": [{"compound": "SUB", "rt_min": 3.62}, {"compound": "octanol", "rt_min": 5.13},
                  {"compound": "PROD", "rt_min": 6.47}],
    "internal_standard": "octanol",
    "sample_patterns": [
        {"role": "sample", "regex": r"^Act\.Assay_(?P<strain>[A-Za-z0-9]+)_(?P<time_min>\d+)min_R(?P<replicate>\d+)_"
                                    r"(?P<substrate>[A-Z]+)$"},
        {"role": "standard", "regex": r"^GC_std_(?P<std_conc_mM>[\d.]+)mM (?P<std_series>[A-Z])$"},
        {"role": "blank", "regex": r"^ether$"},
    ],
    "cal_max_conc_mM": 2.0,
    "concentration_factor": 1.0,
    "rate_window_min": [0, 30],
    "biomass_gcdw_per_l": 0.5,
}
CORRECTIONS: list[dict] = []

HZ = 25.0
T0, T1 = 3.0, 7.2  # minutes recorded
SIGMA = 0.0068  # min (FWHM ~ 0.016 min)
RT = {"SUB": 3.62, "octanol": 5.13, "PROD": 6.47}
SLOPE = {"SUB": 1.15, "PROD": 0.85}  # area ratio per mM
IS_AREA = 2.5  # pA*min


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.asarray(a, "<f8").tobytes()).decode()


def _trace(rng, peaks: list[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(T0, T1, 1 / HZ / 60)
    y = 5.0 + 0.08 * (t - T0) + rng.normal(0, 0.02, t.size)
    for rt, area in peaks:
        if area > 0:
            y += area / (SIGMA * np.sqrt(2 * np.pi)) * np.exp(-0.5 * ((t - rt) / SIGMA) ** 2)
    return t, y


def _injections(rng) -> list[tuple[str, dict[str, float]]]:
    """(name, {compound: concentration mM}) in sequence order."""
    seq: list[tuple[str, dict[str, float]]] = [("ether", {})]
    for series in "AB":
        for c in (0.1, 0.25, 0.5, 1.0, 2.0):
            seq.append((f"GC_std_{c:g}mM {series}", {"SUB": c, "PROD": c}))
    seq.append(("ether", {}))
    for t in (0, 5, 15, 30, 60):
        for r in (1, 2, 3):
            sub = 1.2 * np.exp(-0.015 * t) * rng.normal(1, 0.03)
            prod = 0.8 * (1.2 - 1.2 * np.exp(-0.015 * t)) * rng.normal(1, 0.04)
            seq.append((f"Act.Assay_WT_{t}min_R{r}_SUB", {"SUB": sub, "PROD": max(prod, 0.0), "_imp": 0.04}))
    return seq


def write_gaml(path: Path, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<GAML version="1.20" name="Example activity assay">\n']
    for n, (name, conc) in enumerate(_injections(rng), 1):
        shift = rng.normal(0, 0.002)
        is_area = IS_AREA * rng.normal(1, 0.06)
        peaks = [(RT["octanol"] + shift, is_area)]
        vendor = [("octanol", RT["octanol"] + shift, is_area)]
        for comp in ("SUB", "PROD"):
            c = conc.get(comp, 0.0)
            area = SLOPE[comp] * c * is_area * rng.normal(1, 0.025)
            peaks.append((RT[comp] + shift, area))
            if area > 0.01:
                vendor.append((comp, RT[comp] + shift, area))
        if conc.get("_imp"):
            peaks.append((6.53 + shift, conc["_imp"] * is_area * 8))
            if conc.get("PROD", 0) < 0.06:  # the instrument's method picks the impurity as PROD
                vendor = [v for v in vendor if v[0] != "PROD"] + [("PROD", 6.53 + shift, conc["_imp"] * is_area * 8)]
        t, y = _trace(rng, peaks)
        vt = "".join(
            f'<peak name="{v[0]}"><peakXvalue>{v[1] * 60:.4f}</peakXvalue>'
            f'<parameter name="Peak_Area">{v[2] * 60:.6f}</parameter>'
            f'<parameter name="Peak_Height">{v[2] / (SIGMA * 2.5066):.4f}</parameter></peak>' for v in vendor)
        kind = "BLANK" if name == "ether" else ("STANDARD" if "std" in name else "SAMPLE")
        parts.append(
            f'<experiment name="{name}"><collectdate>2026-10-01T{8 + n // 4:02d}:{(n * 15) % 60:02d}:00Z'
            f'</collectdate><parameter name="type">{kind}</parameter>'
            f'<trace name="FID" technique="CHROM"><Xdata label="Seconds" units="SECONDS">'
            f'<values byteorder="INTEL" format="FLOAT64" numvalues="{t.size}">{_b64(t * 60)}</values>'
            f'<Ydata label="pA" units="PICOAMPERES"><values byteorder="INTEL" format="FLOAT64" '
            f'numvalues="{y.size}">{_b64(y)}</values></Ydata></Xdata><peaktable>{vt}</peaktable></trace>'
            "</experiment>\n")
    parts.append("</GAML>\n")
    path.write_text("".join(parts), "utf-8")


def files(folder: Path) -> dict[str, Path]:
    """The example sequence (always the same file, so it has the same checksum)."""
    path = folder / "Example activity assay.gaml"
    write_gaml(path)
    return {"gaml_file": path}

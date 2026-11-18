"""GC-FID step types: chromatograms in, quantified concentrations and quality checks out.

Ported from `gcassay` (Ragini's GC-FID activity assay package), split into explainable steps. The numbers must stay
identical to gcassay's: tests/test_lab_real.py compares them on the real run when that data is on the PC.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..core import Ctx, Param, Port, StepError, StepOut, StepType, num, register, sheet
from ..gaml import read_gaml
from ..integrate import IntegrationParams, integrate_compound, integrate_manual


class Sequence(list):
    """The injections of one GC sequence, with the key of the file they came from (for caching)."""

    key: str = ""


def _flag(level: str, category: str, injection: Any, compound: str, message: str) -> dict[str, Any]:
    return {"level": level, "category": category, "injection": injection, "compound": compound, "message": message}


def _approved(corrections: list[dict] | None, kind: str) -> list[dict]:
    return [c for c in corrections or [] if c.get("kind") == kind and c.get("status", "approved") == "approved"]


# ---------------------------------------------------------------------------------------------------- read GAML


def _run_read(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    f = inputs["file"]
    if not isinstance(f, dict) or not f.get("path"):
        raise StepError("The raw file input needs a GC sequence file (GAML) from the store.")
    path: Path = ctx.store_path(f["path"])
    if not path.exists():
        raise StepError(f"The raw file {f.get('name') or f['path']} is missing from the store.")
    channel = p.get("channel") or None
    key = ("gaml", f.get("sha256") or str(path), channel)
    seq = ctx.cache.get(key)
    if seq is None:
        ctx.progress(f"Reading {f.get('name') or path.name}")
        seq = Sequence(read_gaml(path, channel))
        seq.key = f"{f.get('sha256') or path}:{channel}"
        for k in [k for k in ctx.cache if isinstance(k, tuple) and k and k[0] == "gaml"]:
            del ctx.cache[k]  # keep one sequence in memory: they are large
        ctx.cache[key] = seq
    rows = [
        {"injection": i.number, "name": i.name, "collected": i.collected, "sample_type": i.sample_type,
         "points": int(i.time_min.size), "run_min": float(i.time_min[-1]) if i.time_min.size else np.nan}
        for i in seq
    ]
    table = pd.DataFrame(rows)
    dt = float(np.median(np.diff(seq[0].time_min))) * 60 if seq and seq[0].time_min.size > 1 else np.nan
    facts = {"injections": len(seq), "channel": channel or "first trace", "sampling_s": dt,
             "points": int(table.points.median()) if len(table) else 0}
    return StepOut({"injections": seq, "list": table}, facts)


register(StepType(
    id="gc.read_gaml",
    title="Read the chromatograms",
    category="GC",
    sentence="Reads every injection of a GC sequence (time and detector signal) from the instrument's GAML export.",
    inputs=[Port("file", "file", "GC sequence (GAML export from Chromeleon)")],
    outputs=[Port("injections", "injections", "Chromatograms"), Port("list", "table", "Injection list")],
    params=[Param("channel", "text", "Detector channel", None, "Trace name in the GAML file, e.g. BackDetectorFID; "
                  "empty: the first trace.")],
    run=_run_read,
    explain=lambda p: sheet(
        rules=[
            "The GAML file is the instrument's open XML export: one block per injection with the raw trace "
            "(x = retention time in seconds, y = FID signal in pA) and the injection's name and time.",
            "Times are converted to minutes. The raw file is never changed; it is stored with its SHA-256 checksum, "
            "and every run records that checksum.",
            "Chromeleon's own peak table is read only to compare with it later (step: quality checks).",
        ],
        code="lab.gaml.read_gaml(path, channel)  # xml.etree iterparse + base64 float64 arrays",
    ),
    example=lambda p, f: [{"label": f"{f.get('injections')} injections read, {f.get('points')} points each, one every "
                                    f"{num(f.get('sampling_s'), 2)} s ({f.get('channel')}).", "latex": ""}],
    methods=lambda p, f: (
        f"Raw FID traces of {f.get('injections', 'all')} injections were read from the Chromeleon GAML export "
        f"({f.get('channel', 'first trace')}, one point every {num(f.get('sampling_s'), 2)} s)."
    ),
))


# ---------------------------------------------------------------------------------------------------- names


META_COLUMNS = ["injection", "name", "original_name", "role", "strain", "time_min", "replicate", "substrate",
                "std_conc_mM", "std_series", "collected"]


def parse_names(injections, patterns: list[dict], renames: dict[int, str]) -> pd.DataFrame:
    compiled = []
    for pat in patterns:
        try:
            compiled.append((pat["role"], re.compile(pat["regex"])))
        except (KeyError, re.error) as e:
            raise StepError(f"Sample pattern {pat} is not usable: {e}") from e
    rows = []
    extra: list[str] = []
    for inj in injections:
        name = renames.get(inj.number, inj.name)
        row = {"injection": inj.number, "name": name, "original_name": inj.name, "role": "unknown",
               "collected": inj.collected}
        for role, rx in compiled:
            m = rx.match(name)
            if m:
                row["role"] = role
                for k, v in m.groupdict().items():
                    if v is not None:
                        row[k] = v
                        if k not in META_COLUMNS and k not in extra:
                            extra.append(k)
                break
        rows.append(row)
    df = pd.DataFrame(rows).reindex(columns=META_COLUMNS + extra)
    for col in ("time_min", "replicate", "std_conc_mM"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def check_design(meta: pd.DataFrame) -> list[dict]:
    flags: list[dict] = []

    def flag(level, inj, msg):
        flags.append(_flag(level, "design", inj, "", msg))

    for _, r in meta[meta.role == "unknown"].iterrows():
        flag("error", r.injection, f"Name '{r['name']}' matches no sample pattern - it is ignored.")
    s = meta[meta.role == "sample"]
    key = ["strain", "substrate", "time_min", "replicate"]
    for _, grp in s[s.duplicated(key, keep=False)].groupby(key):
        injs = ", ".join(f"#{i}" for i in grp.injection)
        flag("error", int(grp.injection.iloc[-1]),
             f"Duplicate sample name '{grp['name'].iloc[0]}' at injections {injs}.")
    seq = s.sort_values("injection").reset_index(drop=True)
    for i in range(1, len(seq) - 1):
        prev_s, cur, next_s = seq.substrate[i - 1], seq.substrate[i], seq.substrate[i + 1]
        if cur != prev_s and cur != next_s and prev_s == next_s:
            flag("error", int(seq.injection[i]),
                 f"'{seq.name[i]}' sits inside a block of {prev_s} samples - probably mislabelled "
                 f"(should it be {prev_s}?).")
    times = sorted(s.time_min.dropna().unique())
    reps = sorted(s.replicate.dropna().unique())
    for (strain, sub), grp in s.groupby(["strain", "substrate"]):
        have = set(zip(grp.time_min, grp.replicate))
        missing = [f"{int(t)}min R{int(r)}" for t in times for r in reps if (t, r) not in have]
        if missing:
            flag("warning", "", f"{strain}/{sub}: missing {', '.join(missing)}.")
    std = meta[meta.role == "standard"]
    for series, grp in std.groupby("std_series"):
        missing = sorted(set(std.std_conc_mM) - set(grp.std_conc_mM))
        if missing:
            flag("warning", "", f"Standard series {series} lacks concentrations {missing}.")
    return flags


def _run_names(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    renames = {int(c["injection"]): c["new_name"] for c in _approved(inputs.get("corrections"), "rename")}
    meta = parse_names(inputs["injections"], p.get("patterns") or [], renames)
    flags = check_design(meta)
    for c in _approved(inputs.get("corrections"), "rename"):
        flags.append(_flag("info", "correction", c["injection"], "",
                           f"renamed to '{c['new_name']}': {c.get('reason', '')}"))
    counts = meta.role.value_counts().to_dict()
    return StepOut({"samples": meta, "flags": pd.DataFrame(flags)},
                   {"roles": {str(k): int(v) for k, v in counts.items()}, "renamed": len(renames),
                    "patterns": p.get("patterns") or []}, flags)


register(StepType(
    id="gc.sample_names",
    title="Read the sample names",
    category="GC",
    sentence="Turns each injection's name into its design (sample, standard or blank; strain, time, replicate, "
             "substrate, standard concentration) and checks the design for typos, duplicates and gaps.",
    inputs=[Port("injections", "injections", "Chromatograms"),
            Port("corrections", "corrections", "Corrections (renames)", optional=True)],
    outputs=[Port("samples", "table", "Injection design"), Port("flags", "table", "Design checks")],
    params=[Param("patterns", "list", "Name patterns", [],
                  "One regular expression per role; named groups (?P<time_min>...) become columns. "
                  "Roles: sample, standard, blank.")],
    run=_run_names,
    explain=lambda p: sheet(
        rules=[
            "The injection names are the only place the design lives, so each name must match one of these "
            "patterns (first match wins): " + "; ".join(f"{x.get('role')}: `{x.get('regex')}`"
                                                      for x in p.get("patterns") or []) + ".",
            "Named parts of a pattern become columns (time_min, replicate, substrate, std_conc_mM, std_series ...).",
            "A rename correction replaces a wrong name before matching; it is listed with its reason.",
            "Checks: names matching no pattern (error), duplicate samples (error), a sample whose substrate "
            "differs from both neighbours (likely mislabelled, error), missing time points or replicates, "
            "standard series lacking a concentration (warnings).",
        ],
        code="re.match(pattern, name).groupdict()",
    ),
    example=lambda p, f: [{"label": "Roles found: " + ", ".join(f"{v} {k}" for k, v in (f.get("roles") or {}).items())
                                    + (f"; {f['renamed']} renamed by a correction" if f.get("renamed") else "") + ".",
                           "latex": ""}],
    methods=lambda p, f: (
        "Samples, standards and blanks were assigned from the injection names "
        f"({', '.join(f'{v} {k}' for k, v in (f.get('roles') or {}).items())})"
        + (f"; {f['renamed']} mislabelled injection(s) were renamed, with reasons recorded" if f.get("renamed")
           else "") + "."
    ),
))


# ---------------------------------------------------------------------------------------------------- RTs


def integration_params(p: dict) -> IntegrationParams:
    ip = IntegrationParams()
    for k in ("rt_tolerance_min", "smoothing_points", "max_half_width_min", "lod_snr", "loq_snr",
              "search_halfwindow_min"):
        if p.get(k) is not None:
            setattr(ip, k, type(getattr(ip, k))(p[k]))
    if p.get("fwhm_ratio_range"):
        ip.fwhm_ratio_range = tuple(p["fwhm_ratio_range"])
    return ip


INTEGRATION_PARAMS = [
    Param("rt_tolerance_min", "number", "Retention time tolerance", 0.012,
          "The apex must lie within this distance of the compound's reference time.", "min"),
    Param("smoothing_points", "integer", "Smoothing window", 11, "Savitzky-Golay window, odd number of points."),
    Param("max_half_width_min", "number", "Longest half-width", 0.15,
          "Never integrate further than this from the apex.", "min"),
    Param("lod_snr", "number", "Detection limit (S/N)", 3, "Below this signal-to-noise the peak is not detected."),
    Param("loq_snr", "number", "Quantification limit (S/N)", 10, "Below this the peak is reported as <LOQ."),
]


def _compounds(p: dict) -> dict[str, dict]:
    comps = p.get("compounds") or {}
    if not isinstance(comps, dict) or not comps:
        raise StepError("Give the compounds with their retention times, e.g. {\"HAME\": {\"rt\": 3.622}}.")
    for name, spec in comps.items():
        if not isinstance(spec, dict) or "rt" not in spec:
            raise StepError(f"Compound {name} needs an rt (expected retention time in minutes).")
    return comps


def _run_rts(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    injections, meta = inputs["injections"], inputs["samples"]
    comps = _compounds(p)
    ip = integration_params(p)
    std_ids = set(meta[meta.role == "standard"].injection)
    rows = []
    for comp, spec in comps.items():
        rts, fw = [], []
        tol = spec.get("rt_tolerance_min", ip.rt_tolerance_min)
        for inj in injections:
            if inj.number in std_ids:
                r = integrate_compound(inj.time_min, inj.signal_pa, comp, spec["rt"], ip, rt_tolerance=tol * 2.5)
                if r.status == "ok":
                    rts.append(r.rt)
                    fw.append(r.fwhm)
        rows.append({"compound": comp, "rt_config": float(spec["rt"]),
                     "rt_ref": float(np.median(rts)) if rts else float(spec["rt"]),
                     "fwhm_ref": float(np.median(fw)) if fw else np.nan,
                     "n_standards": len(rts), "rt_tolerance": float(tol)})
    ref = pd.DataFrame(rows)
    flags = [_flag("warning", "retention time", "", r.compound,
                   f"No standard peak found near {r.rt_config:.3f} min: the configured time is used.")
             for r in ref.itertuples() if r.n_standards == 0]
    return StepOut({"references": ref}, {"references": ref.to_dict("records")}, flags)


register(StepType(
    id="gc.reference_rts",
    title="Fix the retention times from the standards",
    category="GC",
    sentence="Finds each compound's exact retention time and peak width in the calibration standards of this "
             "same sequence.",
    inputs=[Port("injections", "injections", "Chromatograms"), Port("samples", "table", "Injection design")],
    outputs=[Port("references", "table", "Reference retention times")],
    params=[Param("compounds", "object", "Compounds", {},
                  "Name -> {rt: expected minutes, rt_tolerance_min?: own tolerance}.")] + INTEGRATION_PARAMS,
    run=_run_rts,
    explain=lambda p: sheet(
        formulas=[{"label": "Reference retention time of compound k",
                   "latex": r"t_{R,k} = \mathrm{median}\left(t_{\mathrm{apex},k,1}, \ldots, "
                            r"t_{\mathrm{apex},k,n}\right)"}],
        symbols=[{"symbol": r"t_{\mathrm{apex},k,i}", "meaning": "apex time of k in standard injection i",
                  "unit": "min"},
                 {"symbol": "n", "meaning": "standards in which k was found", "unit": ""}],
        rules=[
            "In every standard injection, the peak nearest the configured time (within 2.5 x the tolerance) is "
            "integrated as in the next step; the median of the apex times becomes the reference.",
            "The median of the peak widths at half height (FWHM) is kept too: a sample peak much narrower or wider "
            "than in the standards is not accepted as the same compound.",
            "Configured times: " + ", ".join(f"{k} {v.get('rt')} min" for k, v in (p.get("compounds") or {}).items()),
        ],
        code="np.median([integrate_compound(t, y, k, rt_config, tol*2.5).rt for each standard])",
    ),
    example=lambda p, f: [{"label": f"{r['compound']}: from {r['n_standards']} standards "
                                    f"(configured {num(r['rt_config'])} min)",
                           "latex": rf"t_{{R}} = {num(r['rt_ref'], 5)}\ \mathrm{{min}}, \quad "
                                    rf"\mathrm{{FWHM}} = {num(r['fwhm_ref'], 3)}\ \mathrm{{min}}"}
                          for r in f.get("references") or []],
    methods=lambda p, f: (
        "Retention times were fixed from the calibration standards of the same sequence ("
        + ", ".join(f"{r['compound']} {num(r['rt_ref'])} min" for r in f.get("references") or []) + ")."
    ),
))


# ---------------------------------------------------------------------------------------------------- integrate


def _run_integrate(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    injections, meta, ref = inputs["injections"], inputs["samples"], inputs["references"]
    comps = _compounds(p)
    is_name = p.get("internal_standard")
    if is_name and is_name not in comps:
        raise StepError(f"The internal standard {is_name} is not among the compounds ({', '.join(comps)}).")
    ip = integration_params(p)
    rt_ref = dict(zip(ref.compound, ref.rt_ref))
    fwhm_ref = {c: (None if f != f else f) for c, f in zip(ref.compound, ref.fwhm_ref)}
    order = ([is_name] if is_name else []) + [c for c in comps if c != is_name]
    drift = bool(p.get("rt_drift_correction", True)) and bool(is_name)
    corrections = inputs.get("corrections")
    excl: dict[tuple[int, str], str] = {}
    for e in _approved(corrections, "exclude"):
        for c in [e["compound"]] if e.get("compound") else order:
            excl[(int(e["injection"]), c)] = e.get("reason", "")
    manual = {(int(m["injection"]), m["compound"]): m for m in _approved(corrections, "manual_integration")}
    roles = meta.set_index("injection").role
    memo = ctx.cache.setdefault(("peaks", getattr(injections, "key", "")), {})
    ipkey = tuple(sorted(vars(ip).items()))
    rows = []
    for inj in injections:
        if roles.get(inj.number, "unknown") == "unknown":
            continue
        shift = 0.0
        for comp in order:
            rt_exp = rt_ref[comp] + shift
            tol = comps[comp].get("rt_tolerance_min", ip.rt_tolerance_min)
            m = manual.get((inj.number, comp))
            mk = (inj.number, comp, round(rt_exp, 9), ipkey, fwhm_ref.get(comp), tol,
                  (m["start_min"], m["end_min"], m.get("reason", "")) if m else None)
            r = memo.get(mk)
            if r is None:
                if m:
                    r = integrate_manual(inj.time_min, inj.signal_pa, comp, rt_exp, float(m["start_min"]),
                                         float(m["end_min"]), ip, m.get("reason", ""))
                else:
                    r = integrate_compound(inj.time_min, inj.signal_pa, comp, rt_exp, ip, fwhm_ref.get(comp),
                                           rt_tolerance=tol)
                memo[mk] = r
            d = r.to_dict()
            if (inj.number, comp) in excl:
                d["status"], d["note"] = "excluded", excl[(inj.number, comp)]
            if comp == is_name and drift and d["status"] == "ok":
                shift = d["rt"] - rt_ref[is_name]
            d["injection"] = inj.number
            d["rt_tolerance"] = tol
            v = inj.vendor_peaks[inj.vendor_peaks.name == comp]
            d["vendor_rt"] = float(v.rt_min.iloc[0]) if len(v) else np.nan
            d["vendor_area"] = float(v.area_pa_min.iloc[0]) if len(v) else np.nan
            rows.append(d)
    peaks = pd.DataFrame(rows).merge(meta, on="injection", how="left")
    counts = peaks.groupby(["compound", "status"]).size().unstack(fill_value=0)
    ex = peaks[(peaks.status == "ok") & (peaks.role == "standard")]
    example = ex.iloc[len(ex) // 2].to_dict() if len(ex) else None
    facts = {"status_counts": {c: {s: int(n) for s, n in row.items() if n} for c, row in counts.iterrows()},
             "example": example, "drift": drift, "internal_standard": is_name,
             "manual": len(manual), "excluded": len(excl)}
    return StepOut({"peaks": peaks}, facts)


def _integrate_example(p: dict, f: dict) -> list[dict[str, str]]:
    e = f.get("example")
    if not e:
        return []
    if e.get("start") is None or e.get("start") != e.get("start"):
        return [{"label": f"{e['compound']} in injection #{e['injection']} ({e['name']}): not detected, area 0. "
                          f"{e.get('note') or ''}", "latex": ""}]
    return [
        {"label": f"Peak of {e['compound']} in injection #{e['injection']} ({e['name']})",
         "latex": rf"A = \int_{{{num(e['start'])}}}^{{{num(e['end'])}}} \left(y(t) - b(t)\right) dt = {num(e['area'])}"
                  r"\ \mathrm{pA \cdot min}"},
        {"label": "Baseline: the straight line between the peak's start and end",
         "latex": rf"b(t) = {num(e['baseline_start_y'])} + \frac{{{num(e['baseline_end_y'])} - "
                  rf"{num(e['baseline_start_y'])}}}{{{num(e['end'])} - {num(e['start'])}}}\left(t - {num(e['start'])}"
                  r"\right)"},
        {"label": "Signal-to-noise",
         "latex": rf"S/N = \frac{{h}}{{\sigma}} = \frac{{{num(e['height'])}}}{{{num(e['noise'])}}} = {num(e['snr'])}"},
    ]


register(StepType(
    id="gc.integrate",
    title="Find and integrate the peaks",
    category="GC",
    sentence="Finds each compound's peak only at its reference retention time and measures its area between "
             "a straight baseline drawn from valley to valley.",
    inputs=[Port("injections", "injections", "Chromatograms"), Port("samples", "table", "Injection design"),
            Port("references", "table", "Reference retention times"),
            Port("corrections", "corrections", "Corrections (manual boundaries, exclusions)", optional=True)],
    outputs=[Port("peaks", "table", "Peaks")],
    params=[Param("compounds", "object", "Compounds", {}),
            Param("internal_standard", "text", "Internal standard", None),
            Param("rt_drift_correction", "boolean", "Follow the internal standard's drift", True,
                  "Shift every expected time by how far the internal standard moved in that injection.")]
    + INTEGRATION_PARAMS,
    run=_run_integrate,
    explain=lambda p: sheet(
        formulas=[
            {"label": "Peak area (trapezoidal sum of signal above the baseline)",
             "latex": r"A = \int_{t_s}^{t_e} \left(y(t) - b(t)\right) dt \approx \sum_{i} \frac{(y_i - b_i) + "
                      r"(y_{i+1} - b_{i+1})}{2}\,(t_{i+1} - t_i)"},
            {"label": "Baseline", "latex": r"b(t) = y_s + \frac{y_e - y_s}{t_e - t_s}\,(t - t_s)"},
            {"label": "Signal-to-noise", "latex": r"S/N = \frac{h}{\sigma}, \quad \sigma = 1.4826 \cdot "
                                                  r"\mathrm{median}\left|r_i - \mathrm{median}(r)\right|"},
            {"label": "Expected time with drift correction",
             "latex": r"t_{\mathrm{exp},k} = t_{R,k} + \left(t_{\mathrm{IS}} - t_{R,\mathrm{IS}}\right)"},
        ],
        symbols=[
            {"symbol": "y(t)", "meaning": "raw FID signal", "unit": "pA"},
            {"symbol": r"t_s, t_e", "meaning": "peak start and end", "unit": "min"},
            {"symbol": r"y_s, y_e", "meaning": "smoothed signal at start and end", "unit": "pA"},
            {"symbol": "h", "meaning": "peak height above the baseline (smoothed)", "unit": "pA"},
            {"symbol": r"\sigma", "meaning": "noise: robust SD of the signal minus a heavy smooth, r", "unit": "pA"},
            {"symbol": r"t_{R,k}", "meaning": "reference time of compound k (previous step)", "unit": "min"},
            {"symbol": "A", "meaning": "peak area", "unit": "pA·min"},
        ],
        rules=[
            f"Search ±0.5 min around the expected time on a Savitzky-Golay smoothed trace "
            f"({p.get('smoothing_points', 11)} points, cubic).",
            f"A peak counts as the compound only if its apex lies within ±{p.get('rt_tolerance_min', 0.012)} min "
            "of the expected time (own tolerance per compound where set) and its width is 0.4-3 x the width in "
            "the standards. Otherwise the compound is 'n.d.', even if another peak is near: this keeps impurities "
            "from being reported as product.",
            "Start and end: walk outward from the apex while the smoothed signal keeps falling (valley or return to "
            f"baseline), at most {p.get('max_half_width_min', 0.15)} min each way.",
            f"S/N below {p.get('lod_snr', 3)}: not detected (area 0). Below {p.get('loq_snr', 10)}: '<LOQ'.",
            "The internal standard is integrated first; with drift correction every other compound's expected "
            "time moves by the internal standard's shift in that injection.",
            "Manual boundaries from the corrections replace the automatic start and end (status 'manual'); "
            "excluded peaks keep their numbers but are marked 'excluded' and not used. Each has a written reason.",
        ],
        code="scipy.signal.savgol_filter, scipy.signal.find_peaks(prominence=lod_snr*noise), "
             "numpy.trapezoid(y - baseline, t)",
    ),
    methods=lambda p, f: (
        "Peaks were identified only within ±{tol} min of the reference retention times{drift} and integrated "
        "valley-to-valley with a linear baseline (trapezoidal rule, pA·min); S/N < {lod} was reported as not "
        "detected and S/N < {loq} as below the limit of quantification{manual}."
    ).format(tol=p.get("rt_tolerance_min", 0.012),
             drift=" (corrected for the internal standard's retention-time drift)" if f.get("drift") else "",
             lod=p.get("lod_snr", 3), loq=p.get("loq_snr", 10),
             manual=(f"; {f['manual']} peak(s) were integrated manually and {f['excluded']} excluded, each with a "
                     "recorded reason") if f.get("manual") or f.get("excluded") else ""),
    example=_integrate_example,
))


# ---------------------------------------------------------------------------------------------------- IS ratio


def _run_ratio(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    peaks = inputs["peaks"]
    is_name = p.get("internal_standard")
    if not is_name:
        raise StepError("Say which compound is the internal standard.")
    is_pk = peaks[peaks.compound == is_name].set_index("injection")
    if is_pk.empty:
        raise StepError(f"No peaks of the internal standard {is_name}.")
    is_area = is_pk.area.where(is_pk.status.isin(["ok", "manual"]))
    out = peaks[peaks.compound != is_name].copy()
    out["is_area"] = out.injection.map(is_area)
    out["ratio"] = out.area / out.is_area
    ex = out[(out.status == "ok") & out.ratio.notna()]
    example = ex.iloc[len(ex) // 2][["injection", "name", "compound", "area", "is_area", "ratio"]].to_dict() \
        if len(ex) else None
    return StepOut({"ratios": out}, {"example": example, "internal_standard": is_name,
                                     "missing_is": int(is_area.isna().sum())})


register(StepType(
    id="gc.is_ratio",
    title="Divide by the internal standard",
    category="GC",
    sentence="Divides each compound's peak area by the internal standard's area in the same injection, which "
             "cancels pipetting and injection-volume differences.",
    inputs=[Port("peaks", "table", "Peaks")],
    outputs=[Port("ratios", "table", "Area ratios")],
    params=[Param("internal_standard", "text", "Internal standard", None)],
    run=_run_ratio,
    explain=lambda p: sheet(
        formulas=[{"label": "Area ratio", "latex": r"R_k = \frac{A_k}{A_{\mathrm{IS}}}"}],
        symbols=[{"symbol": r"A_k", "meaning": "peak area of compound k", "unit": "pA·min"},
                 {"symbol": r"A_{\mathrm{IS}}", "meaning": f"peak area of the internal standard "
                                                          f"({p.get('internal_standard') or '?'}) in the same "
                                                          "injection",
                  "unit": "pA·min"}],
        rules=["An injection whose internal-standard peak is not usable (not detected or excluded) gets no ratio, "
               "so none of its compounds are quantified."],
        code="ratio = area / is_area",
    ),
    methods=lambda p, f: f"Peak areas were normalised to the internal standard ({p.get('internal_standard')}).",
    example=lambda p, f: [] if not f.get("example") else [{
        "label": f"{f['example']['compound']} in injection #{f['example']['injection']}",
        "latex": rf"R = \frac{{{num(f['example']['area'])}}}{{{num(f['example']['is_area'])}}} = "
                 rf"{num(f['example']['ratio'])}"}],
))


# ---------------------------------------------------------------------------------------------------- calibrate


def fit_calibration(std: pd.DataFrame, p: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    lo = p.get("min_conc_mM")
    hi = p.get("max_conc_mM")
    lo = 0.0 if lo is None else float(lo)
    hi = np.inf if hi is None else float(hi)
    series = p.get("series") or None
    zero = bool(p.get("force_zero_intercept", False))
    curves, points = [], []
    for comp, g in std.groupby("compound"):
        g = g.copy()
        g["used"] = g.std_conc_mM.between(lo, hi) & (g.status != "excluded") & (g.status != "n.d.")
        g["used"] &= g.ratio.notna()
        if series:
            g["used"] &= g.std_series.isin(series)
        u = g[g.used]
        x, y = u.std_conc_mM.to_numpy(float), u.ratio.to_numpy(float)
        if len(x) < 2:
            raise StepError(f"{comp}: fewer than 2 usable standards in {lo:g}-{hi:g} mM; can't fit a line.")
        if zero:
            slope, intercept = float((x @ y) / (x @ x)), 0.0
        else:
            slope, intercept = (float(v) for v in np.polyfit(x, y, 1))
        pred = slope * x + intercept
        dof = max(len(x) - (1 if zero else 2), 1)
        syx = float(np.sqrt(((y - pred) ** 2).sum() / dof))
        sst = ((y - y.mean()) ** 2).sum()
        r2 = float(1 - ((y - pred) ** 2).sum() / sst) if sst > 0 else np.nan
        curves.append({"compound": comp, "slope": slope, "intercept": intercept, "r2": r2, "n_points": len(x),
                       "range_mM": f"{x.min():g}-{x.max():g}", "max_conc_mM": float(x.max()), "s_yx": syx,
                       "lod_mM": 3.3 * syx / slope, "loq_mM": 10 * syx / slope})
        g["predicted_ratio"] = slope * g.std_conc_mM + intercept
        g["back_calc_mM"] = (g.ratio - intercept) / slope
        g["residual_pct"] = 100 * (g.back_calc_mM / g.std_conc_mM - 1)
        points.append(g)
    if not curves:
        raise StepError("No standards to calibrate with (no injection has the role 'standard').")
    return pd.DataFrame(curves), pd.concat(points, ignore_index=True)


def _run_cal(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    r = inputs["ratios"]
    curves, points = fit_calibration(r[r.role == "standard"], p)
    return StepOut({"curves": curves, "points": points}, {"curves": curves.to_dict("records")})


def _cal_example(p: dict, f: dict) -> list[dict[str, str]]:
    out = []
    for c in f.get("curves") or []:
        out.append({"label": f"{c['compound']}: {c['n_points']} standards, {c['range_mM']} mM",
                    "latex": rf"R = {num(c['slope'])}\,c {'+' if c['intercept'] >= 0 else '-'} "
                             rf"{num(abs(c['intercept']))}, \quad R^2 = {num(c['r2'], 5)}, \quad "
                             rf"\mathrm{{LOD}} = \frac{{3.3 \cdot {num(c['s_yx'])}}}{{{num(c['slope'])}}} = "
                             rf"{num(c['lod_mM'])}\ \mathrm{{mM}}"})
    return out


register(StepType(
    id="gc.calibrate",
    title="Fit the calibration lines",
    category="GC",
    sentence="Fits a straight line through the standards for each compound: area ratio against known "
             "concentration, with detection and quantification limits from the scatter around the line.",
    inputs=[Port("ratios", "table", "Area ratios")],
    outputs=[Port("curves", "table", "Calibration lines"), Port("points", "table", "Calibration points")],
    params=[Param("min_conc_mM", "number", "Lowest standard used", None, "", "mM"),
            Param("max_conc_mM", "number", "Highest standard used", None,
                  "Samples above it are extrapolated and flagged.", "mM"),
            Param("series", "list", "Standard series used", [], "e.g. [\"A\", \"B\"]; empty: all."),
            Param("force_zero_intercept", "boolean", "Force the line through zero", False)],
    run=_run_cal,
    explain=lambda p: sheet(
        formulas=[
            {"label": "Calibration line (least squares)", "latex": r"R = m \cdot c + b"},
            {"label": "Slope and intercept",
             "latex": r"m = \frac{\sum (c_i - \bar{c})(R_i - \bar{R})}{\sum (c_i - \bar{c})^2}, \quad "
                      r"b = \bar{R} - m\,\bar{c}"},
            {"label": "Scatter around the line",
             "latex": r"s_{y/x} = \sqrt{\frac{\sum (R_i - (m c_i + b))^2}{n - 2}}"},
            {"label": "Limits of detection and quantification",
             "latex": r"\mathrm{LOD} = \frac{3.3\, s_{y/x}}{m}, \quad \mathrm{LOQ} = \frac{10\, s_{y/x}}{m}"},
            {"label": "Back-calculated standard (residual)",
             "latex": r"\hat{c}_i = \frac{R_i - b}{m}, \quad \mathrm{residual} = "
                      r"100 \left(\frac{\hat{c}_i}{c_i} - 1\right)"},
        ],
        symbols=[{"symbol": "R", "meaning": "area ratio (compound / internal standard)", "unit": ""},
                 {"symbol": "c", "meaning": "known standard concentration", "unit": "mM"},
                 {"symbol": "m, b", "meaning": "slope and intercept", "unit": "1/mM, -"},
                 {"symbol": "n", "meaning": "standards used", "unit": ""}],
        rules=[
            "Standards used: concentration between {lo} and {hi} mM{ser}; not detected or excluded standards are "
            "left out.".format(lo=p.get("min_conc_mM") if p.get("min_conc_mM") is not None else 0,
                               hi=p.get("max_conc_mM") if p.get("max_conc_mM") is not None else "∞",
                               ser=f", series {', '.join(p['series'])}" if p.get("series") else ""),
            "All replicate series are pooled into one line per compound." +
            (" The line is forced through zero (b = 0)." if p.get("force_zero_intercept") else ""),
            "LOD and LOQ use the calibration-based definition (ICH Q2): 3.3 and 10 times the residual standard "
            "deviation, divided by the slope.",
        ],
        code="numpy.polyfit(conc, ratio, 1)",
        reference="ICH Q2(R2) Validation of analytical procedures, section on detection and quantitation limits.",
    ),
    methods=lambda p, f: (
        "Calibration lines (area ratio vs. concentration, ordinary least squares) were fitted per compound from "
        "standards between {lo} and {hi} mM ({c}); LOD and LOQ were calculated as 3.3 and 10 s(y/x)/slope."
    ).format(lo=p.get("min_conc_mM") if p.get("min_conc_mM") is not None else 0,
             hi=p.get("max_conc_mM") if p.get("max_conc_mM") is not None else "the highest",
             c="; ".join(f"{c['compound']} R² = {c['r2']:.4f}" for c in f.get("curves") or [])),
    example=_cal_example,
))


# ---------------------------------------------------------------------------------------------------- quantify


def _run_quant(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    r, curves = inputs["ratios"], inputs["curves"]
    factor = float(p.get("concentration_factor") or 1.0)
    roles = p.get("roles") or ["sample", "blank"]
    c = curves.set_index("compound")
    out = r[r.role.isin(roles)].copy()
    out["slope"] = out.compound.map(c.slope)
    out["intercept"] = out.compound.map(c.intercept)
    raw = (out.ratio - out.intercept) / out.slope * factor
    nd = out.status.isin(["n.d."])
    out["conc_mM"] = np.where(nd, 0.0, raw)
    out["conc_flag"] = ""
    out.loc[nd, "conc_flag"] = "n.d."
    out.loc[~nd & (out.conc_mM < out.compound.map(c.loq_mM)), "conc_flag"] = "<LOQ"
    out.loc[out.status == "<LOQ", "conc_flag"] = "<LOQ"
    above = out.conc_mM / factor > out.compound.map(c.max_conc_mM) * 1.05
    out.loc[above, "conc_flag"] = "above calibration range"
    out.loc[out.status == "excluded", ["conc_mM", "conc_flag"]] = [np.nan, "excluded"]
    ex = out[(out.status == "ok") & (out.role == "sample") & out.conc_mM.notna()]
    example = ex.iloc[len(ex) // 2][["injection", "name", "compound", "ratio", "slope", "intercept",
                                     "conc_mM"]].to_dict() if len(ex) else None
    flags = out.conc_flag.value_counts().to_dict()
    return StepOut({"concentrations": out}, {"example": example, "factor": factor,
                                             "flag_counts": {str(k): int(v) for k, v in flags.items() if k}})


register(StepType(
    id="gc.quantify",
    title="Calculate the concentrations",
    category="GC",
    sentence="Turns each sample's area ratio into a concentration with the compound's calibration line, and "
             "marks values below the quantification limit or above the calibrated range.",
    inputs=[Port("ratios", "table", "Area ratios"), Port("curves", "table", "Calibration lines")],
    outputs=[Port("concentrations", "table", "Concentrations")],
    params=[Param("concentration_factor", "number", "Concentration factor", 1.0,
                  "Multiplies every concentration, e.g. 500 µl intended / 550 µl added = 0.909."),
            Param("roles", "list", "Roles quantified", ["sample", "blank"])],
    run=_run_quant,
    explain=lambda p: sheet(
        formulas=[{"label": "Concentration (the calibration line solved for c)",
                   "latex": r"c = \frac{R - b}{m} \cdot f"}],
        symbols=[{"symbol": "R", "meaning": "the sample's area ratio", "unit": ""},
                 {"symbol": "m, b", "meaning": "slope and intercept of the compound's calibration line", "unit": ""},
                 {"symbol": "f", "meaning": f"concentration factor ({p.get('concentration_factor') or 1})", "unit": ""},
                 {"symbol": "c", "meaning": "concentration", "unit": "mM"}],
        rules=[
            "Not detected (n.d.) counts as 0 mM, as in the earlier Excel sheets; excluded peaks get no value.",
            "Below the LOQ of the calibration, or a peak with S/N below the LOQ: flagged '<LOQ' (value kept).",
            "More than 5 % above the highest standard used: flagged 'above calibration range' (extrapolated).",
        ],
        code="conc = (ratio - intercept) / slope * factor",
    ),
    methods=lambda p, f: (
        "Concentrations were calculated from the calibration lines"
        + (f" and multiplied by a factor of {f['factor']:g}" if f.get("factor", 1) != 1 else "")
        + "; non-detects were set to 0 mM and values below the LOQ or above the calibrated range were flagged."
    ),
    example=lambda p, f: [] if not f.get("example") else [{
        "label": f"{f['example']['compound']} in {f['example']['name']} (injection #{f['example']['injection']})",
        "latex": rf"c = \frac{{{num(f['example']['ratio'])} - ({num(f['example']['intercept'])})}}"
                 rf"{{{num(f['example']['slope'])}}} \cdot {num(f['factor'])} = {num(f['example']['conc_mM'])}"
                 r"\ \mathrm{mM}"}],
))


# ---------------------------------------------------------------------------------------------------- QC


def _run_qc(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    peaks, ref = inputs["peaks"], inputs["references"]
    curves, points = inputs["curves"], inputs["points"]
    conc, tc, mb = inputs["concentrations"], inputs.get("time_course"), inputs.get("mass_balance")
    is_name = p.get("internal_standard")
    fwhm_ref = {c: (None if f != f else f) for c, f in zip(ref.compound, ref.fwhm_ref)}
    flags: list[dict] = []
    design = inputs.get("design_flags")
    if design is not None and len(design):
        flags += design.to_dict("records")
    # internal standard
    if is_name:
        tol = p.get("is_area_tolerance_pct", 25)
        ip = peaks[(peaks.compound == is_name) & peaks.role.isin(["sample", "standard"])]
        med = ip.area.median()
        for _, r in ip.iterrows():
            dev = 100 * (r.area / med - 1)
            if abs(dev) > tol:
                flags.append(_flag("warning", "internal standard", r.injection, is_name,
                                   f"IS area {dev:+.0f}% vs. run median ({r['name']})."))
            ref_w = fwhm_ref.get(is_name)
            if ref_w and r.fwhm > p.get("shoulder_fwhm_ratio", 1.3) * ref_w:
                flags.append(_flag("warning", "internal standard", r.injection, is_name,
                                   f"IS peak {r.fwhm / ref_w:.1f}x wider than in standards - co-eluting compound? "
                                   "All ratios of this injection are biased low."))
    # peak shape
    lim = p.get("shoulder_fwhm_ratio", 1.3)
    for _, r in peaks[(peaks.compound != is_name) & (peaks.status == "ok")].iterrows():
        ref_w = fwhm_ref.get(r.compound)
        if ref_w and r.fwhm > lim * ref_w:
            flags.append(_flag("warning", "peak shape", r.injection, r.compound,
                               f"Peak {r.fwhm / ref_w:.1f}x wider than in standards - possible co-elution."))
    # Chromeleon's assignment
    v = peaks[peaks.vendor_rt.notna()]
    wrong = v[(v.vendor_rt - v.rt_expected).abs() > v.rt_tolerance]
    for comp, g in wrong.groupby("compound"):
        rts = ", ".join(f"#{i} ({rt:.3f})" for i, rt in zip(g.injection, g.vendor_rt))
        flags.append(_flag("info", "Chromeleon assignment", "", comp,
                           f"Chromeleon labelled a peak far from the {comp} reference RT "
                           f"({g.rt_expected.median():.3f} min) in {len(g)} injections: {rts}. Numbers taken from "
                           "the instrument's own peak table (as in a spreadsheet) would be a different compound."))
    # calibration
    for _, c in curves.iterrows():
        if c.r2 < p.get("min_r2", 0.995):
            flags.append(_flag("warning", "calibration", "", c.compound, f"R^2 = {c.r2:.4f}"))
    rlim = p.get("max_residual_pct", 15)
    for _, q in points[points.used].iterrows():
        if abs(q.residual_pct) > rlim:
            flags.append(_flag("warning", "calibration", q.injection, q.compound,
                               f"Standard {q.std_conc_mM:g} mM {q.std_series}: back-calculated "
                               f"{q.residual_pct:+.0f}%."))
    # concentrations
    for _, r in conc[(conc.role == "sample") & (conc.conc_flag == "above calibration range")].iterrows():
        flags.append(_flag("warning", "range", r.injection, r.compound,
                           f"{r.conc_mM:.3f} mM is above the calibrated range - extrapolated."))
    for _, r in conc[(conc.role == "blank") & (conc.status == "ok")].iterrows():
        flags.append(_flag("warning", "blank", r.injection, r.compound,
                           f"Detected in blank (S/N {r.snr:.0f}) - carry-over or contamination."))
    if tc is not None:
        cv_lim = p.get("max_replicate_cv_pct", 20)
        for _, r in tc[(tc.cv_pct > cv_lim) & (tc["mean"] > p.get("cv_min_conc_mM", 0.02))].iterrows():
            flags.append(_flag("warning", "replicates", "", r.compound,
                               f"{r.substrate}-fed, {r.time_min:g} min: replicate CV {r.cv_pct:.0f}% "
                               f"(mean {r['mean']:.3f} mM)."))
    if mb is not None:
        lo, hi = p.get("mass_balance_range_pct") or [80, 120]
        for _, r in mb.iterrows():
            if not lo <= r.pct_of_t0 <= hi:
                flags.append(_flag("warning", "mass balance", "", r.substrate,
                                   f"{r.substrate}-fed, {r.time_min:g} min: sum of analytes {r.pct_of_t0:.0f}% "
                                   "of t0."))
    for _, r in peaks[peaks.status.isin(["manual", "excluded"])].iterrows():
        flags.append(_flag("info", "correction", r.injection, r.compound, f"{r.status}: {r.note}"))
    df = pd.DataFrame(flags, columns=["level", "category", "injection", "compound", "message"])
    if len(df):
        order = {"error": 0, "warning": 1, "info": 2}
        df["_o"] = df.level.map(order)
        df["_i"] = pd.to_numeric(df.injection, errors="coerce").fillna(np.inf)
        df = df.sort_values(["_o", "category", "_i"]).drop(columns=["_o", "_i"]).reset_index(drop=True)
    counts = df.level.value_counts().to_dict() if len(df) else {}
    return StepOut({"flags": df}, {"counts": {str(k): int(v) for k, v in counts.items()}})


register(StepType(
    id="gc.qc",
    title="Check the run",
    category="GC",
    sentence="Runs every automatic plausibility check and lists what needs a look; nothing is changed silently.",
    inputs=[Port("peaks", "table", "Peaks"), Port("references", "table", "Reference retention times"),
            Port("curves", "table", "Calibration lines"), Port("points", "table", "Calibration points"),
            Port("concentrations", "table", "Concentrations"),
            Port("time_course", "table", "Time course", optional=True),
            Port("mass_balance", "table", "Mass balance", optional=True),
            Port("design_flags", "table", "Design checks", optional=True)],
    outputs=[Port("flags", "table", "Quality flags")],
    params=[Param("internal_standard", "text", "Internal standard", None),
            Param("is_area_tolerance_pct", "number", "Internal standard area tolerance", 25, "", "%"),
            Param("shoulder_fwhm_ratio", "number", "Widest acceptable peak (x standards)", 1.3),
            Param("min_r2", "number", "Lowest acceptable R²", 0.995),
            Param("max_residual_pct", "number", "Largest standard residual", 15, "", "%"),
            Param("max_replicate_cv_pct", "number", "Largest replicate CV", 20, "", "%"),
            Param("cv_min_conc_mM", "number", "CV checked above", 0.02, "", "mM"),
            Param("mass_balance_range_pct", "list", "Mass balance range", [80, 120], "", "% of t0")],
    run=_run_qc,
    explain=lambda p: sheet(
        formulas=[{"label": "Replicate coefficient of variation", "latex": r"\mathrm{CV} = 100\,\frac{s}{\bar{x}}"},
                  {"label": "Internal standard deviation from the run median",
                   "latex": r"\Delta = 100\left(\frac{A_{\mathrm{IS}}}{\mathrm{median}(A_{\mathrm{IS}})} - 1\right)"}],
        rules=[
            f"Internal standard area more than ±{p.get('is_area_tolerance_pct', 25)} % from the run median, or its "
            f"peak more than {p.get('shoulder_fwhm_ratio', 1.3)} x wider than in the standards (co-elution).",
            f"Any compound peak more than {p.get('shoulder_fwhm_ratio', 1.3)} x wider than in the standards.",
            "Chromeleon labelled a different peak than the reference time (explains differences to spreadsheet "
            "numbers).",
            f"Calibration R² below {p.get('min_r2', 0.995)}, or a standard back-calculated more than "
            f"±{p.get('max_residual_pct', 15)} % off.",
            "Sample concentration above the calibrated range; any compound detected in a blank.",
            f"Replicate CV above {p.get('max_replicate_cv_pct', 20)} % (means above "
            f"{p.get('cv_min_conc_mM', 0.02)} mM); mass balance outside "
            f"{(p.get('mass_balance_range_pct') or [80, 120])[0]}-{(p.get('mass_balance_range_pct') or [80, 120])[1]}"
            " % of t0.",
            "Every manual integration, exclusion and rename is listed with its reason.",
        ],
    ),
    methods=lambda p, f: (
        "Automatic checks covered internal-standard stability, peak shape, calibration quality, blanks, replicate "
        "variation and mass balance (" + ", ".join(f"{v} {k}" for k, v in (f.get("counts") or {}).items()) + ")."
    ),
))

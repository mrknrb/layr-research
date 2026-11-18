"""Kinetics step types: time courses, specific activities (initial and interval rates) and mass balance.

Ported from gcassay's activity.py. Specific activity in U/gCDW (= µmol min⁻¹ gCDW⁻¹):
    activity = d[c]/dt [mM/min] * 1000 / biomass [gCDW/L]
"""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
from scipy import stats

from ..core import Ctx, Param, Port, StepError, StepOut, StepType, num, register, sheet

GROUP = ["strain", "substrate", "compound"]


def _biomass(p: dict, substrate: str) -> float:
    bm = p.get("biomass_gcdw_per_l")
    if isinstance(bm, dict):
        if substrate in bm:
            return float(bm[substrate])
        if "*" in bm:
            return float(bm["*"])
        raise StepError(f"No biomass for substrate {substrate}: add it to biomass_gcdw_per_l.")
    if bm is None:
        raise StepError("Give the biomass (gCDW/L), one number or one per substrate.")
    return float(bm)


def _samples(conc: pd.DataFrame) -> pd.DataFrame:
    return conc[conc.role == "sample"].dropna(subset=["conc_mM"])


# ---------------------------------------------------------------------------------------------------- time course


def _run_tc(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    s = _samples(inputs["concentrations"])
    g = s.groupby(GROUP + ["time_min"]).conc_mM
    tc = g.agg(mean="mean", sd="std", n="count").reset_index()
    t0 = tc[tc.time_min == tc.groupby(GROUP).time_min.transform("min")].set_index(GROUP)["mean"]
    tc["delta_vs_t0"] = tc["mean"] - tc.set_index(GROUP).index.map(t0).to_numpy()
    tc["cv_pct"] = 100 * tc.sd / tc["mean"].abs()
    ex = tc[(tc.n > 1) & (tc["mean"] > 0)]
    example = None
    if len(ex):
        r = ex.iloc[len(ex) // 2]
        vals = s[(s.strain == r.strain) & (s.substrate == r.substrate) & (s.compound == r.compound)
                 & (s.time_min == r.time_min)].conc_mM.tolist()
        example = {**{k: r[k] for k in GROUP + ["time_min", "mean", "sd", "n", "cv_pct"]}, "values": vals}
    return StepOut({"time_course": tc}, {"example": example, "groups": int(tc.groupby(GROUP).ngroups)})


def _tc_example(p: dict, f: dict) -> list[dict[str, str]]:
    e = f.get("example")
    if not e:
        return []
    vals = " + ".join(num(v) for v in e["values"])
    return [{"label": f"{e['compound']}, {e['substrate']}-fed {e['strain']}, {e['time_min']:g} min",
             "latex": rf"\bar{{c}} = \frac{{{vals}}}{{{e['n']}}} = {num(e['mean'])}\ \mathrm{{mM}}, \quad "
                      rf"s = {num(e['sd'])}, \quad \mathrm{{CV}} = {num(e['cv_pct'], 3)}\,\%"}]


register(StepType(
    id="kin.time_course",
    title="Average the replicates over time",
    category="Kinetics",
    sentence="Averages the replicate concentrations of each compound at each time point, with standard deviation, "
             "CV and the change since the first time point.",
    inputs=[Port("concentrations", "table", "Concentrations")],
    outputs=[Port("time_course", "table", "Time course")],
    params=[],
    run=_run_tc,
    explain=lambda p: sheet(
        formulas=[{"label": "Mean, standard deviation and CV of n replicates",
                   "latex": r"\bar{c} = \frac{1}{n}\sum_{i=1}^{n} c_i, \quad s = \sqrt{\frac{\sum (c_i - \bar{c})^2}"
                            r"{n-1}}, \quad \mathrm{CV} = 100\,\frac{s}{\bar{c}}"},
                  {"label": "Change since the first time point", "latex": r"\Delta c(t) = \bar{c}(t) - \bar{c}(t_0)"}],
        symbols=[{"symbol": r"c_i", "meaning": "concentration of replicate i", "unit": "mM"}],
        rules=["Grouped by strain, fed substrate, compound and time; only samples (not standards or blanks) and only "
               "quantified values (excluded peaks are left out, non-detects count as 0)."],
        code="conc.groupby([strain, substrate, compound, time_min]).conc_mM.agg(['mean', 'std', 'count'])",
    ),
    methods=lambda p, f: "Replicates were averaged per time point (mean ± SD).",
    example=_tc_example,
))


# ---------------------------------------------------------------------------------------------------- initial rates


def _run_rates(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    s = _samples(inputs["concentrations"])
    w0, w1 = p.get("rate_window_min") or [0, 30]
    rows = []
    example = None
    for (strain, sub, comp), g in s.groupby(GROUP):
        bm = _biomass(p, sub)
        gw = g[g.time_min.between(w0, w1)]
        if gw.time_min.nunique() >= 2:
            fit = stats.linregress(gw.time_min, gw.conc_mM)
            slope, se, r2, icpt = fit.slope, fit.stderr, fit.rvalue**2, fit.intercept
        else:
            slope = se = r2 = icpt = np.nan
        row = {"strain": strain, "substrate_fed": sub, "compound": comp,
               "role": "substrate" if comp == sub else "product", "window_min": f"{w0}-{w1}",
               "n_points": len(gw), "rate_mM_per_min": slope, "activity_U_per_gCDW": slope * 1000 / bm,
               "activity_se": se * 1000 / bm if se == se else np.nan, "r2": r2, "biomass_gCDW_per_L": bm,
               "intercept_mM": icpt}
        rows.append(row)
        clear = abs(row["activity_U_per_gCDW"]) / row["activity_se"] if se == se and se > 0 else -1
        if comp == sub and slope == slope and (example is None or clear > example["_clear"]):
            example = {**row, "_clear": clear}
    act = pd.DataFrame(rows)
    if len(act):
        act["nonzero"] = np.where(act.activity_se.isna(), "no (all n.d.)",
                                  np.where(act.activity_U_per_gCDW.abs() > 2 * act.activity_se, "yes", "no"))
    return StepOut({"activities": act}, {"example": example, "window": [w0, w1], "rows": len(act)})


def _rates_example(p: dict, f: dict) -> list[dict[str, str]]:
    e = f.get("example")
    if not e:
        return []
    return [
        {"label": f"{e['compound']} in {e['substrate_fed']}-fed {e['strain']} ({e['n_points']} values, "
                  f"{e['window_min']} min)",
         "latex": rf"c(t) = {num(e['intercept_mM'])} {'+' if e['rate_mM_per_min'] >= 0 else '-'} "
                  rf"{num(abs(e['rate_mM_per_min']))}\,t, \quad R^2 = {num(e['r2'], 3)}"},
        {"label": "Specific activity",
         "latex": rf"a = \frac{{{num(e['rate_mM_per_min'])} \cdot 1000}}{{{num(e['biomass_gCDW_per_L'])}}} = "
                  rf"{num(e['activity_U_per_gCDW'])} \pm {num(e['activity_se'])}\ \mathrm{{U/g_{{CDW}}}}"},
    ]


BIOMASS = Param("biomass_gcdw_per_l", "object", "Biomass", None,
                "Cell dry weight in the assay: one number, or one per fed substrate ({\"HAME\": 0.56, \"*\": 0.5}).",
                "gCDW/L")

register(StepType(
    id="kin.initial_rates",
    title="Calculate the initial rates",
    category="Kinetics",
    sentence="Fits a straight line through all replicate concentrations in the early time window and turns its "
             "slope into a specific activity per gram of cell dry weight.",
    inputs=[Port("concentrations", "table", "Concentrations")],
    outputs=[Port("activities", "table", "Specific activities")],
    params=[Param("rate_window_min", "list", "Time window", [0, 30], "Start and end of the linear range.", "min"),
            BIOMASS],
    run=_run_rates,
    explain=lambda p: sheet(
        formulas=[{"label": "Linear regression of concentration on time (all replicates)",
                   "latex": r"c(t) = c_0 + r\,t, \quad r = \frac{\sum (t_i - \bar{t})(c_i - \bar{c})}"
                            r"{\sum (t_i - \bar{t})^2}"},
                  {"label": "Specific activity and its standard error",
                   "latex": r"a = \frac{r \cdot 1000}{X}, \quad \mathrm{SE}(a) = \frac{\mathrm{SE}(r) \cdot 1000}{X}"},
                  {"label": "Distinguishable from zero", "latex": r"|a| > 2\,\mathrm{SE}(a)"}],
        symbols=[{"symbol": "r", "meaning": "rate: slope of the line", "unit": "mM/min"},
                 {"symbol": "X", "meaning": "biomass (cell dry weight)", "unit": "gCDW/L"},
                 {"symbol": "a", "meaning": "specific activity; negative = consumption",
                  "unit": "U/gCDW = µmol/min/gCDW"},
                 {"symbol": "1000", "meaning": "mM = mmol/L to µmol/L", "unit": ""}],
        rules=[f"Window: {(p.get('rate_window_min') or [0, 30])[0]}-{(p.get('rate_window_min') or [0, 30])[1]} min; "
               "every replicate value in it is one point of the regression (not the means).",
               "Biomass: " + (", ".join(f"{k} {v}" for k, v in p["biomass_gcdw_per_l"].items())
                              if isinstance(p.get("biomass_gcdw_per_l"), dict)
                              else str(p.get("biomass_gcdw_per_l"))) + " gCDW/L.",
               "The fed substrate's own rate is its consumption; other compounds are products."],
        code="scipy.stats.linregress(time_min, conc_mM)",
    ),
    methods=lambda p, f: (
        "Initial specific activities (U/gCDW) were calculated from the slope of a linear regression of all "
        f"replicate concentrations between {f.get('window', [0, 30])[0]} and {f.get('window', [0, 30])[1]} min, "
        "divided by the biomass; rates smaller than twice their standard error were reported as not different "
        "from zero."
    ),
    example=_rates_example,
))


# ---------------------------------------------------------------------------------------------------- interval rates


def _run_intervals(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    tc = inputs["time_course"]
    rows = []
    example = None
    for (strain, sub, comp), g in tc.groupby(GROUP):
        bm = _biomass(p, sub)
        g = g.sort_values("time_min").reset_index(drop=True)
        for i in range(1, len(g)):
            dt = g.time_min[i] - g.time_min[i - 1]
            a = (g["mean"][i] - g["mean"][i - 1]) * 1000 / dt / bm
            rows.append({"strain": strain, "substrate_fed": sub, "compound": comp,
                         "interval_min": f"{g.time_min[i - 1]:g}-{g.time_min[i]:g}", "activity_U_per_gCDW": a})
            if example is None and comp == sub:
                example = {"compound": comp, "strain": strain, "t0": g.time_min[i - 1], "t1": g.time_min[i],
                           "c0": g["mean"][i - 1], "c1": g["mean"][i], "bm": bm, "a": a}
    return StepOut({"interval_rates": pd.DataFrame(rows)}, {"example": example})


register(StepType(
    id="kin.interval_rates",
    title="Calculate rates between time points",
    category="Kinetics",
    sentence="Calculates the activity between each pair of consecutive time points from the mean concentrations, "
             "as the earlier Excel sheets did.",
    inputs=[Port("time_course", "table", "Time course")],
    outputs=[Port("interval_rates", "table", "Interval rates")],
    params=[BIOMASS],
    run=_run_intervals,
    explain=lambda p: sheet(
        formulas=[{"label": "Activity between two time points",
                   "latex": r"a_{j} = \frac{\bar{c}(t_j) - \bar{c}(t_{j-1})}{t_j - t_{j-1}} \cdot \frac{1000}{X}"}],
        symbols=[{"symbol": r"\bar{c}(t)", "meaning": "mean concentration at time t", "unit": "mM"},
                 {"symbol": "X", "meaning": "biomass", "unit": "gCDW/L"}],
        rules=["Uses means only, so it has no standard error; the initial rate (regression) is the recommended "
               "number."],
        code="(mean[i] - mean[i-1]) * 1000 / (t[i] - t[i-1]) / biomass",
    ),
    methods=None,
    example=lambda p, f: [] if not f.get("example") else [{
        "label": f"{f['example']['compound']}, {f['example']['strain']}, "
                 f"{f['example']['t0']:g}-{f['example']['t1']:g} min",
        "latex": rf"a = \frac{{{num(f['example']['c1'])} - {num(f['example']['c0'])}}}{{{num(f['example']['t1'])} - "
                 rf"{num(f['example']['t0'])}}} \cdot \frac{{1000}}{{{num(f['example']['bm'])}}} = "
                 rf"{num(f['example']['a'])}\ \mathrm{{U/g_{{CDW}}}}"}],
))


# ---------------------------------------------------------------------------------------------------- mass balance


def _run_mb(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    tc = inputs["time_course"]
    mb = tc.groupby(["strain", "substrate", "time_min"])["mean"].sum().reset_index(name="sum_mM")
    t0 = mb.groupby(["strain", "substrate"]).sum_mM.transform("first")
    mb["pct_of_t0"] = 100 * mb.sum_mM / t0
    last = mb.sort_values("time_min").groupby(["strain", "substrate"]).tail(1)
    return StepOut({"mass_balance": mb}, {"last": last.to_dict("records")})


register(StepType(
    id="kin.mass_balance",
    title="Check the mass balance",
    category="Kinetics",
    sentence="Adds up all quantified compounds at each time point and compares the sum with the first time point.",
    inputs=[Port("time_course", "table", "Time course")],
    outputs=[Port("mass_balance", "table", "Mass balance")],
    params=[],
    run=_run_mb,
    explain=lambda p: sheet(
        formulas=[{"label": "Sum of analytes relative to t0",
                   "latex": r"\mathrm{MB}(t) = 100\,\frac{\sum_k \bar{c}_k(t)}{\sum_k \bar{c}_k(t_0)}"}],
        rules=["All compounds count with weight 1 (equimolar conversion assumed); the internal standard is not a "
               "compound here.", "A drop means material goes to something the method does not measure."],
        code="time_course.groupby([strain, substrate, time_min])['mean'].sum()",
    ),
    methods=lambda p, f: "Mass balances were calculated as the sum of all quantified analytes relative to t0.",
    example=lambda p, f: [{"label": f"{r['substrate']}-fed {r['strain']} at {r['time_min']:g} min",
                           "latex": rf"\mathrm{{MB}} = {num(r['pct_of_t0'], 3)}\,\%"}
                          for r in (f.get("last") or [])[:4]],
))


# ---------------------------------------------------------------------------------------------------- plot


SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


def _run_plot(ctx: Ctx, inputs: dict, p: dict) -> StepOut:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tc = inputs["time_course"]
    if tc.empty:
        raise StepError("The time course is empty: nothing to plot.")
    panels = list(tc.groupby(["strain", "substrate"]))
    comps = list(dict.fromkeys(tc.compound))
    color = {c: SERIES[i % len(SERIES)] for i, c in enumerate(comps)}  # fixed slot per compound, never cycled
    cols = min(len(panels), 2)
    rows = (len(panels) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5.2 * cols, 3.6 * rows), squeeze=False, dpi=130)
    for ax, ((strain, sub), g) in zip(axes.flat, panels):
        for comp, gc in g.groupby("compound", sort=False):
            gc = gc.sort_values("time_min")
            ax.errorbar(gc.time_min, gc["mean"], yerr=gc.sd.fillna(0), marker="o", ms=4, capsize=3, lw=1.6,
                        color=color[comp], label=comp)
        ax.set_title(f"{sub}-fed {strain}", fontsize=10)
        ax.set_xlabel("Time (min)")
        ax.set_ylabel("Concentration (mM)")
        ax.grid(axis="y", color="#e6e8ec", lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    for ax in list(axes.flat)[len(panels):]:
        ax.set_visible(False)
    handles = [plt.Line2D([], [], color=color[c], marker="o", lw=1.6) for c in comps]
    fig.legend(handles, comps, loc="upper center", ncol=min(len(comps), 6), frameon=False,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return StepOut({"figure": buf.getvalue()}, {"panels": len(panels)})


register(StepType(
    id="plot.time_course",
    title="Plot the time courses",
    category="Plots",
    sentence="Draws mean concentration ± SD over time, one panel per strain and fed substrate, one fixed colour "
             "per compound.",
    inputs=[Port("time_course", "table", "Time course")],
    outputs=[Port("figure", "figure", "Time course figure")],
    params=[],
    run=_run_plot,
    explain=lambda p: sheet(rules=["Error bars are ± one standard deviation of the replicates."],
                            code="matplotlib errorbar(time_min, mean, yerr=sd)"),
))

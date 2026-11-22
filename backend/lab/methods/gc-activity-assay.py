"""GC-FID activity assay

Whole-cell activity assay measured by GC-FID: chromatograms, peaks at fixed retention times, internal-standard
ratios, calibration, concentrations, specific activities (U/gCDW), mass balance and quality checks. Derived from
gcassay; its numbers equal gcassay's.
"""

import numpy as np
import pandas as pd

from toolbox import combine_flags, fit_line, fit_line_through_zero, flag, flags_table, match_name
from toolbox.gc import PeakSettings, integrate_by_hand, integrate_peak, plot_time_courses, read_gaml, vendor_peak

# The kinds of correction an experiment can record, each with a reason; approved ones reach the recipe's `corrections`.
CORRECTIONS = {
    "rename": {
        "label": "Rename an injection",
        "help": "Fix a wrong injection name before it is read.",
        "fields": {"injection": "integer", "new_name": "text"},
        "key": ["injection"],
    },
    "exclude": {
        "label": "Leave out an injection, or one compound in it",
        "fields": {"injection": "integer", "compound": "optional text"},
    },
    "manual_integration": {
        "label": "Integrate a peak by hand",
        "help": "Set a peak's start and end by hand.",
        "fields": {"injection": "integer", "compound": "text", "start_min": "number", "end_min": "number"},
    },
}

# The experiment's own pages, made of cards (the panel's page builder writes this; it can be edited here).
PAGES = [
    {
        "title": "Peaks",
        "cards": [
            {
                "card": "review",
                "title": "Peaks",
                "regions": "peaks",
                "item": "injection",
                "name": "name",
                "group": "role",
                "label": "compound",
                "start": "start",
                "end": "end",
                "status": "status",
                "ok": ["ok", "n.d."],
                "status_labels": {
                    "ok": "found",
                    "<LOQ": "below LOQ",
                    "n.d.": "not detected",
                    "manual": "set by hand",
                    "excluded": "excluded",
                },
                "curve": {"file": "gaml_file"},
                "expected": "rt_expected",
                "tolerance": "rt_tolerance",
                "baseline": ["baseline_start_y", "baseline_end_y"],
                "info": [
                    {"column": "area", "label": "Area", "unit": "pA·min"},
                    {"column": "snr", "label": "S/N"},
                    {"column": "rt", "label": "Apex", "unit": "min"},
                    {"column": "vendor_area", "label": "Instrument's area", "unit": "pA·min"},
                ],
                "note": "note",
                "marker": "vendor_rt",
                "marker_label": "instrument",
                "window": 0.25,
                "x_label": "min",
                "y_label": "pA",
                "correct": "manual_integration",
                "start_field": "start_min",
                "end_field": "end_min",
                "exclude": "exclude",
            },
        ],
    },
]

DESIGN_COLUMNS = ["injection", "name", "original_name", "role", "strain", "time_min", "replicate", "substrate",
                  "std_conc_mM", "std_series", "collected"]

SAMPLE_PATTERNS = [
    {"role": "sample",
     "regex": r"^Act\.Assay_(?P<strain>[A-Za-z0-9]+)_(?P<time_min>\d+)min_R(?P<replicate>\d+)_"
              r"(?P<substrate>[A-Za-z0-9]+)$"},
    {"role": "standard", "regex": r"^.*_std_.*_(?P<std_conc_mM>[\d.]+)mM (?P<std_series>[A-Z])$"},
    {"role": "blank", "regex": r"^ether$"},
]


# ---------------------------------------------------------------- formulas: one calculation each, on numbers


def area_ratio(area, is_area):
    """A compound's peak area divided by the internal standard's area in the same injection."""
    return area / is_area


def concentration_mM(ratio, slope, intercept, concentration_factor):
    """The concentration: the calibration line solved for the concentration, times the concentration factor."""
    return (ratio - intercept) / slope * concentration_factor


def predicted_ratio(std_conc_mM, slope, intercept):
    """The area ratio the calibration line predicts for a known concentration."""
    return slope * std_conc_mM + intercept


def back_calculated_mM(ratio, slope, intercept):
    """A standard's concentration read back from its calibration line."""
    return (ratio - intercept) / slope


def residual_pct(back_calc_mM, std_conc_mM):
    """How far a standard reads back from its known concentration, in percent."""
    return 100 * (back_calc_mM / std_conc_mM - 1)


def detection_limit_mM(s_yx, slope):
    """Limit of detection (ICH Q2): 3.3 times the scatter around the calibration line, divided by its slope."""
    return 3.3 * s_yx / slope


def quantification_limit_mM(s_yx, slope):
    """Limit of quantification (ICH Q2): 10 times the scatter around the calibration line, divided by its slope."""
    return 10 * s_yx / slope


def specific_activity(rate_mM_per_min, biomass_gCDW_per_L):
    """Specific activity in U/gCDW: micromoles converted per minute per gram of cell dry weight (1 mM = 1000 µM)."""
    return rate_mM_per_min * 1000 / biomass_gCDW_per_L


def rate_between(conc_start_mM, conc_end_mM, time_start_min, time_end_min):
    """How fast the concentration changed between two time points, in mM per minute."""
    return (conc_end_mM - conc_start_mM) / (time_end_min - time_start_min)


def coefficient_of_variation_pct(sd, mean):
    """The spread of the replicates relative to their mean, in percent."""
    return 100 * sd / abs(mean)


def percent_of_start(sum_mM, start_sum_mM):
    """A sum of concentrations as a percentage of the sum at the first time point."""
    return 100 * sum_mM / start_sum_mM


def deviation_pct(value, reference):
    """How far a value is from a reference value, in percent (positive: above it)."""
    return 100 * (value / reference - 1)


def width_ratio(fwhm_min, reference_fwhm_min):
    """How many times wider a peak is than the same compound's peak in the standards."""
    return fwhm_min / reference_fwhm_min


def expected_retention_time(reference_rt_min, shift_min):
    """Where a compound's peak is expected: its reference time, moved as far as the internal standard moved."""
    return reference_rt_min + shift_min


def retention_shift(rt_min, reference_rt_min):
    """How far the internal standard's peak moved from its reference time in one injection."""
    return rt_min - reference_rt_min


# ---------------------------------------------------------------- steps: tables in, a table out


def gc_flag(level, category, message, injection="", compound=""):
    """One quality flag about an injection and a compound (either may be left empty)."""
    return flag(level, category, message, injection=injection, compound=compound)


def sample_names(injections, sample_patterns, corrections):
    """Turn each injection's name into its design: role, strain, time, replicate, substrate and standard.

    Each name must match one of the sample patterns (the first that matches wins); the pattern's named parts become
    columns. A rename correction replaces a wrong name before it is read.
    """
    new_names = renamed_injections(corrections)
    rows = []
    for injection in injections:
        name = new_names.get(injection.number, injection.name)
        role, parts = match_name(name, sample_patterns)
        row = {"injection": injection.number, "name": name, "original_name": injection.name, "role": role,
               "collected": injection.collected}
        row.update(parts)
        rows.append(row)
    samples = pd.DataFrame(rows)
    extra = [column for column in samples.columns if column not in DESIGN_COLUMNS]
    samples = samples.reindex(columns=DESIGN_COLUMNS + extra)
    for column in ["time_min", "replicate", "std_conc_mM"]:
        samples[column] = pd.to_numeric(samples[column], errors="coerce")
    return samples


def renamed_injections(corrections):
    """The new name of every injection a rename correction fixes: {injection number: new name}."""
    names = {}
    for correction in corrections:
        if correction["kind"] == "rename":
            names[int(correction["injection"])] = correction["new_name"]
    return names


def design_checks(samples, corrections):
    """Check the design read from the names: unknown names, duplicates, mislabelled samples and gaps.

    Errors: a name no pattern matches (that injection is left out), a sample named twice, a sample whose substrate
    differs from both its neighbours. Warnings: missing time points or replicates, a standard series lacking a
    concentration. Every rename is listed with its reason.
    """
    flags = []
    flags += unmatched_names(samples)
    flags += duplicate_samples(samples)
    flags += mislabelled_samples(samples)
    flags += missing_samples(samples)
    flags += incomplete_standards(samples)
    flags += rename_notes(corrections)
    return flags_table(flags)


def unmatched_names(samples):
    """An error for every name no sample pattern matches: such injections are left out."""
    flags = []
    for row in samples[samples.role == "unknown"].to_dict("records"):
        message = f"Name '{row['name']}' matches no sample pattern - it is ignored."
        flags.append(gc_flag("error", "design", message, row["injection"]))
    return flags


def duplicate_samples(samples):
    """An error for every sample (strain, substrate, time, replicate) that is named more than once."""
    real = samples[samples.role == "sample"]
    flags = []
    for _, group in real.groupby(["strain", "substrate", "time_min", "replicate"]):
        if len(group) > 1:
            numbers = ", ".join(f"#{number}" for number in group.injection)
            message = f"Duplicate sample name '{group['name'].iloc[0]}' at injections {numbers}."
            flags.append(gc_flag("error", "design", message, int(group.injection.iloc[-1])))
    return flags


def mislabelled_samples(samples):
    """An error for a sample whose substrate differs from both its neighbours, which are alike: almost always a
    typo in the name."""
    sequence = samples[samples.role == "sample"].sort_values("injection")
    substrates = list(sequence.substrate)
    numbers = list(sequence.injection)
    names = list(sequence["name"])
    flags = []
    for i in range(1, len(substrates) - 1):
        before, here, after = substrates[i - 1], substrates[i], substrates[i + 1]
        if here != before and here != after and before == after:
            message = (f"'{names[i]}' sits inside a block of {before} samples - probably mislabelled "
                       f"(should it be {before}?).")
            flags.append(gc_flag("error", "design", message, int(numbers[i])))
    return flags


def missing_samples(samples):
    """A warning for every strain and substrate that lacks a time point or replicate the others have."""
    real = samples[samples.role == "sample"]
    times = sorted(real.time_min.dropna().unique())
    replicates = sorted(real.replicate.dropna().unique())
    flags = []
    for (strain, substrate), group in real.groupby(["strain", "substrate"]):
        have = set(zip(group.time_min, group.replicate))
        missing = []
        for time in times:
            for replicate in replicates:
                if (time, replicate) not in have:
                    missing.append(f"{int(time)}min R{int(replicate)}")
        if missing:
            flags.append(gc_flag("warning", "design", f"{strain}/{substrate}: missing {', '.join(missing)}."))
    return flags


def incomplete_standards(samples):
    """A warning for a standard series that lacks a concentration another series has."""
    standards = samples[samples.role == "standard"]
    every = set(standards.std_conc_mM)
    flags = []
    for series, group in standards.groupby("std_series"):
        missing = sorted(every - set(group.std_conc_mM))
        if missing:
            flags.append(gc_flag("warning", "design", f"Standard series {series} lacks concentrations {missing}."))
    return flags


def rename_notes(corrections):
    """A note for every rename correction, with its reason."""
    flags = []
    for correction in corrections:
        if correction["kind"] == "rename":
            message = f"renamed to '{correction['new_name']}': {correction.get('reason', '')}"
            flags.append(gc_flag("info", "correction", message, correction["injection"]))
    return flags


def reference_retention_times(injections, samples, compounds, rt_tolerance_min, settings):
    """Find each compound's exact retention time and peak width in the calibration standards of this sequence.

    In every standard, the compound's peak nearest its configured time (within 2.5 times the tolerance) is measured.
    The median of the apex times becomes the reference time; the median width at half height is kept too, so a
    sample peak much narrower or wider than in the standards is not taken for the same compound.
    """
    if not compounds:
        raise ValueError("Give the compounds with their retention times in the settings (one row each: compound, "
                         "rt_min).")
    standard_numbers = set(samples[samples.role == "standard"].injection)
    rows = []
    for spec in compounds:
        compound = spec["compound"]
        if spec.get("rt_min") is None:
            raise ValueError(f"The compound {compound} needs an rt_min: its expected retention time in minutes.")
        tolerance = spec.get("rt_tolerance_min")
        if tolerance is None:
            tolerance = rt_tolerance_min
        times = []
        widths = []
        for injection in injections:
            if injection.number in standard_numbers:
                peak = integrate_peak(injection, compound, spec["rt_min"], tolerance * 2.5, settings)
                if peak["status"] == "ok":
                    times.append(peak["rt"])
                    widths.append(peak["fwhm"])
        rows.append({"compound": compound, "rt_config": float(spec["rt_min"]),
                     "rt_ref": float(np.median(times)) if times else float(spec["rt_min"]),
                     "fwhm_ref": float(np.median(widths)) if widths else np.nan,
                     "n_standards": len(times), "rt_tolerance": float(tolerance)})
    return pd.DataFrame(rows)


def integrate_peaks(injections, samples, references, internal_standard, corrections, rt_drift_correction, settings):
    """Find each compound's peak at its reference retention time in every injection and measure its area.

    The internal standard is measured first; with drift correction every other compound's expected time moves by
    how far the internal standard moved in that injection. Peaks set by hand replace the automatic ones (status
    "manual"); excluded peaks keep their numbers but get the status "excluded" and are not used.
    """
    compounds = list(references.compound)
    if internal_standard not in compounds:
        raise ValueError(f"The internal standard {internal_standard} is not one of the compounds "
                         f"({', '.join(compounds)}).")
    order = [internal_standard] + [compound for compound in compounds if compound != internal_standard]
    excluded = excluded_peaks(corrections, order)
    by_hand = manual_boundaries(corrections)
    named = set(samples[samples.role != "unknown"].injection)
    rows = []
    for injection in injections:
        if injection.number in named:
            rows += integrate_injection(injection, order, references, internal_standard, excluded, by_hand,
                                        rt_drift_correction, settings)
    peaks = pd.DataFrame(rows)
    return peaks.merge(samples, on="injection", how="left")


def excluded_peaks(corrections, compounds):
    """The peaks the exclusion corrections leave out, with their reasons: {(injection, compound): reason}.

    An exclusion without a compound leaves out every compound of that injection.
    """
    excluded = {}
    for correction in corrections:
        if correction["kind"] == "exclude":
            targets = [correction["compound"]] if correction.get("compound") else compounds
            for compound in targets:
                excluded[(int(correction["injection"]), compound)] = correction.get("reason", "")
    return excluded


def manual_boundaries(corrections):
    """The peaks measured between boundaries set by hand: {(injection, compound): the correction}."""
    boundaries = {}
    for correction in corrections:
        if correction["kind"] == "manual_integration":
            boundaries[(int(correction["injection"]), correction["compound"])] = correction
    return boundaries


def integrate_injection(injection, order, references, internal_standard, excluded, by_hand, rt_drift_correction,
                        settings):
    """Measure every compound's peak in one injection, the internal standard first (it may move the others)."""
    shift = 0.0
    rows = []
    for compound in order:
        reference = references[references.compound == compound].iloc[0]
        rt_expected = expected_retention_time(reference.rt_ref, shift)
        boundaries = by_hand.get((injection.number, compound))
        if boundaries:
            peak = integrate_by_hand(injection, compound, rt_expected, boundaries["start_min"], boundaries["end_min"],
                                     settings, boundaries.get("reason", ""))
        else:
            peak = integrate_peak(injection, compound, rt_expected, reference.rt_tolerance, settings,
                                  reference.fwhm_ref)
        if (injection.number, compound) in excluded:
            peak["status"] = "excluded"
            peak["note"] = excluded[(injection.number, compound)]
        if compound == internal_standard and rt_drift_correction and peak["status"] == "ok":
            shift = retention_shift(peak["rt"], reference.rt_ref)
        peak["injection"] = injection.number
        peak["rt_tolerance"] = reference.rt_tolerance
        peak["vendor_rt"], peak["vendor_area"] = vendor_peak(injection, compound)
        rows.append(peak)
    return rows


def area_ratios(peaks, internal_standard):
    """Divide each compound's peak area by the internal standard's area in the same injection.

    This cancels differences in pipetting and injection volume. An injection whose internal standard peak is not
    usable (not detected or excluded) gets no ratio, so none of its compounds is quantified.
    """
    standard = peaks[peaks.compound == internal_standard]
    if standard.empty:
        raise ValueError(f"No peaks of the internal standard {internal_standard}.")
    usable = standard[standard.status.isin(["ok", "manual"])]
    is_areas = usable[["injection", "area"]].rename(columns={"area": "is_area"})
    ratios = peaks[peaks.compound != internal_standard].merge(is_areas, on="injection", how="left")
    ratios["ratio"] = area_ratio(ratios.area, ratios.is_area)
    return ratios


def calibration_standards(ratios, cal_min_conc_mM, cal_max_conc_mM, cal_series):
    """Mark the standards the calibration lines are fitted to (column `used`).

    A standard is used when its concentration is within the range, it belongs to a chosen series (empty: all),
    and its peak was detected and not excluded. All series are pooled into one line per compound.
    """
    standards = ratios[ratios.role == "standard"].copy()
    low = 0.0 if cal_min_conc_mM is None else float(cal_min_conc_mM)
    high = np.inf if cal_max_conc_mM is None else float(cal_max_conc_mM)
    in_range = standards.std_conc_mM.between(low, high)
    measured = (standards.status != "excluded") & (standards.status != "n.d.") & standards.ratio.notna()
    standards["used"] = in_range & measured
    if cal_series:
        standards["used"] = standards.used & standards.std_series.isin(cal_series)
    return standards


def fit_calibration(standards, force_zero_intercept):
    """Fit a straight line through each compound's used standards: area ratio against known concentration.

    The detection and quantification limits come from the scatter of the standards around the line (ICH Q2).
    """
    if standards.empty:
        raise ValueError("No standards to calibrate with: no injection has the role 'standard'.")
    rows = []
    for compound, group in standards.groupby("compound"):
        used = group[group.used]
        if len(used) < 2:
            raise ValueError(f"{compound}: fewer than 2 usable standards, so no calibration line can be fitted.")
        if force_zero_intercept:
            line = fit_line_through_zero(used.std_conc_mM, used.ratio)
        else:
            line = fit_line(used.std_conc_mM, used.ratio)
        known = used.std_conc_mM
        rows.append({"compound": compound, "slope": line.slope, "intercept": line.intercept, "r2": line.r2,
                     "n_points": line.n, "range_mM": f"{known.min():g}-{known.max():g}", "max_conc_mM": known.max(),
                     "s_yx": line.s_yx, "lod_mM": detection_limit_mM(line.s_yx, line.slope),
                     "loq_mM": quantification_limit_mM(line.s_yx, line.slope)})
    return pd.DataFrame(rows)


def check_standards(standards, calibration):
    """Read every standard back from its compound's calibration line and say how far off it is, in percent."""
    lines = calibration[["compound", "slope", "intercept"]]
    points = standards.merge(lines, on="compound", how="left")
    points["predicted_ratio"] = predicted_ratio(points.std_conc_mM, points.slope, points.intercept)
    points["back_calc_mM"] = back_calculated_mM(points.ratio, points.slope, points.intercept)
    points["residual_pct"] = residual_pct(points.back_calc_mM, points.std_conc_mM)
    return points


def quantify(ratios, calibration, concentration_factor):
    """Turn each sample's and blank's area ratio into a concentration with its compound's calibration line.

    Not detected counts as 0 mM (as in the earlier Excel sheets); excluded peaks get no value. The flag column
    says which values need care: below the quantification limit, or above the highest standard used.
    """
    lines = calibration[["compound", "slope", "intercept", "loq_mM", "max_conc_mM"]]
    table = ratios[ratios.role.isin(["sample", "blank"])].merge(lines, on="compound", how="left")
    table["conc_mM"] = concentration_mM(table.ratio, table.slope, table.intercept, concentration_factor)
    table.loc[table.status == "n.d.", "conc_mM"] = 0.0
    table.loc[table.status == "excluded", "conc_mM"] = np.nan
    flags = []
    for row in table.to_dict("records"):
        flags.append(concentration_flag(row, concentration_factor))
    table["conc_flag"] = flags
    return table


def concentration_flag(row, concentration_factor):
    """Why a concentration needs care: excluded, not detected, above the calibrated range (more than 5 % above the
    highest standard used) or below the limit of quantification. Empty when it needs none."""
    if row["status"] == "excluded":
        return "excluded"
    if row["status"] == "n.d.":
        return "n.d."
    if row["conc_mM"] / concentration_factor > 1.05 * row["max_conc_mM"]:
        return "above calibration range"
    if row["conc_mM"] < row["loq_mM"] or row["status"] == "<LOQ":
        return "<LOQ"
    return ""


def time_course(concentrations):
    """Average the replicate concentrations of each compound at each time point.

    Gives the mean, standard deviation and number of replicates, the change since the first time point and the CV.
    Only quantified samples count: excluded peaks are left out, not detected counts as 0.
    """
    samples = concentrations[(concentrations.role == "sample") & concentrations.conc_mM.notna()]
    rows = []
    for (strain, substrate, compound, time), group in samples.groupby(["strain", "substrate", "compound",
                                                                        "time_min"]):
        rows.append({"strain": strain, "substrate": substrate, "compound": compound, "time_min": time,
                     "mean": group.conc_mM.mean(), "sd": group.conc_mM.std(), "n": group.conc_mM.count()})
    course = pd.DataFrame(rows)
    course["delta_vs_t0"] = course["mean"] - start_values(course, ["strain", "substrate", "compound"], "mean")
    course["cv_pct"] = coefficient_of_variation_pct(course.sd, course["mean"])
    return course


def start_values(table, keys, column):
    """For every row, the value of `column` at the first time point of its group (the rows with the same `keys`)."""
    first = {}
    for row in table.sort_values("time_min").to_dict("records"):
        group = tuple(row[key] for key in keys)
        if group not in first:
            first[group] = row[column]
    values = []
    for row in table.to_dict("records"):
        values.append(first[tuple(row[key] for key in keys)])
    return values


def initial_rates(concentrations, rate_window_min, biomass_gcdw_per_l, biomass_by_substrate):
    """Fit a straight line through all replicate concentrations in the early time window, for each compound.

    Its slope becomes a specific activity per gram of cell dry weight. The fed substrate's own rate is its
    consumption (negative); the other compounds are products. Every replicate value is one point of the line (not
    the means). An activity within twice its standard error of zero can't be told apart from zero.
    """
    start, end = rate_window_min
    samples = concentrations[(concentrations.role == "sample") & concentrations.conc_mM.notna()]
    rows = []
    for (strain, substrate, compound), group in samples.groupby(["strain", "substrate", "compound"]):
        biomass = biomass_for(substrate, biomass_gcdw_per_l, biomass_by_substrate)
        early = group[group.time_min.between(start, end)]
        line = fit_line(early.time_min, early.conc_mM)
        activity = specific_activity(line.slope, biomass)
        activity_se = specific_activity(line.se, biomass)
        rows.append({"strain": strain, "substrate_fed": substrate, "compound": compound,
                     "role": "substrate" if compound == substrate else "product", "window_min": f"{start}-{end}",
                     "n_points": len(early), "rate_mM_per_min": line.slope, "activity_U_per_gCDW": activity,
                     "activity_se": activity_se, "r2": line.r2, "biomass_gCDW_per_L": biomass,
                     "intercept_mM": line.intercept, "nonzero": nonzero_label(activity, activity_se)})
    return pd.DataFrame(rows)


def biomass_for(substrate, biomass_gcdw_per_l, biomass_by_substrate):
    """The cell dry weight (gCDW/L) for a fed substrate: its own row in the biomass per substrate, else the biomass
    for all."""
    for row in biomass_by_substrate or []:
        if row["substrate"] == substrate:
            return float(row["biomass_gcdw_per_l"])
    if biomass_gcdw_per_l is None:
        raise ValueError(f"No biomass for the substrate {substrate}: give the biomass (gCDW/L) in the settings, for "
                         "all substrates or per substrate.")
    return float(biomass_gcdw_per_l)


def nonzero_label(activity, activity_se):
    """Whether an activity can be told apart from zero: "yes" when it is more than twice its standard error."""
    if pd.isna(activity_se):
        return "no (all n.d.)"
    if abs(activity) > 2 * activity_se:
        return "yes"
    return "no"


def interval_rates(course, biomass_gcdw_per_l, biomass_by_substrate):
    """The activity between each pair of consecutive time points, from the mean concentrations.

    This is what the earlier Excel sheets did. It uses means only, so it has no standard error: the initial rate
    is the recommended number.
    """
    rows = []
    for (strain, substrate, compound), group in course.groupby(["strain", "substrate", "compound"]):
        biomass = biomass_for(substrate, biomass_gcdw_per_l, biomass_by_substrate)
        points = group.sort_values("time_min").to_dict("records")
        for before, after in zip(points, points[1:]):
            rate = rate_between(before["mean"], after["mean"], before["time_min"], after["time_min"])
            rows.append({"strain": strain, "substrate_fed": substrate, "compound": compound,
                         "interval_min": f"{before['time_min']:g}-{after['time_min']:g}",
                         "activity_U_per_gCDW": specific_activity(rate, biomass)})
    return pd.DataFrame(rows)


def mass_balance(course):
    """Add up all quantified compounds at each time point and compare the sum with the first time point's.

    Every compound counts with weight 1 (equimolar conversion assumed). A drop means material went somewhere the
    method does not measure.
    """
    rows = []
    for (strain, substrate, time), group in course.groupby(["strain", "substrate", "time_min"]):
        rows.append({"strain": strain, "substrate": substrate, "time_min": time, "sum_mM": group["mean"].sum()})
    balance = pd.DataFrame(rows)
    balance["start_sum_mM"] = start_values(balance, ["strain", "substrate"], "sum_mM")
    balance["pct_of_t0"] = percent_of_start(balance.sum_mM, balance.start_sum_mM)
    return balance


# ---------------------------------------------------------------- quality checks: each gives a table of flags


def reference_widths(references):
    """The peak width (FWHM, min) of each compound in the standards, for the compounds where it was measured."""
    widths = {}
    for row in references.to_dict("records"):
        if pd.notna(row["fwhm_ref"]):
            widths[row["compound"]] = row["fwhm_ref"]
    return widths


def internal_standard_checks(peaks, references, internal_standard, is_area_tolerance_pct, shoulder_fwhm_ratio):
    """Warn when the internal standard's area is far from the run's median, or its peak is much wider than in the
    standards: a compound eluting with it would bias every ratio of that injection low."""
    standard = peaks[(peaks.compound == internal_standard) & peaks.role.isin(["sample", "standard"])]
    median_area = standard.area.median()
    width = reference_widths(references).get(internal_standard)
    flags = []
    for row in standard.to_dict("records"):
        deviation = deviation_pct(row["area"], median_area)
        if abs(deviation) > is_area_tolerance_pct:
            message = f"IS area {deviation:+.0f}% vs. run median ({row['name']})."
            flags.append(gc_flag("warning", "internal standard", message, row["injection"], internal_standard))
        if width and row["fwhm"] > shoulder_fwhm_ratio * width:
            message = (f"IS peak {width_ratio(row['fwhm'], width):.1f}x wider than in standards - co-eluting "
                       "compound? All ratios of this injection are biased low.")
            flags.append(gc_flag("warning", "internal standard", message, row["injection"], internal_standard))
    return flags_table(flags)


def peak_shape_checks(peaks, references, internal_standard, shoulder_fwhm_ratio):
    """Warn about every compound peak much wider than in the standards: possibly two compounds in one peak."""
    widths = reference_widths(references)
    found = peaks[(peaks.compound != internal_standard) & (peaks.status == "ok")]
    flags = []
    for row in found.to_dict("records"):
        width = widths.get(row["compound"])
        if width and row["fwhm"] > shoulder_fwhm_ratio * width:
            message = f"Peak {width_ratio(row['fwhm'], width):.1f}x wider than in standards - possible co-elution."
            flags.append(gc_flag("warning", "peak shape", message, row["injection"], row["compound"]))
    return flags_table(flags)


def instrument_checks(peaks):
    """A note for every compound the instrument's own peak table placed far from its reference time: numbers taken
    from that table, as in a spreadsheet, would be a different compound."""
    labelled = peaks[peaks.vendor_rt.notna()]
    wrong = labelled[(labelled.vendor_rt - labelled.rt_expected).abs() > labelled.rt_tolerance]
    flags = []
    for compound, group in wrong.groupby("compound"):
        where = ", ".join(f"#{number} ({rt:.3f})" for number, rt in zip(group.injection, group.vendor_rt))
        message = (f"Chromeleon labelled a peak far from the {compound} reference RT ({group.rt_expected.median():.3f} "
                   f"min) in {len(group)} injections: {where}. Numbers taken from the instrument's own peak table (as "
                   "in a spreadsheet) would be a different compound.")
        flags.append(gc_flag("info", "Chromeleon assignment", message, "", compound))
    return flags_table(flags)


def calibration_checks(calibration, calibration_points, min_r2, max_residual_pct):
    """Warn about a calibration line with a low R², and about a used standard that reads back far from its known
    concentration."""
    flags = []
    for line in calibration.to_dict("records"):
        if line["r2"] < min_r2:
            flags.append(gc_flag("warning", "calibration", f"R^2 = {line['r2']:.4f}", "", line["compound"]))
    used = calibration_points[calibration_points.used]
    for point in used.to_dict("records"):
        if abs(point["residual_pct"]) > max_residual_pct:
            message = (f"Standard {point['std_conc_mM']:g} mM {point['std_series']}: back-calculated "
                       f"{point['residual_pct']:+.0f}%.")
            flags.append(gc_flag("warning", "calibration", message, point["injection"], point["compound"]))
    return flags_table(flags)


def concentration_checks(concentrations):
    """Warn about a sample above the calibrated range (extrapolated), and about any compound found in a blank
    (carry-over or contamination)."""
    flags = []
    samples = concentrations[concentrations.role == "sample"]
    for row in samples[samples.conc_flag == "above calibration range"].to_dict("records"):
        message = f"{row['conc_mM']:.3f} mM is above the calibrated range - extrapolated."
        flags.append(gc_flag("warning", "range", message, row["injection"], row["compound"]))
    blanks = concentrations[concentrations.role == "blank"]
    for row in blanks[blanks.status == "ok"].to_dict("records"):
        message = f"Detected in blank (S/N {row['snr']:.0f}) - carry-over or contamination."
        flags.append(gc_flag("warning", "blank", message, row["injection"], row["compound"]))
    return flags_table(flags)


def replicate_checks(course, max_replicate_cv_pct, cv_min_conc_mM):
    """Warn about a time point whose replicates disagree (CV above the limit), for means above a small
    concentration: near zero any scatter looks large."""
    spread = course[(course.cv_pct > max_replicate_cv_pct) & (course["mean"] > cv_min_conc_mM)]
    flags = []
    for row in spread.to_dict("records"):
        message = (f"{row['substrate']}-fed, {row['time_min']:g} min: replicate CV {row['cv_pct']:.0f}% "
                   f"(mean {row['mean']:.3f} mM).")
        flags.append(gc_flag("warning", "replicates", message, "", row["compound"]))
    return flags_table(flags)


def mass_balance_checks(balance, mass_balance_range_pct):
    """Warn when the sum of the analytes leaves the expected range around the first time point's sum."""
    low, high = mass_balance_range_pct
    flags = []
    for row in balance.to_dict("records"):
        if not low <= row["pct_of_t0"] <= high:
            message = (f"{row['substrate']}-fed, {row['time_min']:g} min: sum of analytes {row['pct_of_t0']:.0f}% "
                       "of t0.")
            flags.append(gc_flag("warning", "mass balance", message, "", row["substrate"]))
    return flags_table(flags)


def correction_notes(peaks):
    """A note for every peak measured by hand or excluded, with its reason."""
    corrected = peaks[peaks.status.isin(["manual", "excluded"])]
    flags = []
    for row in corrected.to_dict("records"):
        message = f"{row['status']}: {row['note']}"
        flags.append(gc_flag("info", "correction", message, row["injection"], row["compound"]))
    return flags_table(flags)


# ---------------------------------------------------------------- the recipe: the steps in order


def activity_assay(gaml_file, corrections, compounds, channel=None, sample_patterns=SAMPLE_PATTERNS,
                   internal_standard="octanol", rt_tolerance_min=0.012, rt_drift_correction=True, smoothing_points=11,
                   max_half_width_min=0.15, lod_snr=3, loq_snr=10, cal_min_conc_mM=None, cal_max_conc_mM=None,
                   cal_series=(), force_zero_intercept=False, concentration_factor=1.0, rate_window_min=(0, 30),
                   biomass_gcdw_per_l=None, biomass_by_substrate=(), is_area_tolerance_pct=25, shoulder_fwhm_ratio=1.3,
                   min_r2=0.995, max_residual_pct=15, max_replicate_cv_pct=20, cv_min_conc_mM=0.02,
                   mass_balance_range_pct=(80, 120)):
    """Whole-cell activity assay measured by GC-FID.

    Inputs:
        gaml_file: GC sequence, the GAML export from Chromeleon [.gaml]
        corrections: The experiment's approved corrections: renames, exclusions, peaks set by hand.

    Settings:
        Raw data:
            channel (text): Detector channel. The trace's name in the GAML file (e.g. BackDetectorFID); empty: the
                first trace.
        Samples:
            sample_patterns (rows: role text, regex text): Sample name patterns. One regular expression per role
                (sample, standard, blank), tried in order; named groups become columns: strain, time_min,
                replicate, substrate, std_conc_mM, std_series.
        Compounds:
            compounds (rows: compound text, rt_min number, optional rt_tolerance_min number): Compounds and
                retention times. Each compound with its expected retention time (min) and, if needed, its own
                tolerance.
            internal_standard: Internal standard. One of the compounds, e.g. octanol.
        Integration:
            rt_tolerance_min: Retention time tolerance. The apex must lie this close to the reference time.
            rt_drift_correction: Follow the internal standard's drift.
            smoothing_points: Smoothing window. Savitzky-Golay window (an odd number of points).
            max_half_width_min: Longest half-width.
            lod_snr: Detection limit (S/N).
            loq_snr: Quantification limit (S/N).
        Calibration:
            cal_min_conc_mM: Lowest standard used.
            cal_max_conc_mM: Highest standard used. Samples above it are extrapolated and flagged.
            cal_series (list): Standard series used. For example A, B; empty: all.
            force_zero_intercept: Force calibration through zero.
        Activity:
            concentration_factor: Concentration factor. For example 500 µl intended / 550 µl cells added = 0.909.
            rate_window_min: Initial rate window. Start and end of the linear range.
            biomass_gcdw_per_l: Biomass [gCDW/L]. Cell dry weight in the assay, for every fed substrate without
                its own row below.
            biomass_by_substrate (rows: substrate text, biomass_gcdw_per_l number): Biomass per fed substrate.
                A substrate's own cell dry weight, when it differs.
        Checks:
            is_area_tolerance_pct: Internal standard area tolerance.
            shoulder_fwhm_ratio: Widest acceptable peak (x standards).
            min_r2: Lowest acceptable calibration R².
            max_residual_pct: Largest standard residual.
            max_replicate_cv_pct: Largest replicate CV.
            cv_min_conc_mM: CV checked above.
            mass_balance_range_pct: Mass balance range [% of t0].

    Returns:
        activities: Specific activities
        time_course: Time course
        concentrations: Concentrations
        interval_rates: Interval rates
        mass_balance: Mass balance
        calibration: Calibration lines
        calibration_points: Calibration points
        peaks: Peaks
        flags: Quality flags
        figure: Time course figure
    """
    injections = read_gaml(gaml_file, channel)
    samples = sample_names(injections, sample_patterns, corrections)
    design_flags = design_checks(samples, corrections)
    settings = PeakSettings(smoothing_points, max_half_width_min, lod_snr, loq_snr)
    references = reference_retention_times(injections, samples, compounds, rt_tolerance_min, settings)
    peaks = integrate_peaks(injections, samples, references, internal_standard, corrections, rt_drift_correction,
                            settings)
    ratios = area_ratios(peaks, internal_standard)
    standards = calibration_standards(ratios, cal_min_conc_mM, cal_max_conc_mM, cal_series)
    calibration = fit_calibration(standards, force_zero_intercept)
    calibration_points = check_standards(standards, calibration)
    concentrations = quantify(ratios, calibration, concentration_factor)
    course = time_course(concentrations)
    activities = initial_rates(concentrations, rate_window_min, biomass_gcdw_per_l, biomass_by_substrate)
    intervals = interval_rates(course, biomass_gcdw_per_l, biomass_by_substrate)
    balance = mass_balance(course)
    standard_flags = internal_standard_checks(peaks, references, internal_standard, is_area_tolerance_pct,
                                              shoulder_fwhm_ratio)
    shape_flags = peak_shape_checks(peaks, references, internal_standard, shoulder_fwhm_ratio)
    instrument_flags = instrument_checks(peaks)
    calibration_flags = calibration_checks(calibration, calibration_points, min_r2, max_residual_pct)
    concentration_flags = concentration_checks(concentrations)
    replicate_flags = replicate_checks(course, max_replicate_cv_pct, cv_min_conc_mM)
    balance_flags = mass_balance_checks(balance, mass_balance_range_pct)
    correction_flags = correction_notes(peaks)
    flags = combine_flags(design_flags, standard_flags, shape_flags, instrument_flags, calibration_flags,
                          concentration_flags, replicate_flags, balance_flags, correction_flags)
    figure = plot_time_courses(course)
    return {"activities": activities, "time_course": course, "concentrations": concentrations,
            "interval_rates": intervals, "mass_balance": balance, "calibration": calibration,
            "calibration_points": calibration_points, "peaks": peaks, "flags": flags, "figure": figure}

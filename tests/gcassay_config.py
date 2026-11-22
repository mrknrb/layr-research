"""A test helper for test_lab_real.py: gcassay's experiment config and corrections file (YAML) read into the GC
method's settings and corrections (the plugin itself no longer reads them).

The config's sections map onto the method's settings; the compounds become rows (compound, rt_min,
rt_tolerance_min), a biomass per substrate becomes the biomass for all ("*") plus rows for the others; each correction
(rename, exclude, manual_integration) becomes an approved correction with its reason.
"""

from __future__ import annotations

from typing import Any

MAP = {
    ("integration", "rt_tolerance_min"): "rt_tolerance_min",
    ("integration", "rt_drift_correction"): "rt_drift_correction",
    ("integration", "smoothing_points"): "smoothing_points",
    ("integration", "max_half_width_min"): "max_half_width_min",
    ("integration", "lod_snr"): "lod_snr",
    ("integration", "loq_snr"): "loq_snr",
    ("calibration", "min_conc_mM"): "cal_min_conc_mM",
    ("calibration", "max_conc_mM"): "cal_max_conc_mM",
    ("calibration", "series"): "cal_series",
    ("calibration", "force_zero_intercept"): "force_zero_intercept",
    ("quantification", "concentration_factor"): "concentration_factor",
    ("activity", "rate_window_min"): "rate_window_min",
}
QC = ["is_area_tolerance_pct", "shoulder_fwhm_ratio", "min_r2", "max_residual_pct", "max_replicate_cv_pct",
      "cv_min_conc_mM", "mass_balance_range_pct"]


def parse_yaml(text: str) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("A gcassay file is a YAML mapping.")
    return data


def compound_rows(targets: dict[str, dict]) -> list[dict[str, Any]]:
    rows = []
    for name, spec in targets.items():
        row = {"compound": name, "rt_min": float(spec["rt"])}
        if spec.get("rt_tolerance_min") is not None:
            row["rt_tolerance_min"] = float(spec["rt_tolerance_min"])
        rows.append(row)
    return rows


def biomass(value: Any) -> dict[str, Any]:
    """{biomass_gcdw_per_l, biomass_by_substrate} from gcassay's one number or {substrate: number, "*": number}."""
    if not isinstance(value, dict):
        return {"biomass_gcdw_per_l": value}
    return {"biomass_gcdw_per_l": value.get("*"),
            "biomass_by_substrate": [{"substrate": k, "biomass_gcdw_per_l": float(v)} for k, v in value.items()
                                     if k != "*"]}


def params_from_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the method's settings, {title, raw_file, corrections, output_dir}) from a gcassay config."""
    comps = cfg.get("compounds") or {}
    exp = cfg.get("experiment") or {}
    p: dict[str, Any] = {"compounds": compound_rows(comps["targets"]),
                         "internal_standard": comps.get("internal_standard"), "channel": exp.get("channel")}
    if cfg.get("sample_patterns"):
        p["sample_patterns"] = [{"role": x["role"], "regex": x["regex"]} for x in cfg["sample_patterns"]]
    for (sec, key), name in MAP.items():
        if key in (cfg.get(sec) or {}):
            p[name] = cfg[sec][key]
    if "biomass_gcdw_per_l" in (cfg.get("activity") or {}):
        p.update(biomass(cfg["activity"]["biomass_gcdw_per_l"]))
    for key in QC:
        if key in (cfg.get("qc") or {}):
            p[key] = cfg["qc"][key]
    info = {"title": exp.get("title") or exp.get("id") or "GC experiment", "raw_file": exp.get("raw_file"),
            "corrections": exp.get("corrections")}
    return p, info


def corrections_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for kind in ("rename", "exclude", "manual_integration"):
        for item in data.get(kind) or []:
            c = {"id": f"c{len(out) + 1:03d}", "kind": kind, "injection": int(item["injection"]),
                 "reason": str(item.get("reason", "")).strip(), "status": "approved", "by": "gcassay"}
            if kind == "rename":
                c["new_name"] = item["new_name"]
            if item.get("compound"):
                c["compound"] = item["compound"]
            if kind == "manual_integration":
                c["start_min"], c["end_min"] = float(item["start_min"]), float(item["end_min"])
            out.append(c)
    return out

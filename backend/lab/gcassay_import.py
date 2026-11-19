"""Experiments from gcassay's files: its experiment config (YAML) and its corrections file.

The config's sections map onto the GC-FID activity assay method's settings; each correction (rename, exclude,
manual_integration) becomes an approved correction with its reason, marked as imported.
"""

from __future__ import annotations

from typing import Any

import yaml

from .errors import LabError
from .store import now

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
    ("activity", "biomass_gcdw_per_l"): "biomass_gcdw_per_l",
}
QC = ["is_area_tolerance_pct", "shoulder_fwhm_ratio", "min_r2", "max_residual_pct", "max_replicate_cv_pct",
      "cv_min_conc_mM", "mass_balance_range_pct"]


def parse_yaml(text: str, what: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise LabError(f"The {what} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise LabError(f"The {what} should be a YAML mapping.")
    return data


def params_from_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(method parameters, experiment info {title, raw_file, corrections, id}) from a gcassay config."""
    comps = (cfg.get("compounds") or {})
    if not comps.get("targets"):
        raise LabError("This doesn't look like a gcassay config: it has no compounds.targets.")
    exp = cfg.get("experiment") or {}
    p: dict[str, Any] = {"compounds": comps["targets"], "internal_standard": comps.get("internal_standard"),
                         "channel": exp.get("channel")}
    if cfg.get("sample_patterns"):
        p["sample_patterns"] = cfg["sample_patterns"]
    for (sec, key), name in MAP.items():
        if key in (cfg.get(sec) or {}):
            p[name] = cfg[sec][key]
    for key in QC:
        if key in (cfg.get("qc") or {}):
            p[key] = cfg["qc"][key]
    info = {"title": exp.get("title") or exp.get("id") or "GC experiment", "id": exp.get("id"),
            "raw_file": exp.get("raw_file"), "corrections": exp.get("corrections")}
    return p, info


def corrections_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for kind in ("rename", "exclude", "manual_integration"):
        for item in data.get(kind) or []:
            reason = str(item.get("reason", "")).strip()
            if not reason:
                raise LabError(f"Every correction needs a reason ({kind}: {item}).")
            c = {"id": f"c{len(out) + 1:03d}", "kind": kind, "injection": int(item["injection"]), "reason": reason,
                 "status": "approved", "by": "imported from gcassay", "at": now()}
            if kind == "rename":
                c["new_name"] = item["new_name"]
            if item.get("compound"):
                c["compound"] = item["compound"]
            if kind == "manual_integration":
                c["start_min"], c["end_min"] = float(item["start_min"]), float(item["end_min"])
            out.append(c)
    return out

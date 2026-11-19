"""The GC-FID method against gcassay on a real run: identical areas, ratios, concentrations, activities and flags.

Runs only when GCASSAY_PROJECT names a gcassay project folder with its raw GAML, a config and gcassay's results
(set GCASSAY_CONFIG too when the config isn't config/activity_assay_05.yaml). Raw data never leaves that folder: the
store here is a temporary one, and the method reads the GAML through it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab.gcassay_import import corrections_from, params_from_config, parse_yaml  # noqa: E402
from lab.runner import Runner  # noqa: E402
from lab.store import Store, new_id, now  # noqa: E402

PROJECT = Path(os.environ["GCASSAY_PROJECT"]) if os.environ.get("GCASSAY_PROJECT") else None
pytestmark = pytest.mark.skipif(PROJECT is None, reason="GCASSAY_PROJECT not set")
TOL = 1e-9


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    cfg_path = PROJECT / os.environ.get("GCASSAY_CONFIG", "config/activity_assay_05.yaml")
    cfg = parse_yaml(cfg_path.read_text("utf-8"), "config")
    params, info = params_from_config(cfg)
    corr_path = (cfg_path.parent / info["corrections"]).resolve()
    corr = corrections_from(parse_yaml(corr_path.read_text("utf-8"), "corrections")) if corr_path.exists() else []
    raw = (cfg_path.parent / info["raw_file"]).resolve()
    out = (cfg_path.parent / cfg["experiment"]["output_dir"]).resolve()
    if not raw.exists() or not (out / "results.xlsx").exists():
        pytest.skip("raw GAML or gcassay results missing")
    store = Store(tmp_path_factory.mktemp("lab"))
    exp = {"id": new_id("e"), "title": info["title"], "method": "gc-activity-assay",
           "inputs": {"gaml_file": store.add_file(raw)}, "params": params, "corrections": corr, "created": now()}
    store.save_experiment(exp)
    runner = Runner(store)
    try:
        run = runner.run(exp, store.get_method("gc-activity-assay"))
        assert run["status"] == "done", run.get("error")
        rerun = runner.run(exp, store.get_method("gc-activity-assay"))  # from the remembered GAML and peaks
        assert rerun["status"] == "done", rerun.get("error")
    finally:
        runner.pool.close()
    return store, run, rerun, out


def _same(ref: pd.DataFrame, mine: pd.DataFrame, keys: list[str], columns: list[str]) -> None:
    m = ref.merge(mine, on=keys, suffixes=("_ref", "_lab"))
    assert len(m) == len(ref) == len(mine), (len(ref), len(mine), len(m))
    for col in columns:
        a, b = m[f"{col}_ref"], m[f"{col}_lab"]
        assert (a.isna() == b.isna()).all(), col
        assert ((a - b).abs().fillna(0) < TOL).all(), (col, (a - b).abs().max())


def test_concentrations_identical(runs):
    store, run, _, out = runs
    ref = pd.read_csv(out / "concentrations.csv")
    mine = store.run_table(run["id"], "concentrations")
    _same(ref, mine, ["injection", "compound"], ["area", "is_area", "ratio", "conc_mM"])
    m = ref.merge(mine, on=["injection", "compound"], suffixes=("_ref", "_lab"))
    assert (m.conc_flag_ref.fillna("") == m.conc_flag_lab.fillna("")).all()


def test_activities_and_flags_identical(runs):
    store, run, _, out = runs
    ref = pd.read_excel(out / "results.xlsx", sheet_name="activities")
    mine = store.run_table(run["id"], "activities")
    _same(ref, mine, ["strain", "substrate_fed", "compound"], ["activity_U_per_gCDW", "activity_se", "r2"])
    m = ref.merge(mine, on=["strain", "substrate_fed", "compound"], suffixes=("_ref", "_lab"))
    assert (m.nonzero_ref == m.nonzero_lab).all()
    flags = pd.read_excel(out / "results.xlsx", sheet_name="qc_flags")
    mine_flags = store.run_table(run["id"], "flags")
    assert flags.level.value_counts().to_dict() == mine_flags.level.value_counts().to_dict()
    assert sorted(flags.category) == sorted(mine_flags.category)


def test_time_course_rates_and_balance_identical(runs):
    store, run, _, out = runs
    for sheet, keys, cols in [
        ("time_course", ["strain", "substrate", "compound", "time_min"], ["mean", "sd", "delta_vs_t0", "cv_pct"]),
        ("interval_rates", ["strain", "substrate_fed", "compound", "interval_min"], ["activity_U_per_gCDW"]),
        ("mass_balance", ["strain", "substrate", "time_min"], ["sum_mM", "pct_of_t0"]),
        ("calibration", ["compound"], ["slope", "intercept", "r2", "lod_mM", "loq_mM"]),
    ]:
        ref = pd.read_excel(out / "results.xlsx", sheet_name=sheet)
        _same(ref, store.run_table(run["id"], sheet), keys, cols)


def test_rerun_from_the_cache_is_identical(runs):
    store, run, rerun, _ = runs
    for name in ("concentrations", "activities", "peaks"):
        a, b = store.run_table(run["id"], name), store.run_table(rerun["id"], name)
        pd.testing.assert_frame_equal(a, b)
    assert rerun["ms"] < run["ms"]

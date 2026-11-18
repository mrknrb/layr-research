"""The GC-FID method against gcassay on a real run: identical areas, ratios, concentrations, activities and flags.

Runs only when GCASSAY_PROJECT names a gcassay project folder with its raw GAML, a config and gcassay's results
(set GCASSAY_CONFIG too when the config isn't config/activity_assay_05.yaml). Raw data never leaves that folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import engine  # noqa: E402
from lab.gcassay_import import corrections_from, params_from_config, parse_yaml  # noqa: E402
from lab.store import Store, new_id, now  # noqa: E402

PROJECT = Path(os.environ["GCASSAY_PROJECT"]) if os.environ.get("GCASSAY_PROJECT") else None
pytestmark = pytest.mark.skipif(PROJECT is None, reason="GCASSAY_PROJECT not set")


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
           "inputs": {"raw": store.add_file(raw)}, "params": params, "corrections": corr, "created": now()}
    store.save_experiment(exp)
    run = engine.Runner(store).run_experiment(exp)
    assert run["status"] == "done", run.get("error")
    return store, run, out


def test_concentrations_identical(runs):
    store, run, out = runs
    ref = pd.read_csv(out / "concentrations.csv")
    mine = store.run_table(run["id"], "concentrations")
    m = ref.merge(mine, on=["injection", "compound"], suffixes=("_ref", "_lab"))
    assert len(m) == len(ref) == len(mine)
    for col in ("area", "is_area", "ratio", "conc_mM"):
        assert (m[f"{col}_ref"] - m[f"{col}_lab"]).abs().max() < 1e-9, col
    assert (m.conc_flag_ref.fillna("") == m.conc_flag_lab.fillna("")).all()


def test_activities_and_flags_identical(runs):
    store, run, out = runs
    ref = pd.read_excel(out / "results.xlsx", sheet_name="activities")
    mine = store.run_table(run["id"], "activities")
    m = ref.merge(mine, on=["strain", "substrate_fed", "compound"], suffixes=("_ref", "_lab"))
    assert len(m) == len(ref)
    assert (m.activity_U_per_gCDW_ref - m.activity_U_per_gCDW_lab).abs().max() < 1e-9
    flags = pd.read_excel(out / "results.xlsx", sheet_name="qc_flags")
    mine_flags = store.run_table(run["id"], "flags")
    assert flags.level.value_counts().to_dict() == mine_flags.level.value_counts().to_dict()

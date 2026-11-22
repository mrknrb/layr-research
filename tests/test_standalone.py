"""A method exported as a folder runs without layr and gives the same numbers as the plugin.
Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab import examples, standalone  # noqa: E402
from lab.runner import Runner  # noqa: E402
from lab.store import Store, new_id, now  # noqa: E402


def test_exported_folder_gives_the_plugins_numbers(tmp_path: Path):
    store = Store(tmp_path / "lab")
    example = examples.get("gc")
    f = examples.stored_files(store, example)["gaml_file"]
    corrections = [{"id": "c1", "kind": "exclude", "injection": 20, "compound": "SUB", "reason": "pipetting error",
                    "status": "approved"}]
    exp = {"id": new_id("e"), "title": "Example", "method": "gc-activity-assay", "inputs": {"gaml_file": f},
           "params": example.SETTINGS, "corrections": corrections, "created": now()}
    store.save_experiment(exp)
    runner = Runner(store)
    try:
        run = runner.run(exp, store.get_method("gc-activity-assay"))
    finally:
        runner.pool.close()
    assert run["status"] == "done", run.get("error")

    folder = standalone.export_folder(store, store.get_method("gc-activity-assay"), tmp_path / "export", exp, run,
                                      include_raw=True)
    names = {p.name for p in folder.iterdir()}
    assert {"gc_activity_assay.py", "toolbox", "settings.json", "corrections.json", "inputs.json", "run.py",
            "requirements.txt", "README.txt", "data"} <= names
    inputs = json.loads((folder / "inputs.json").read_text("utf-8"))
    assert inputs["gaml_file"]["sha256"] == f["sha256"]
    assert json.loads((folder / "corrections.json").read_text("utf-8"))[0]["reason"] == "pipetting error"
    assert "pandas==" in (folder / "requirements.txt").read_text("utf-8")

    # run it alone: another Python process, started in the folder, with nothing of layr on its path
    out = subprocess.run([sys.executable, "-I", str(folder / "run.py"), "--out", str(tmp_path / "results")],
                         cwd=folder, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    for name in ("activities", "concentrations", "time_course", "flags", "peaks"):
        mine = pd.read_csv(tmp_path / "results" / f"{name}.csv")
        plugin = store.run_table(run["id"], name)
        pd.testing.assert_frame_equal(mine, plugin)
    assert (tmp_path / "results" / "figure.png").stat().st_size > 1000

    zipped = standalone.zip_folder(folder)
    assert zipped.suffix == ".zip" and zipped.stat().st_size > 5000

    # a raw file that isn't the analysed one is refused
    (folder / "data" / inputs["gaml_file"]["name"]).write_text("<GAML/>", "utf-8")
    inputs["gaml_file"]["path"] = ""
    (folder / "inputs.json").write_text(json.dumps(inputs), "utf-8")
    bad = subprocess.run([sys.executable, "-I", str(folder / "run.py")], cwd=folder, capture_output=True, text=True,
                         timeout=120)
    assert bad.returncode != 0 and "SHA-256" in bad.stderr

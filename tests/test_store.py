"""The store's format mark: a folder an earlier version of the plugin wrote is emptied once, then left alone."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab.store import FORMAT, Store  # noqa: E402


def test_a_new_folder_is_only_marked(tmp_path: Path):
    store = Store(tmp_path / "lab")
    assert store.started_fresh is False
    assert json.loads((tmp_path / "lab" / "format.json").read_text("utf-8"))["format"] == FORMAT
    assert (tmp_path / "lab" / "experiments").is_dir()


def test_an_older_folder_is_emptied_once(tmp_path: Path):
    root = tmp_path / "lab"
    (root / "experiments").mkdir(parents=True)
    (root / "experiments" / "e1.json").write_text("{}", "utf-8")
    (root / "methods" / "old-method").mkdir(parents=True)
    (root / "methods" / "old-method" / "1.py").write_text("x = 1", "utf-8")
    (root / "results.sqlite").write_bytes(b"old")
    store = Store(root)
    assert store.started_fresh is True
    assert not (root / "experiments" / "e1.json").exists()
    assert not (root / "methods" / "old-method").exists()
    assert not (root / "results.sqlite").exists()

    # from now on what is stored stays
    (root / "experiments" / "e2.json").write_text("{}", "utf-8")
    again = Store(root)
    assert again.started_fresh is False
    assert (root / "experiments" / "e2.json").exists()


def test_a_folder_with_another_format_is_emptied(tmp_path: Path):
    root = tmp_path / "lab"
    (root / "runs" / "r1").mkdir(parents=True)
    (root / "format.json").write_text(json.dumps({"format": FORMAT - 1}), "utf-8")
    assert Store(root).started_fresh is True
    assert not (root / "runs" / "r1").exists()

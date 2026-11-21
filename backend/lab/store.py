"""Where methods, experiments, raw files, runs and the PhD-wide results database live (LAYR_DATA_DIR/lab).

    files/<sha16>/<name>            raw files, never changed (the name is the original's; sha16 = start of SHA-256)
    methods/<id>/<n>.py             every version of a method made or changed here (built-in ones ship in
                                    lab/methods/<id>.py as version 1); <n>.meta.json: when, by whom, why
    methods/<id>/draft.py|.json     a change an AI agent proposed, waiting for the user (one per method)
    methods/<id>/method.json        the method's reference experiment
    methods/<id>/<n>.json           old step methods of Research 0.2 (kept, listed, no longer run)
    experiments/<id>.json           a method applied to raw files: settings and corrections
    runs/<id>/run.json              one run: what went in (checksums, settings, corrections), steps, outputs
    runs/<id>/method.py             the exact source it ran
    runs/<id>/outputs/, steps/      its tables (CSV) and figures (PNG); calls.json: every recorded call
    cache/                          remembered GAML files and peaks (toolbox/cache.py)
    exports/                        reports and folders written for the user
    results.sqlite                  every experiment's latest results, one table per method output
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .errors import LabError

BUILTIN = Path(__file__).resolve().parent / "methods"
ID = re.compile(r"^[a-z][a-z0-9-]{1,60}$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8]


def jsonable(v: Any) -> Any:
    """Plain JSON from numpy, pandas and dates (NaN and infinities become null)."""
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (str,)):
        return v
    if isinstance(v, pd.DataFrame):
        return records(v)
    return str(v)


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [jsonable(r) for r in df.to_dict(orient="records")]


def step_output(step: dict[str, Any]) -> dict[str, Any]:
    """A run step's result summary as a dict ({} for a step that gave a plain number or text, or nothing)."""
    out = step.get("output")
    return out if isinstance(out, dict) else {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(jsonable(value), indent=1, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8", newline="\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def method_name(source: str, fallback: str) -> tuple[str, str]:
    """(name, description) from a method file's module docstring."""
    from . import methodfile

    try:
        m = methodfile.parse(source)
    except SyntaxError:
        return fallback, ""
    return m.name or fallback, m.description


class Store:
    def __init__(self, root: Path):
        self.root = root
        for d in ("files", "methods", "experiments", "runs", "exports", "uploads", "cache"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self.db_path = root / "results.sqlite"

    # ------------------------------------------------------------------ raw files

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise LabError(f"{rel} is outside the store.")
        return p

    def add_file(self, src: Path, name: str | None = None, move: bool = False) -> dict[str, Any]:
        """Copies (or moves) a file into the store under its checksum; the same file twice is stored once."""
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        name = re.sub(r'[\\/:*?"<>|]+', "_", name or src.name).strip() or "file"
        dest = self.root / "files" / sha[:16] / name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(src), dest)
            else:
                shutil.copyfile(src, dest)
        elif move:
            src.unlink(missing_ok=True)
        return {"path": dest.relative_to(self.root).as_posix(), "name": name, "sha256": sha,
                "size": dest.stat().st_size, "added": now()}

    # ------------------------------------------------------------------ methods

    def _builtin(self) -> dict[str, dict]:
        out = {}
        for f in sorted(BUILTIN.glob("*.py")):
            source = f.read_text("utf-8")
            name, description = method_name(source, f.stem)
            out[f.stem] = {"id": f.stem, "version": 1, "builtin": True, "name": name, "description": description,
                           "source": source, "saved": None, "saved_by": "built in", "change": "Shipped with the plugin"}
        return out

    def _dir(self, method_id: str) -> Path:
        if not ID.match(method_id or ""):
            raise LabError(f"'{method_id}' is not a method id: lowercase letters, digits and dashes, starting with a "
                           "letter (e.g. growth-curve).")
        return self.root / "methods" / method_id

    def _versions_here(self, method_id: str) -> list[int]:
        d = self._dir(method_id)
        return sorted(int(p.stem) for p in d.glob("*.py") if p.stem.isdigit()) if d.exists() else []

    def _legacy_versions(self, method_id: str) -> list[int]:
        d = self._dir(method_id)
        return sorted(int(p.stem) for p in d.glob("*.json") if p.stem.isdigit()) if d.exists() else []

    def draft_only(self, method_id: str) -> bool:
        """A new method an AI agent proposed: it has a draft and nothing else yet."""
        mine = self._versions_here(method_id)
        return not mine and method_id not in self._builtin() and (self._dir(method_id) / "draft.py").exists()

    def is_legacy(self, method_id: str) -> bool:
        return not self._versions_here(method_id) and bool(self._legacy_versions(method_id)) \
            and method_id not in self._builtin()

    def method_versions(self, method_id: str) -> list[dict[str, Any]]:
        vs = []
        b = self._builtin().get(method_id)
        if b:
            vs.append({"version": 1, "at": None, "by": "built in", "summary": "Shipped with the plugin"})
        d = self._dir(method_id)
        for n in self._versions_here(method_id):
            meta = self._meta(d / f"{n}.meta.json")
            vs.append({"version": n, "at": meta.get("saved"), "by": meta.get("saved_by"),
                       "summary": meta.get("change") or "", "comparison": meta.get("comparison")})
        return vs

    @staticmethod
    def _meta(path: Path) -> dict[str, Any]:
        try:
            return _read_json(path)
        except (OSError, json.JSONDecodeError):
            return {}

    def get_method(self, method_id: str, version: int | None = None) -> dict[str, Any]:
        """A method's source and facts: {id, version, name, description, source, builtin, saved, saved_by, change}."""
        d = self._dir(method_id)
        mine = self._versions_here(method_id)
        pick = None
        if version is None and mine:
            pick = mine[-1]
        elif version is not None and version in mine:
            pick = version
        if pick is not None:
            source = (d / f"{pick}.py").read_text("utf-8")
            meta = self._meta(d / f"{pick}.meta.json")
            name, description = method_name(source, method_id)
            return {"id": method_id, "version": pick, "builtin": False, "name": name, "description": description,
                    "source": source, "saved": meta.get("saved"), "saved_by": meta.get("saved_by"),
                    "change": meta.get("change"), "builtin_base": method_id in self._builtin()}
        b = self._builtin().get(method_id)
        if b and version in (None, 1):
            return dict(b)
        if self._legacy_versions(method_id) and not mine:
            raise LabError(f"The method '{method_id}' is an old step method from Research 0.2: methods are Python "
                           "files now and it can't run any more. Make a new method (or copy the built-in one) and "
                           "point the experiment to it.")
        known = ", ".join(m["id"] for m in self.list_methods() if not m.get("legacy")) or "none"
        raise LabError(f"No method '{method_id}'" + (f" version {version}" if version else "")
                       + f". Methods: {known}.")

    def save_method(self, method_id: str, source: str, by: str, change: str,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Saves a new version (the next number) and gives its facts."""
        d = self._dir(method_id)
        numbers = self._versions_here(method_id) + self._legacy_versions(method_id)
        if method_id in self._builtin():
            numbers.append(1)
        version = (max(numbers) if numbers else 0) + 1
        _write_text(d / f"{version}.py", source)
        meta = {"version": version, "saved": now(), "saved_by": by, "change": change, **(extra or {})}
        _write_json(d / f"{version}.meta.json", meta)
        return self.get_method(method_id, version)

    def set_version_meta(self, method_id: str, version: int, **fields: Any) -> None:
        p = self._dir(method_id) / f"{version}.meta.json"
        if p.exists():
            meta = self._meta(p)
            meta.update(fields)
            _write_json(p, meta)

    def list_methods(self) -> list[dict[str, Any]]:
        from . import methodfile

        ids = set(self._builtin())
        ids |= {p.name for p in (self.root / "methods").iterdir() if p.is_dir() and ID.match(p.name)}
        out = []
        for mid in sorted(ids):
            if self.is_legacy(mid):
                versions = self._legacy_versions(mid)
                try:
                    old = _read_json(self._dir(mid) / f"{versions[-1]}.json")
                except (OSError, json.JSONDecodeError):
                    old = {}
                out.append({"id": mid, "name": old.get("name", mid), "version": versions[-1], "legacy": True,
                            "description": "An old step method from Research 0.2: it no longer runs.",
                            "builtin": False, "steps": 0, "inputs": [], "draft": False})
                continue
            if self.draft_only(mid):
                d = self.get_draft(mid)
                name, description = method_name(d["source"], mid)
                out.append({"id": mid, "name": name, "version": 0, "description": description, "steps": 0,
                            "builtin": False, "inputs": [], "draft": True, "draft_only": True})
                continue
            if not self._versions_here(mid) and mid not in self._builtin():
                continue
            m = self.get_method(mid)
            try:
                summary = methodfile.summary(methodfile.parse(m["source"]))
            except SyntaxError:
                summary = {"steps": 0, "inputs": []}
            out.append({"id": mid, "name": m["name"], "version": m["version"], "description": m["description"],
                        "steps": summary["steps"], "builtin": m["builtin"] or m.get("builtin_base", False),
                        "inputs": summary["inputs"], "draft": self.get_draft(mid) is not None,
                        "reference": self.method_settings(mid).get("reference")})
        return out

    def delete_method(self, method_id: str) -> None:
        d = self._dir(method_id)
        if method_id in self._builtin() and not d.exists():
            raise LabError("Built-in methods can't be deleted.")
        shutil.rmtree(d, ignore_errors=True)

    # drafts (changes an AI agent proposed) and settings

    def get_draft(self, method_id: str) -> dict[str, Any] | None:
        d = self._dir(method_id)
        if not (d / "draft.py").exists():
            return None
        meta = self._meta(d / "draft.json")
        return {**meta, "source": (d / "draft.py").read_text("utf-8")}

    def save_draft(self, method_id: str, source: str, meta: dict[str, Any]) -> dict[str, Any]:
        d = self._dir(method_id)
        _write_text(d / "draft.py", source)
        _write_json(d / "draft.json", meta)
        return self.get_draft(method_id)

    def update_draft(self, method_id: str, **fields: Any) -> None:
        d = self._dir(method_id)
        if (d / "draft.json").exists():
            meta = self._meta(d / "draft.json")
            meta.update(fields)
            _write_json(d / "draft.json", meta)

    def drop_draft(self, method_id: str) -> None:
        d = self._dir(method_id)
        (d / "draft.py").unlink(missing_ok=True)
        (d / "draft.json").unlink(missing_ok=True)

    def method_settings(self, method_id: str) -> dict[str, Any]:
        p = self._dir(method_id) / "method.json"
        return self._meta(p) if p.exists() else {}

    def set_method_settings(self, method_id: str, **fields: Any) -> dict[str, Any]:
        s = self.method_settings(method_id)
        for k, v in fields.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_json(self._dir(method_id) / "method.json", s)
        return s

    # ------------------------------------------------------------------ experiments

    def experiment_path(self, exp_id: str) -> Path:
        if not re.fullmatch(r"e[0-9a-f]{8}", exp_id or ""):
            raise LabError(f"'{exp_id}' is not an experiment id (they look like e1a2b3c4d; experiments lists "
                           "them).")
        return self.root / "experiments" / f"{exp_id}.json"

    def get_experiment(self, exp_id: str) -> dict[str, Any]:
        p = self.experiment_path(exp_id)
        if not p.exists():
            raise LabError(f"No experiment {exp_id}.")
        return _read_json(p)

    def save_experiment(self, exp: dict[str, Any]) -> dict[str, Any]:
        exp["updated"] = now()
        _write_json(self.experiment_path(exp["id"]), exp)
        return exp

    def list_experiments(self) -> list[dict[str, Any]]:
        out = []
        for f in (self.root / "experiments").glob("e*.json"):
            try:
                out.append(_read_json(f))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(out, key=lambda e: e.get("updated", ""), reverse=True)

    def delete_experiment(self, exp_id: str) -> None:
        self.experiment_path(exp_id).unlink(missing_ok=True)
        for r in self.list_runs(exp_id, trials=True):
            shutil.rmtree(self.root / "runs" / r["id"], ignore_errors=True)
        self.db_forget(exp_id)

    # ------------------------------------------------------------------ runs

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"r[0-9a-f]{8}", run_id or ""):
            raise LabError(f"'{run_id}' is not a run id (they look like r1a2b3c4d).")
        return self.root / "runs" / run_id

    def save_run(self, run: dict[str, Any]) -> None:
        _write_json(self.run_dir(run["id"]) / "run.json", run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        p = self.run_dir(run_id) / "run.json"
        if not p.exists():
            raise LabError(f"No run {run_id}.")
        return _read_json(p)

    def list_runs(self, exp_id: str | None = None, trials: bool = False) -> list[dict[str, Any]]:
        out = []
        for f in (self.root / "runs").glob("r*/run.json"):
            try:
                r = _read_json(f)
            except (OSError, json.JSONDecodeError):
                continue
            if (exp_id is None or r.get("experiment") == exp_id) and (trials or not r.get("trial")):
                out.append({k: r.get(k) for k in ("id", "experiment", "status", "started", "finished", "summary",
                                                  "method", "error", "trial")})
        return sorted(out, key=lambda r: r.get("started") or "", reverse=True)

    def run_table(self, run_id: str, ref: str) -> pd.DataFrame:
        run = self.get_run(run_id)
        out = self.output_file(run, ref)
        if not out or out.get("kind") != "table" or not out.get("file"):
            raise LabError(f"Run {run_id} has no table '{ref}'. Tables: {', '.join(self.table_names(run))}.")
        try:
            return pd.read_csv(self.run_dir(run_id) / out["file"])
        except pd.errors.EmptyDataError:
            return pd.DataFrame(columns=out.get("columns") or [])

    @staticmethod
    def table_names(run: dict[str, Any]) -> list[str]:
        names = [n for n, o in (run.get("outputs") or {}).items() if o.get("kind") == "table"]
        for s in run.get("steps") or []:
            if step_output(s).get("file") and s.get("function"):
                names.append(f"step:{s['function']}")
        return names

    @staticmethod
    def output_file(run: dict[str, Any], ref: str) -> dict[str, Any] | None:
        """An output by name ("activities"), or a step's table ("step:integrate_peaks")."""
        outs = run.get("outputs") or {}
        if ref in outs:
            return outs[ref]
        name = ref[5:] if ref.startswith("step:") else ref
        for s in run.get("steps") or []:
            if s.get("function") == name and step_output(s).get("file"):
                return {"kind": "table", **s["output"]}
        return None

    # ------------------------------------------------------------------ PhD-wide database

    def db_write(self, table: str, exp: dict[str, Any], run: dict[str, Any], df: pd.DataFrame) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", table):
            return
        new = df.copy()
        new.insert(0, "experiment", exp["id"])
        new.insert(1, "experiment_title", exp.get("title", ""))
        new.insert(2, "run", run["id"])
        new.insert(3, "run_finished", run.get("finished") or now())
        with sqlite3.connect(self.db_path) as con:
            exists = con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
            if exists:
                old = pd.read_sql(f'select * from "{table}"', con)
                old = old[old.experiment != exp["id"]]
                new = pd.concat([old, new], ignore_index=True)
            new.to_sql(table, con, if_exists="replace", index=False)

    def db_forget(self, exp_id: str) -> None:
        if not self.db_path.exists():
            return
        with sqlite3.connect(self.db_path) as con:
            for (t,) in con.execute("select name from sqlite_master where type='table'").fetchall():
                con.execute(f'delete from "{t}" where experiment = ?', (exp_id,))

    def db_tables(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with sqlite3.connect(self.db_path) as con:
            out = []
            for (t,) in con.execute("select name from sqlite_master where type='table' order by name").fetchall():
                cols = [r[1] for r in con.execute(f'pragma table_info("{t}")').fetchall()]
                n, e = con.execute(f'select count(*), count(distinct experiment) from "{t}"').fetchone()
                out.append({"table": t, "rows": n, "experiments": e, "columns": cols})
            return out

    def db_query(self, table: str, where: dict[str, Any] | None = None, limit: int = 500) -> pd.DataFrame:
        names = {t["table"]: t["columns"] for t in self.db_tables()}
        if table not in names:
            have = ", ".join(names) or "none yet (run an experiment first)"
            raise LabError(f"No results table '{table}'. Tables: {have}.")
        sql, args = f'select * from "{table}"', []
        conds = []
        for col, val in (where or {}).items():
            if col not in names[table]:
                raise LabError(f"No column '{col}' in {table}. Columns: {', '.join(names[table])}.")
            if isinstance(val, list):
                conds.append(f'"{col}" in ({",".join("?" * len(val))})')
                args += val
            else:
                conds.append(f'"{col}" = ?')
                args.append(val)
        if conds:
            sql += " where " + " and ".join(conds)
        sql += f" limit {int(max(1, min(limit, 100000)))}"
        with sqlite3.connect(self.db_path) as con:
            return pd.read_sql(sql, con, params=args)

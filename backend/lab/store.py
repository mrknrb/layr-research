"""Where methods, experiments, raw files, runs and the PhD-wide results database live (LAYR_DATA_DIR/lab).

    files/<sha16>/<name>          raw files, never changed (the name is the original's; sha16 = start of SHA-256)
    methods/<id>/<n>.json         every version of a method made here (built-in ones ship in lab/methods/)
    experiments/<id>.json         a method applied to raw files: parameter values and corrections
    runs/<id>/run.json            one run: what went in (checksums, parameters, corrections), every step's facts
    runs/<id>/<step>.<port>.csv   its tables (and .png figures)
    exports/                      reports written for the user
    results.sqlite                every experiment's latest results, one table per method output
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

from .core import StepError

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


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(jsonable(value), indent=1, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


class Store:
    def __init__(self, root: Path):
        self.root = root
        for d in ("files", "methods", "experiments", "runs", "exports", "uploads"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self.db_path = root / "results.sqlite"

    # ------------------------------------------------------------------ raw files

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise StepError(f"{rel} is outside the store.")
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
        for f in sorted(BUILTIN.glob("*.json")):
            m = _read_json(f)
            m.setdefault("version", 1)
            m["builtin"] = True
            out[m["id"]] = m
        return out

    def method_versions(self, method_id: str) -> list[dict[str, Any]]:
        vs = []
        b = self._builtin().get(method_id)
        if b:
            vs.append({"version": b["version"], "at": None, "by": "built in", "summary": "Shipped with the plugin"})
        d = self.root / "methods" / method_id
        for f in sorted(d.glob("*.json"), key=lambda p: int(p.stem)) if d.exists() else []:
            m = _read_json(f)
            vs.append({"version": m["version"], "at": m.get("saved"), "by": m.get("saved_by"),
                       "summary": m.get("change") or ""})
        return vs

    def get_method(self, method_id: str, version: int | None = None) -> dict[str, Any]:
        d = self.root / "methods" / method_id
        mine = sorted((int(p.stem) for p in d.glob("*.json")), reverse=True) if d.exists() else []
        if version is None and mine:
            return _read_json(d / f"{mine[0]}.json")
        if version is not None and version in mine:
            return _read_json(d / f"{version}.json")
        b = self._builtin().get(method_id)
        if b and (version is None or version == b["version"]):
            return b
        known = ", ".join(m["id"] for m in self.list_methods()) or "none"
        raise StepError(f"No method '{method_id}'" + (f" version {version}" if version else "")
                        + f". Methods: {known}.")

    def save_method(self, method: dict[str, Any], by: str, change: str) -> dict[str, Any]:
        mid = method.get("id") or ""
        if not ID.match(mid):
            raise StepError("A method id is lowercase letters, digits and dashes, starting with a letter "
                            "(e.g. growth-curve).")
        versions = [v["version"] for v in self.method_versions(mid)]
        m = {k: v for k, v in method.items() if k != "builtin"}
        m["version"] = (max(versions) if versions else 0) + 1
        m["saved"], m["saved_by"], m["change"] = now(), by, change
        _write_json(self.root / "methods" / mid / f"{m['version']}.json", m)
        return m

    def list_methods(self) -> list[dict[str, Any]]:
        ids = set(self._builtin())
        ids |= {p.name for p in (self.root / "methods").iterdir() if p.is_dir()}
        out = []
        for mid in sorted(ids):
            m = self.get_method(mid)
            out.append({"id": mid, "name": m.get("name", mid), "version": m["version"],
                        "description": m.get("description", ""), "steps": len(m.get("steps", [])),
                        "builtin": bool(m.get("builtin")), "inputs": m.get("inputs", [])})
        return out

    def delete_method(self, method_id: str) -> None:
        d = self.root / "methods" / method_id
        if method_id in self._builtin() and not d.exists():
            raise StepError("Built-in methods can't be deleted.")
        shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ experiments

    def experiment_path(self, exp_id: str) -> Path:
        if not re.fullmatch(r"e[0-9a-f]{8}", exp_id or ""):
            raise StepError(f"'{exp_id}' is not an experiment id (they look like e1a2b3c4d; experiments lists "
                            "them).")
        return self.root / "experiments" / f"{exp_id}.json"

    def get_experiment(self, exp_id: str) -> dict[str, Any]:
        p = self.experiment_path(exp_id)
        if not p.exists():
            raise StepError(f"No experiment {exp_id}.")
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
        for r in self.list_runs(exp_id):
            shutil.rmtree(self.root / "runs" / r["id"], ignore_errors=True)
        self.db_forget(exp_id)

    # ------------------------------------------------------------------ runs

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"r[0-9a-f]{8}", run_id or ""):
            raise StepError(f"'{run_id}' is not a run id (they look like r1a2b3c4d).")
        return self.root / "runs" / run_id

    def save_run(self, run: dict[str, Any]) -> None:
        _write_json(self.run_dir(run["id"]) / "run.json", run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        p = self.run_dir(run_id) / "run.json"
        if not p.exists():
            raise StepError(f"No run {run_id}.")
        return _read_json(p)

    def list_runs(self, exp_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for f in (self.root / "runs").glob("r*/run.json"):
            try:
                r = _read_json(f)
            except (OSError, json.JSONDecodeError):
                continue
            if exp_id is None or r.get("experiment") == exp_id:
                out.append({k: r.get(k) for k in ("id", "experiment", "status", "started", "finished", "summary",
                                                  "method", "error")})
        return sorted(out, key=lambda r: r.get("started") or "", reverse=True)

    def run_table(self, run_id: str, ref: str) -> pd.DataFrame:
        run = self.get_run(run_id)
        out = self.output_file(run, ref)
        if not out or out.get("kind") != "table":
            raise StepError(f"Run {run_id} has no table '{ref}'. Tables: {', '.join(self.table_names(run))}.")
        return pd.read_csv(self.run_dir(run_id) / out["file"])

    @staticmethod
    def table_names(run: dict[str, Any]) -> list[str]:
        names = list(run.get("outputs") or {})
        for s in run.get("steps") or []:
            for port, o in (s.get("outputs") or {}).items():
                if o.get("kind") == "table":
                    names.append(f"{s['id']}.{port}")
        return names

    @staticmethod
    def output_file(run: dict[str, Any], ref: str) -> dict[str, Any] | None:
        outs = run.get("outputs") or {}
        if ref in outs:
            ref = outs[ref]["from"]
        sid, _, port = ref.partition(".")
        for s in run.get("steps") or []:
            if s["id"] == sid:
                return (s.get("outputs") or {}).get(port)
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
            raise StepError(f"No results table '{table}'. Tables: {have}.")
        sql, args = f'select * from "{table}"', []
        conds = []
        for col, val in (where or {}).items():
            if col not in names[table]:
                raise StepError(f"No column '{col}' in {table}. Columns: {', '.join(names[table])}.")
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

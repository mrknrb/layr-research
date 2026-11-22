"""Which numbers moved between two runs (a method's reference experiment before and after a change).

Rows of the same table are paired by their identifying columns: of the columns both tables have that hold texts,
whole numbers or yes/no (never decimal numbers), the one that tells the most rows apart, then the next that tells
more apart, and so on until every row is told apart; a column that tells no more rows apart is not used. When those
columns together can't tell every row apart (in either table), rows are paired by their position. Then every number
that differs by more than a relative 1e-9, and every text that differs, is listed with its old and new value.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .store import Store, jsonable

REL_TOL = 1e-9


def _read(store: Store, run: dict[str, Any], name: str) -> pd.DataFrame | None:
    o = (run.get("outputs") or {}).get(name)
    if not o or o.get("kind") != "table":
        return None
    try:
        return pd.read_csv(store.run_dir(run["id"]) / o["file"])
    except (OSError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=o.get("columns") or [])


def _identifying(a: pd.DataFrame, b: pd.DataFrame, c: str) -> bool:
    """A column that can name rows: texts, whole numbers or yes/no in both tables (never decimal numbers)."""
    def kind(s: pd.Series) -> bool:
        return not pd.api.types.is_float_dtype(s) and (pd.api.types.is_integer_dtype(s) or pd.api.types.is_bool_dtype(s)
                                                       or pd.api.types.is_object_dtype(s)
                                                       or pd.api.types.is_string_dtype(s))
    return kind(a[c]) and kind(b[c])


def _keys(a: pd.DataFrame, b: pd.DataFrame) -> list[str] | None:
    """The columns that pair the rows of two tables (see the module's docstring), or None: by position."""
    candidates = [c for c in a.columns if c in b.columns and _identifying(a, b, c)]
    both = pd.concat([a[candidates], b[candidates]], ignore_index=True).astype(str) if candidates else None
    keys: list[str] = []
    told_apart = 1
    while candidates and (a.duplicated(keys).any() or b.duplicated(keys).any() if keys else True):
        counts = {c: len(both.drop_duplicates(keys + [c])) for c in candidates}
        best = max(candidates, key=lambda c: counts[c])  # the first of the best, in the table's order
        if counts[best] <= told_apart:
            break
        keys.append(best)
        candidates.remove(best)
        told_apart = counts[best]
    if keys and not a.duplicated(keys).any() and not b.duplicated(keys).any():
        return keys
    return None


def _row_key(row: dict[str, Any], keys: list[str]) -> str:
    return "|".join(str(jsonable(row[k])) for k in keys)


def _differs(x: Any, y: Any) -> bool:
    xn = isinstance(x, (int, float)) and not isinstance(x, bool)
    yn = isinstance(y, (int, float)) and not isinstance(y, bool)
    if xn and yn:
        fx, fy = float(x), float(y)
        if math.isnan(fx) and math.isnan(fy):
            return False
        if math.isnan(fx) or math.isnan(fy):
            return True
        return not math.isclose(fx, fy, rel_tol=REL_TOL, abs_tol=1e-12)
    if (x is None or (isinstance(x, float) and math.isnan(x))) and (y is None or (isinstance(y, float)
                                                                              and math.isnan(y))):
        return False
    return str(x) != str(y)


def compare_tables(a: pd.DataFrame, b: pd.DataFrame, limit: int = 50) -> dict[str, Any]:
    out: dict[str, Any] = {"rows_before": len(a), "rows_after": len(b),
                           "columns_added": [c for c in b.columns if c not in a.columns],
                           "columns_removed": [c for c in a.columns if c not in b.columns],
                           "rows_added": 0, "rows_removed": 0, "cells_changed": 0, "changes": []}
    keys = _keys(a, b)
    common = [c for c in a.columns if c in b.columns and c not in (keys or [])]
    if keys:
        a_by = {_row_key(r, keys): r for r in a.to_dict("records")}
        b_by = {_row_key(r, keys): r for r in b.to_dict("records")}
        out["rows_added"] = sum(1 for k in b_by if k not in a_by)
        out["rows_removed"] = sum(1 for k in a_by if k not in b_by)
        pairs = [(a_by[k], b_by[k]) for k in a_by if k in b_by]
        out["paired_by"] = keys
        for k in list(b_by)[:limit]:
            if k not in a_by and len(out["changes"]) < limit:
                out["changes"].append({"key": {c: jsonable(b_by[k][c]) for c in keys}, "row": "added"})
        for k in list(a_by)[:limit]:
            if k not in b_by and len(out["changes"]) < limit:
                out["changes"].append({"key": {c: jsonable(a_by[k][c]) for c in keys}, "row": "removed"})
    else:
        n = min(len(a), len(b))
        out["rows_added"] = max(0, len(b) - len(a))
        out["rows_removed"] = max(0, len(a) - len(b))
        pairs = list(zip(a.head(n).to_dict("records"), b.head(n).to_dict("records")))
        out["paired_by"] = "position"
    for i, (ra, rb) in enumerate(pairs):
        for c in common:
            if _differs(ra.get(c), rb.get(c)):
                out["cells_changed"] += 1
                if len(out["changes"]) < limit:
                    change = {"key": {k: jsonable(rb[k]) for k in keys} if keys else {"row": i + 1}, "column": c,
                              "before": jsonable(ra.get(c)), "after": jsonable(rb.get(c))}
                    try:
                        fa, fb = float(ra.get(c)), float(rb.get(c))
                        if fa and not math.isnan(fa) and not math.isnan(fb):
                            change["change_pct"] = 100 * (fb - fa) / abs(fa)
                    except (TypeError, ValueError):
                        pass
                    out["changes"].append(change)
    out["moved"] = bool(out["cells_changed"] or out["rows_added"] or out["rows_removed"] or out["columns_added"]
                        or out["columns_removed"])
    return out


def compare_runs(store: Store, before: dict[str, Any], after: dict[str, Any], limit: int = 50) -> dict[str, Any]:
    """Every output table of two runs compared; figures and other outputs are listed when they appear or go."""
    names = list(dict.fromkeys(list(before.get("outputs") or {}) + list(after.get("outputs") or {})))
    tables = []
    numbers = 0
    for name in names:
        oa = (before.get("outputs") or {}).get(name)
        ob = (after.get("outputs") or {}).get(name)
        if oa is None or ob is None:
            tables.append({"table": name, "added" if oa is None else "removed": True, "moved": True})
            continue
        if oa.get("kind") != "table" or ob.get("kind") != "table":
            continue
        result = compare_tables(_read(store, before, name), _read(store, after, name), limit)
        result["table"] = name
        numbers += result["cells_changed"]
        tables.append(result)
    moved = [t for t in tables if t.get("moved")]
    return {"before": {"run": before["id"], "method": before.get("method"), "status": before.get("status")},
            "after": {"run": after["id"], "method": after.get("method"), "status": after.get("status"),
                      "error": (after.get("error") or {}).get("message")},
            "numbers_changed": numbers, "tables_changed": len(moved), "tables": tables,
            "summary": (f"{numbers} numbers in {len(moved)} tables moved" if moved else "No number moved")
            if after.get("status") == "done" else f"The run with the change {after.get('status')}: "
                                                   f"{(after.get('error') or {}).get('message', '')}"}

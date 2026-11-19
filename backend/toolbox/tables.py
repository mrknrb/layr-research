"""Small helpers for tables: reading one from a file, quality flags."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd

FLAG_COLUMNS = ["level", "category", "injection", "compound", "message"]
LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def read_table(table_file: str | Path, sheet: str | None = None, separator: str | None = None,
               decimal: str | None = None) -> pd.DataFrame:
    """Read a table (CSV, TSV or Excel) into rows and named columns.

    The separator is guessed from , ; tab or | when not given; with ; a decimal comma (1,5) is understood. Empty
    rows are left out; the file itself is never changed.
    """
    path = Path(table_file)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        table = pd.read_excel(path, sheet_name=sheet or 0)
    else:
        text = path.read_text("utf-8-sig", errors="replace")
        if separator is None:
            head = text[:4096]
            separator = max([",", ";", "\t", "|"], key=head.count)
        if decimal is None:
            decimal = "," if separator == ";" else "."
        table = pd.read_csv(io.StringIO(text), sep=separator, decimal=decimal)
    table.columns = [str(c).strip() for c in table.columns]
    return table.dropna(how="all")


def flag(level: str, category: str, message: str, injection="", compound: str = "") -> dict:
    """One quality flag: level (error, warning or info), category, the injection and compound it is about, and a
    message saying what to look at."""
    return {"level": level, "category": category, "injection": injection, "compound": compound, "message": message}


def flags_table(flags: list[dict]) -> pd.DataFrame:
    """A table of quality flags (from `flag`), with the usual columns even when there are none."""
    return pd.DataFrame(flags, columns=FLAG_COLUMNS)


def combine_flags(*tables: pd.DataFrame) -> pd.DataFrame:
    """All quality flags in one table: errors first, then warnings, then notes; within those by category and
    injection."""
    parts = [t for t in tables if t is not None and len(t)]
    if not parts:
        return flags_table([])
    flags = pd.concat(parts, ignore_index=True).reindex(columns=FLAG_COLUMNS)
    flags["_o"] = flags.level.map(LEVEL_ORDER)
    flags["_i"] = pd.to_numeric(flags.injection, errors="coerce").fillna(np.inf)
    return flags.sort_values(["_o", "category", "_i"]).drop(columns=["_o", "_i"]).reset_index(drop=True)

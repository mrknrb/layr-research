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
    """Read a table (CSV, TSV, Excel, SPSS, Stata, SAS or JSON) into rows and named columns.

    The separator is guessed from , ; tab or | when not given; with ; a decimal comma (1,5) is understood. SPSS and
    Stata answers with labels come as their labels (e.g. "agree" rather than 4). Empty rows are left out; the file
    itself is never changed.
    """
    path = Path(table_file)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        table = pd.read_excel(path, sheet_name=sheet or 0)
    elif suffix in (".sav", ".zsav", ".por"):
        table = pd.read_spss(path, convert_categoricals=True)
        table = table.astype({c: "object" for c in table.columns if isinstance(table[c].dtype, pd.CategoricalDtype)})
    elif suffix == ".dta":
        table = pd.read_stata(path, convert_categoricals=True)
        table = table.astype({c: "object" for c in table.columns if isinstance(table[c].dtype, pd.CategoricalDtype)})
    elif suffix in (".sas7bdat", ".xpt"):
        table = pd.read_sas(path)
    elif suffix in (".json", ".jsonl"):
        table = pd.read_json(path, lines=suffix == ".jsonl")
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


def flag(level: str, category: str, message: str, injection="", compound: str = "", **about) -> dict:
    """One quality flag: level (error, warning or info), category, what it is about, and a message saying what to
    look at. A GC flag names the injection and compound; any other method names its own items as keywords, e.g.
    flag("warning", "answers", "Half the answers are missing", respondent=17)."""
    out = {"level": level, "category": category, "injection": injection, "compound": compound, "message": message}
    out.update(about)
    return out


def _flag_columns(columns) -> list[str]:
    extra = [c for c in columns if c not in FLAG_COLUMNS]
    return FLAG_COLUMNS[:4] + extra + ["message"]


def _drop_unused(flags: pd.DataFrame) -> pd.DataFrame:
    """A method that names its own items has no use for empty injection and compound columns."""
    if not any(c not in FLAG_COLUMNS for c in flags.columns):
        return flags
    unused = [c for c in ("injection", "compound") if flags[c].replace("", np.nan).isna().all()]
    return flags.drop(columns=unused)


def flags_table(flags: list[dict]) -> pd.DataFrame:
    """A table of quality flags (from `flag`), with the usual columns even when there are none."""
    columns = _flag_columns(dict.fromkeys(k for f in flags for k in f))
    return _drop_unused(pd.DataFrame(flags, columns=columns))


def combine_flags(*tables: pd.DataFrame) -> pd.DataFrame:
    """All quality flags in one table: errors first, then warnings, then notes; within those by category and
    injection."""
    parts = [t for t in tables if t is not None and len(t)]
    if not parts:
        return flags_table([])
    columns = _flag_columns(dict.fromkeys(c for t in parts for c in t.columns))
    flags = pd.concat(parts, ignore_index=True).reindex(columns=columns)
    flags["_o"] = flags.level.map(LEVEL_ORDER)
    flags["_i"] = pd.to_numeric(flags.injection, errors="coerce").fillna(np.inf)
    flags = flags.sort_values(["_o", "category", "_i"]).drop(columns=["_o", "_i"]).reset_index(drop=True)
    return _drop_unused(flags)

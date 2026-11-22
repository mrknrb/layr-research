"""layr research backend: an MCP server (FastMCP, stdio) for small data analyses.

Load a CSV table, look at it (describe), draw charts (plot) and compare groups (ttest, anova). Every result is kept
in a history, so the panel shows what an AI agent did too. Files live in LAYR_DATA_DIR:

    datasets/<id>.csv + <id>.json   the loaded tables (normalized CSV) and what we know about them
    plots/<id>.png                  every chart drawn
    history.jsonl                   one line per result, newest last

Run alone: `uv run backend/server.py` (it waits for MCP on stdin), or
`uv run --with "mcp[cli]<2" mcp dev backend/server.py` for the MCP Inspector. Tests: `uv run pytest tests -q`.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from mcp.server.fastmcp import FastMCP, Image  # noqa: E402
from pydantic import Field  # noqa: E402
from scipy import stats  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("research")

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("LAYR_DATA_DIR") or PLUGIN_DIR / ".layr-data")
DATASETS = DATA / "datasets"
PLOTS = DATA / "plots"
HISTORY = DATA / "history.jsonl"
SAMPLE = PLUGIN_DIR / "sample" / "plant_growth.csv"
for d in (DATASETS, PLOTS):
    d.mkdir(parents=True, exist_ok=True)

PREVIEW_ROWS = 10
MAX_GROUPS = 8  # categorical colors; more groups fold into "Other"

mcp = FastMCP(
    "research",
    instructions=(
        "Explainable data analysis for a researcher learning Python: any study whose data fits in tables (surveys, "
        "lab measurements, instrument exports...). Two parts. (1) Methods and experiments: a method is one plain "
        "Python file (methods, method_get show the source and its outline; toolbox_docs the ready-made functions it "
        "imports from `toolbox` and its add-ons `toolbox.<add-on>`); an "
        "experiment applies a method's recipe to raw files with settings and corrections (experiments, "
        "experiment_create, experiment_update); run computes it in a separate process; result_table, figure, explain "
        "(each step, its formulas drawn from the code, this run's numbers), explain_function and provenance read the "
        "results; compare_runs lists numbers that moved. "
        "Rules: every number you tell the user comes from result_table / provenance, with the run "
        "id; never compute statistics yourself. Change a method only with method_write (the whole file, or one "
        "function): your change is a DRAFT that counts only when the user accepts it in the panel; method_write "
        "returns the style checker's warnings: fix them (write again) before you tell the user it is ready; "
        "draft_get shows the changed lines and which numbers moved on the reference experiment. Never call "
        "draft_decide (it is the user's). Style the user can read, which the checker enforces: three levels in one "
        "file. Formulas: tiny functions on numbers, one formula each, arithmetic only (+ - * / ** and sqrt, log, "
        "abs, min, max; no pandas), so they also work on whole columns. Steps: functions that take tables and values "
        "and return a new table; pick rows, loop over groups ('for each group: calculate, add a row dict to a list, "
        "make a table with pd.DataFrame(rows)'), call the formulas; prefer a plain loop or a merge to clever pandas "
        "(no transform, set_index().map(), index tricks); never change a table you were given (copy it); about 25 "
        "lines at most, else split. The recipe (the last function, or RECIPE = 'name'): only calls steps, keeps "
        "their results and returns a dict of named results; its parameters are the inputs (names ending in _file are "
        "raw files; a parameter whose Inputs: line ends in [result] or [result: scores] reads results of other "
        "experiments: the experiment picks them with experiment_create/experiment_update sources={input: "
        "[{experiment, result}]} and the recipe gets one table, the picked tables stacked with the columns "
        "experiment and experiment_id in front; `corrections` gets the approved corrections) and the settings "
        "(defaults; keep existing setting "
        "names so stored experiments still work); its docstring documents them (Inputs:, Settings: with groups, "
        "Returns:). Every function starts with a docstring whose first line is one plain sentence; put a Formula: "
        "block in it for what isn't one expression. Units go in names (conc_mM, time_min, rate_mM_per_min). Imports: "
        "toolbox, pandas, numpy, scipy, statsmodels, scikit-learn (sklearn), matplotlib and the maths modules; any "
        "other package from PyPI goes into the method's REQUIREMENTS (method_declare what=requirements; installed "
        "before its next run); no file or network access (raw files come in through the recipe, read_table reads "
        "CSV, Excel, SPSS, Stata; results go out by returning them: tables, figures, single values, texts such as a "
        "model's summary). Quality checks: return a table named flags (toolbox flag(level, category, message, "
        "respondent=17) names any item). Corrections are of the kinds the method declares in CORRECTIONS "
        "(correction_kinds lists them; a method that declares none takes none); each needs a reason and "
        "stays proposed until the user approves it; don't call correction_decide yourself unless the user asks. "
        "Each experiment has pages the method declares in PAGES, made of cards (text, number, table, figure, chart, "
        "review: a curve per item with regions to correct); change them, the kinds of correction or the packages "
        "only with method_declare (a draft for you, like method_write). "
        "The flow: the user sees a method as a flow of boxes drawn from its recipe (one box per recipe line, wires "
        "are the names passed between them). Read it with flow_get (method, experiment or run; with a run every "
        "box has its status: done, cached (remembered from an earlier run with the same code and inputs, not "
        "calculated again), failed or not_run, and every wire its value). To change the recipe the way the flow "
        "does (add a step or a ready-made block from `blocks`, connect or disconnect an input, add a setting, an "
        "input or a result, group steps), call flow_edit: it returns the new source and saves nothing; then save it "
        "with method_write(source=...), which is a draft for you like any change. To see what happened inside a "
        "step in a run: function_inside (its code with the values of every line, loops' first and last rounds); "
        "a value with a `file` is a table: read its rows with value_get. run(source=..., settings=...) is a trial "
        "run of an unsaved change: it saves nothing and isn't the experiment's latest run; run(fresh=true) "
        "calculates every step again. Numbers you report still come only from runs (result_table, provenance, "
        "value_get, function_inside), with the run id. "
        "An experiment that reads others is `outdated` (experiments, experiment_get say why) when a source has newer "
        "numbers; it is never rerun by itself: run it when the user wants the new numbers. To compare experiments, "
        "use the built-in method compare-experiments (lab_sample which=compare shows it). "
        "To refer to a result, a number, a step or a method from another plugin (a Wiki page), put the markdown of "
        "reference_make there (a layr://research/open/... link); reference_get shows what a reference shows now. "
        "(2) Quick one-off analyses: load_csv (or load_sample) returns a dataset id; then describe, plot, ttest or "
        "anova it; list_datasets shows what is loaded."
    ),
)

ColumnType = Literal["integer", "number", "text", "date", "boolean"]


class ToolError(ValueError):
    """A mistake in the arguments, said so a person (or an AI) can fix it."""


# ---------- datasets ----------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8]


def _column_type(s: pd.Series) -> ColumnType:
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_numeric_dtype(s):
        non_null = s.dropna()
        if len(non_null) and np.all(np.equal(np.mod(non_null, 1), 0)) and non_null.abs().max() < 2**53:
            return "integer"
        return "number"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    return "text"


_BOOL = {"true": True, "false": False, "yes": True, "no": False, "igen": True, "nem": False}
_DATEISH = re.compile(r"^\d{1,4}[-./]\d{1,2}[-./]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")


def _infer_types(df: pd.DataFrame) -> pd.DataFrame:
    """Turns text columns that are really numbers, dates or yes/no into those."""
    for col in df.columns:
        s = df[col]
        if s.dtype != object:
            continue
        text = s.dropna().astype(str).str.strip()
        if text.empty:
            continue
        lowered = text.str.lower()
        if lowered.isin(_BOOL).all():
            df[col] = s.map(lambda v: _BOOL.get(str(v).strip().lower()) if pd.notna(v) else None).astype("boolean")
            continue
        nums = pd.to_numeric(text.str.replace(",", ".", regex=False), errors="coerce")
        if nums.notna().mean() >= 0.95:
            df[col] = pd.to_numeric(s.astype(str).str.strip().str.replace(",", ".", regex=False), errors="coerce")
            continue
        if text.str.match(_DATEISH).mean() >= 0.9:
            dates = pd.to_datetime(s, errors="coerce", format="mixed")
            if dates.notna().mean() >= 0.9:
                df[col] = dates
    return df


def _read_csv_text(content: str) -> pd.DataFrame:
    """Reads CSV text with , ; tab or | between values. With ; a decimal comma is allowed (European exports)."""
    content = content.lstrip("﻿")
    if not content.strip():
        raise ToolError("The CSV is empty.")
    sample = content[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    decimal = "," if delimiter == ";" else "."
    try:
        df = pd.read_csv(io.StringIO(content), sep=delimiter, decimal=decimal, skipinitialspace=True)
    except Exception as e:  # pandas raises several kinds
        raise ToolError(f"Couldn't read the CSV: {e}") from e
    if df.shape[1] == 0 or df.shape[0] == 0:
        raise ToolError("The CSV has no rows. The first line should name the columns, the rest hold the values.")
    df.columns = [str(c).strip() or f"column_{i + 1}" for i, c in enumerate(df.columns)]
    df = df.dropna(how="all")
    return _infer_types(df)


def _json_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float | np.floating):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_ | bool):
        return bool(v)
    if isinstance(v, pd.Timestamp | datetime):
        return v.isoformat()
    if v is pd.NA or v is pd.NaT:
        return None
    return v


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{str(k): _json_value(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def _meta_path(dataset: str) -> Path:
    if not re.fullmatch(r"d[0-9a-f]{8}", dataset or ""):
        raise ToolError(f"'{dataset}' is not a dataset id. Ids look like d1a2b3c4d; list_datasets shows them.")
    return DATASETS / f"{dataset}.json"


_cache: dict[str, pd.DataFrame] = {}


def _load(dataset: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    meta_file = _meta_path(dataset)
    if not meta_file.exists():
        known = ", ".join(m["dataset"] for m in _all_meta()) or "none yet"
        raise ToolError(f"No dataset '{dataset}'. Loaded datasets: {known}.")
    meta = json.loads(meta_file.read_text("utf-8"))
    if dataset not in _cache:
        dates = [c["name"] for c in meta["columns"] if c["type"] == "date"]
        df = pd.read_csv(DATASETS / f"{dataset}.csv", parse_dates=dates or False)
        for c in meta["columns"]:
            if c["type"] == "boolean":
                df[c["name"]] = df[c["name"]].astype("boolean")
        _cache[dataset] = df
    return _cache[dataset], meta


def _all_meta() -> list[dict[str, Any]]:
    metas = []
    for f in DATASETS.glob("d*.json"):
        try:
            metas.append(json.loads(f.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            log.warning("skipping unreadable %s", f)
    return sorted(metas, key=lambda m: m.get("loadedAt", ""), reverse=True)


def _summary(df: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset": meta["dataset"],
        "name": meta["name"],
        "rows": int(len(df)),
        "columns": meta["columns"],
        "preview": _records(df.head(PREVIEW_ROWS)),
    }


def _store(df: pd.DataFrame, name: str, source: str) -> dict[str, Any]:
    dataset = _new_id("d")
    df.to_csv(DATASETS / f"{dataset}.csv", index=False)
    meta = {
        "dataset": dataset,
        "name": name,
        "rows": int(len(df)),
        "columns": [{"name": str(c), "type": _column_type(df[c])} for c in df.columns],
        "source": source,
        "loadedAt": _now(),
    }
    (DATASETS / f"{dataset}.json").write_text(json.dumps(meta, indent=1), "utf-8")
    _cache[dataset] = df
    return meta


def _column(df: pd.DataFrame, name: str | None, what: str) -> str:
    """The real column name for `name` (exact, else ignoring case and spaces), or a clear error."""
    if not name:
        raise ToolError(f"Say which column to use as {what}. Columns: {', '.join(map(str, df.columns))}.")
    if name in df.columns:
        return name
    key = re.sub(r"[\s_]+", "", name).lower()
    for c in df.columns:
        if re.sub(r"[\s_]+", "", str(c)).lower() == key:
            return str(c)
    raise ToolError(f"No column '{name}' (for {what}). Columns: {', '.join(map(str, df.columns))}.")


def _numeric(df: pd.DataFrame, col: str, what: str) -> pd.Series:
    s = df[col]
    if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
        raise ToolError(f"Column '{col}' ({what}) must hold numbers, but it holds {_column_type(s)} values.")
    return s.astype(float)


# ---------- history and events ----------


def _record(tool: str, dataset: str | None, title: str, result: dict[str, Any], plot: str | None = None) -> dict:
    entry = {
        "id": _new_id("r"),
        "at": _now(),
        "tool": tool,
        "dataset": dataset,
        "title": title,
        "result": result,
        "plot": plot,
    }
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _emit_soon({"id": entry["id"], "tool": tool, "dataset": dataset, "title": title})
    return entry


def _history(limit: int) -> list[dict[str, Any]]:
    if not HISTORY.exists():
        return []
    lines = HISTORY.read_text("utf-8").splitlines()
    out = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _hub_known() -> bool:
    return bool(os.environ.get("LAYR_HUB_URL") and os.environ.get("LAYR_HUB_TOKEN"))


def _emit_soon(data: dict[str, Any], topic: str = "research:result") -> None:
    """Tells the plugin's panels (topic research:result, research:lab) through the hub, from the event loop or from
    a worker thread (lab/events.py). Best effort: no hub, no event."""
    EVENTS.emit(data, topic)


class _Hub:
    """One connection to the hub, kept open for every event (lab/events.py hands them over one at a time). Opening
    one costs the event loop a quarter of a second (the TLS settings of its HTTP client, made here once in a thread),
    so it is opened once, by one task that owns it. A connection that worked and then broke (the hub restarted, or
    closed it after a quiet hour) is opened again at once and the event sent again; when the hub can't be reached it
    is tried again only after RETRY_S, and the events in between are dropped (they are a courtesy to the panels)."""

    RETRY_S = 10

    def __init__(self) -> None:
        self.queue: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.tls = None
        self.retry_at = 0.0

    async def send(self, data: dict[str, Any], topic: str = "research:result") -> None:
        if self.task is None or self.task.done():
            self.queue = asyncio.Queue()
            self.task = asyncio.ensure_future(self._serve())
        done = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((data, topic, done))
        await done  # one at a time: lab/events.py skips progress a newer event follows while the hub is slow

    async def _serve(self) -> None:
        import ssl
        from datetime import timedelta

        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        def client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
            return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30, read=300), auth=auth,
                                     verify=self.tls)

        pending = None  # (data, topic, future) not sent yet
        while True:
            if pending is None:
                pending = await self.queue.get()
            if time.monotonic() < self.retry_at:
                _finish(pending[2])
                pending = None
                continue
            sent = False  # whether this connection delivered anything
            try:
                if self.tls is None:
                    self.tls = await asyncio.to_thread(ssl.create_default_context)
                headers = {"Authorization": f"Bearer {os.environ['LAYR_HUB_TOKEN']}"}
                async with streamablehttp_client(os.environ["LAYR_HUB_URL"], headers=headers,
                                                 httpx_client_factory=client) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                        await session.initialize()
                        while True:  # the connection stays open while the plugin waits for the next event
                            data, topic, done = pending
                            await session.call_tool("layr__events_emit", {"topic": topic, "data": data})
                            sent = True
                            _finish(done)
                            pending = await self.queue.get()
            except Exception as e:  # the hub is optional for this plugin
                log.info("couldn't emit an event through the hub: %s", e)
                if not sent:
                    self.retry_at = time.monotonic() + self.RETRY_S
                    _finish(pending[2])
                    pending = None



def _finish(done: asyncio.Future) -> None:
    if not done.done():  # its waiter may be gone (the server is stopping)
        done.set_result(None)


# ---------- charts ----------

THEMES = {
    "light": {
        "bg": "#ffffff", "fg": "#1d2129", "dim": "#646b78", "grid": "#e6e8ec",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    },
    "dark": {
        "bg": "#1d1f24", "fg": "#e6e7eb", "dim": "#9097a3", "grid": "#30343d",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
    },
}


def _groups(df: pd.DataFrame, col: str | None) -> list[tuple[str, pd.DataFrame]]:
    """Rows split by a column's values: at most MAX_GROUPS, the smallest folded into "Other"."""
    if not col:
        return [("", df)]
    keys = df[col].astype("string").fillna("(missing)")
    counts = keys.value_counts()
    if len(counts) > MAX_GROUPS:
        keep = set(counts.index[: MAX_GROUPS - 1])
        keys = keys.where(keys.isin(keep), "Other")
    # A control group goes first (it is what the others are compared with), "Other" and missing last.
    order = sorted(keys.unique(), key=lambda k: (k == "Other", k == "(missing)", str(k).lower() not in _CONTROL,
                                                 _natural(k)))
    return [(str(k), df[keys == k]) for k in order]


_CONTROL = {"control", "ctrl", "kontroll", "placebo", "untreated", "wt", "wild type"}


def _natural(s: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def _style(ax: plt.Axes, t: dict[str, Any], *, grid_axis: str = "y") -> None:
    ax.set_facecolor(t["bg"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["grid"])
    ax.tick_params(colors=t["dim"], labelsize=9, length=0, pad=6)
    ax.xaxis.label.set_color(t["dim"])
    ax.yaxis.label.set_color(t["dim"])
    ax.grid(axis=grid_axis, color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def _draw(df: pd.DataFrame, x: str | None, y: str | None, kind: str, group: str | None, title: str | None,
          theme: str) -> tuple[bytes, str]:
    t = THEMES[theme]
    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=130)
    fig.patch.set_facecolor(t["bg"])
    _style(ax, t, grid_axis="both" if kind == "scatter" else "y")
    colors = t["series"]
    series = 0

    if kind == "scatter":
        xs_col, ys_col = _column(df, x, "x"), _column(df, y, "y")
        _numeric(df, xs_col, "x")
        _numeric(df, ys_col, "y")
        for i, (name, part) in enumerate(_groups(df, group)):
            ax.scatter(part[xs_col], part[ys_col], s=38, color=colors[i % 8], edgecolors=t["bg"], linewidths=0.9,
                       label=name or None, zorder=3)
            series += 1
        ax.set_xlabel(xs_col)
        ax.set_ylabel(ys_col)
        default_title = f"{ys_col} against {xs_col}"
    elif kind == "line":
        xs_col, ys_col = _column(df, x, "x"), _column(df, y, "y")
        _numeric(df, ys_col, "y")
        for i, (name, part) in enumerate(_groups(df, group)):
            line = part.groupby(xs_col, sort=True)[ys_col].mean()
            ax.plot(line.index, line.values, color=colors[i % 8], linewidth=2, marker="o", markersize=4,
                    label=name or None, zorder=3)
            series += 1
        ax.set_xlabel(xs_col)
        ax.set_ylabel(f"{ys_col} (mean)" if df[xs_col].duplicated().any() else ys_col)
        default_title = f"{ys_col} over {xs_col}"
    elif kind == "hist":
        xs_col = _column(df, x, "x (the values to count)")
        values = _numeric(df, xs_col, "x").dropna()
        bins = np.histogram_bin_edges(values, bins="auto")
        if len(bins) > 31:
            bins = np.histogram_bin_edges(values, bins=30)
        parts = _groups(df, group)
        for i, (name, part) in enumerate(parts):
            ax.hist(part[xs_col].dropna(), bins=bins, color=colors[i % 8], alpha=0.6 if len(parts) > 1 else 0.9,
                    edgecolor=t["bg"], linewidth=1, label=name or None, zorder=3)
            series += 1
        ax.set_xlabel(xs_col)
        ax.set_ylabel("count")
        default_title = f"Distribution of {xs_col}"
    elif kind in ("box", "bar"):
        # y: the numbers; x (or group): the categories. With only x given and numeric, one box of x.
        cat = group or (x if y else None)
        val_col = _column(df, y or x, "y (the numbers)")
        _numeric(df, val_col, "y")
        if cat:
            cat = _column(df, cat, "x (the groups)")
        parts = _groups(df, cat)
        data = [p[val_col].dropna().astype(float).to_numpy() for _, p in parts]
        labels = [n or val_col for n, _ in parts]
        pos = np.arange(1, len(parts) + 1)
        rng = np.random.default_rng(0)
        if kind == "box":
            bp = ax.boxplot(data, positions=pos, widths=0.55, patch_artist=True, showfliers=False,
                            medianprops={"color": t["fg"], "linewidth": 1.6},
                            whiskerprops={"color": t["dim"]}, capprops={"color": t["dim"]},
                            boxprops={"linewidth": 0})
            for i, patch in enumerate(bp["boxes"]):
                patch.set_facecolor(colors[i % 8])
                patch.set_alpha(0.35)
        else:
            means = [d.mean() if len(d) else np.nan for d in data]
            sems = [stats.sem(d) if len(d) > 1 else 0 for d in data]
            ax.bar(pos, means, width=0.6, color=[colors[i % 8] for i in range(len(parts))], alpha=0.85,
                   yerr=sems, capsize=4, error_kw={"ecolor": t["fg"], "elinewidth": 1.2}, zorder=2)
        for i, d in enumerate(data):
            jitter = rng.uniform(-0.12, 0.12, len(d))
            ax.scatter(pos[i] + jitter, d, s=16, color=colors[i % 8], edgecolors=t["bg"], linewidths=0.6, zorder=3)
        ax.set_xticks(pos, labels)
        if cat:
            ax.set_xlabel(cat)
        ax.set_ylabel(f"{val_col} (mean ± SEM)" if kind == "bar" else val_col)
        default_title = f"{val_col} by {cat}" if cat else f"{val_col}"
    else:  # pragma: no cover - Literal guards it
        raise ToolError(f"Unknown kind '{kind}'.")

    if series > 1:
        leg = ax.legend(frameon=False, fontsize=9, title=group, title_fontsize=9, loc="best")
        for txt in leg.get_texts():
            txt.set_color(t["fg"])
        if leg.get_title():
            leg.get_title().set_color(t["dim"])
    final_title = title or default_title
    ax.set_title(final_title, color=t["fg"], fontsize=12, fontweight="bold", loc="left", pad=12)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=t["bg"])
    plt.close(fig)
    return buf.getvalue(), final_title


# ---------- statistics ----------


def _num_stats(s: pd.Series) -> dict[str, Any]:
    v = s.dropna().astype(float)
    if v.empty:
        return {"count": 0, "missing": int(s.isna().sum())}
    return {
        "count": int(v.count()),
        "missing": int(s.isna().sum()),
        "mean": _json_value(v.mean()),
        "sd": _json_value(v.std(ddof=1)) if len(v) > 1 else None,
        "sem": _json_value(stats.sem(v)) if len(v) > 1 else None,
        "min": _json_value(v.min()),
        "q1": _json_value(v.quantile(0.25)),
        "median": _json_value(v.median()),
        "q3": _json_value(v.quantile(0.75)),
        "max": _json_value(v.max()),
    }


def _fmt_p(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


# ---------- tools ----------


@mcp.tool()
async def load_csv(
    content: Annotated[str | None, Field(description="The CSV text itself (first line: column names).")] = None,
    path: Annotated[str | None, Field(description="Or: the path of a .csv file on this PC.")] = None,
    name: Annotated[str | None, Field(description="A name to show for it, e.g. the file name.")] = None,
) -> dict[str, Any]:
    """Load a table from CSV and keep it as a dataset for the other tools.

    Give either `content` (the CSV text) or `path` (a file on the PC). Values may be separated by commas,
    semicolons, tabs or |; with semicolons a decimal comma (1,5) is understood. Numbers, dates and yes/no columns
    are recognized. Returns {dataset, name, rows, columns: [{name, type}], preview: first 10 rows}; pass `dataset`
    (an id like d1a2b3c4d) to describe, plot, ttest and anova.
    """
    if bool(content) == bool(path):
        raise ToolError("Give either content (the CSV text) or path (a .csv file), not both or neither.")
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"No file at {p}.")
        text = p.read_text("utf-8-sig", errors="replace")
        name = name or p.stem
        source = str(p)
    else:
        text = content or ""
        source = "content"
    df = await asyncio.to_thread(_read_csv_text, text)
    meta = _store(df, (name or "Table").strip()[:80] or "Table", source)
    out = _summary(df, meta)
    _record("load_csv", meta["dataset"], f"Loaded {meta['name']} ({len(df)} rows)",
            {"rows": len(df), "columns": meta["columns"]})
    log.info("loaded %s: %s rows from %s", meta["dataset"], len(df), source)
    return out


@mcp.tool()
async def load_sample() -> dict[str, Any]:
    """Load the example dataset: plant growth by treatment (control, nitrogen, compost) and light (shade, sun).

    48 made-up plants with height_cm, leaves, dry_mass_g and germination_days. Returns the same as load_csv.
    """
    return await load_csv(path=str(SAMPLE), name="Plant growth (sample)")


@mcp.tool()
def list_datasets() -> list[dict[str, Any]]:
    """List the loaded datasets, newest first: [{dataset, name, rows, columns, loadedAt}]."""
    return [{k: m.get(k) for k in ("dataset", "name", "rows", "columns", "loadedAt")} for m in _all_meta()]


@mcp.tool()
def preview(
    dataset: Annotated[str, Field(description="The dataset id from load_csv or list_datasets.")],
    offset: Annotated[int, Field(ge=0, description="Rows to skip.")] = 0,
    limit: Annotated[int, Field(ge=1, le=500, description="Rows to return (at most 500).")] = 50,
) -> dict[str, Any]:
    """Rows of a dataset: {dataset, name, rows (total), columns, offset, data: [{column: value}]}."""
    df, meta = _load(dataset)
    return {
        "dataset": dataset,
        "name": meta["name"],
        "rows": int(len(df)),
        "columns": meta["columns"],
        "offset": offset,
        "data": _records(df.iloc[offset : offset + limit]),
    }


@mcp.tool()
async def describe(
    dataset: Annotated[str, Field(description="The dataset id from load_csv or list_datasets.")],
    by: Annotated[str | None, Field(description="Optional: a column to group by, e.g. treatment.")] = None,
    remember: Annotated[bool, Field(description="false: don't add it to the results history.")] = True,
) -> dict[str, Any]:
    """Summary statistics for every column of a dataset.

    Number columns: count, missing, mean, sd, sem, min, q1, median, q3, max. Text and yes/no columns: count,
    missing, unique and the most common values. With `by`, the number columns are also summarized per group
    (n, mean, sd, sem, median, min, max). Returns {dataset, rows, columns: [{name, type, ...stats}] in table order,
    and with `by`: by, groups: [{group, n, stats: {column: {...}}}] (a control group first).
    """
    df, meta = _load(dataset)

    def work() -> dict[str, Any]:
        cols: list[dict[str, Any]] = []
        for c in meta["columns"]:
            s = df[c["name"]]
            if c["type"] in ("integer", "number"):
                cols.append({"name": c["name"], "type": c["type"], **_num_stats(s)})
            elif c["type"] == "date":
                v = s.dropna()
                cols.append({"name": c["name"], "type": "date", "count": int(v.count()), "missing": int(s.isna().sum()),
                             "min": _json_value(v.min()) if len(v) else None,
                             "max": _json_value(v.max()) if len(v) else None})
            else:
                v = s.dropna().astype(str)
                top = v.value_counts().head(8)
                cols.append({"name": c["name"], "type": c["type"], "count": int(v.count()),
                             "missing": int(s.isna().sum()), "unique": int(v.nunique()),
                             "top": [{"value": k, "count": int(n)} for k, n in top.items()]})
        out: dict[str, Any] = {"dataset": dataset, "name": meta["name"], "rows": int(len(df)), "columns": cols}
        if by:
            g = _column(df, by, "by")
            numeric = [c["name"] for c in meta["columns"] if c["type"] in ("integer", "number") and c["name"] != g]
            groups: list[dict[str, Any]] = []
            for name, part in _groups(df, g):
                per: dict[str, Any] = {}
                for col in numeric:
                    st = _num_stats(part[col])
                    per[col] = {k: st.get(k) for k in ("count", "mean", "sd", "sem", "median", "min", "max")}
                groups.append({"group": name, "n": int(len(part)), "stats": per})
            out["by"] = g
            out["groups"] = groups
        return out

    out = await asyncio.to_thread(work)
    if remember:
        _record("describe", dataset, f"Described {meta['name']}" + (f" by {out.get('by')}" if by else ""),
                {"by": out.get("by")})
    return out


@mcp.tool(structured_output=False)
async def plot(
    dataset: Annotated[str, Field(description="The dataset id from load_csv or list_datasets.")],
    kind: Annotated[
        Literal["scatter", "line", "hist", "box", "bar"],
        Field(description="scatter: y against x. line: y over x (mean per x). hist: how x's values spread. "
              "box: y's spread per category in x, with the points. bar: y's mean ± SEM per category in x."),
    ] = "scatter",
    x: Annotated[str | None, Field(description="Column for the x axis (for box/bar: the categories).")] = None,
    y: Annotated[str | None, Field(description="Column for the y axis (numbers). Not used by hist.")] = None,
    group: Annotated[str | None, Field(description="Optional: a column whose values get their own color.")] = None,
    title: Annotated[str | None, Field(description="Optional chart title.")] = None,
    theme: Annotated[Literal["light", "dark"], Field(description="Colors for a light or dark background.")] = "light",
) -> list[Any]:
    """Draw a chart of a dataset and return it as a PNG image (also saved on the PC; the text says where).

    Examples: {kind: "box", x: "treatment", y: "height_cm"}; {kind: "scatter", x: "height_cm", y: "dry_mass_g",
    group: "treatment"}; {kind: "hist", x: "height_cm"}. Column names are matched ignoring case.
    """
    df, meta = _load(dataset)
    png, final_title = await asyncio.to_thread(_draw, df, x, y, kind, group, title, theme)
    plot_id = _new_id("g")
    path = PLOTS / f"{plot_id}.png"
    path.write_bytes(png)
    _record("plot", dataset, final_title, {"kind": kind, "x": x, "y": y, "group": group, "path": str(path)},
            plot=plot_id)
    return [Image(data=png, format="png"), f"{final_title} ({kind} chart of {meta['name']}), plot {plot_id}, "
            f"saved to {path}"]


@mcp.tool()
async def ttest(
    dataset: Annotated[str, Field(description="The dataset id from load_csv or list_datasets.")],
    column: Annotated[str, Field(description="The number column to compare, e.g. height_cm.")],
    group: Annotated[str, Field(description="The column that says which group each row is in, e.g. treatment.")],
    groups: Annotated[
        list[str] | None,
        Field(description="The two groups to compare, e.g. [\"control\", \"nitrogen\"]. Needed when the group "
              "column has more than two values."),
    ] = None,
    equal_var: Annotated[bool, Field(description="false: Welch's test (safer, the default); true: Student's.")] = False,
) -> dict[str, Any]:
    """Compare the means of two groups with a two-sample t-test (Welch's by default).

    Returns {test, column, group, groups: [{group, n, mean, sd}] (in the order compared), mean_difference
    (first − second), t, df, p,
    ci95: [low, high] of the difference, cohens_d, significant (p < 0.05), interpretation (one sentence)}.
    For three or more groups use anova.
    """
    df, meta = _load(dataset)
    val = _column(df, column, "column")
    _numeric(df, val, "column")
    g = _column(df, group, "group")
    keys = df[g].astype("string")
    levels = sorted(keys.dropna().unique(), key=_natural)
    if groups:
        if len(groups) != 2:
            raise ToolError("Give exactly two groups to compare.")
        chosen = []
        for want in groups:
            match = next((lv for lv in levels if lv == want), None) or next(
                (lv for lv in levels if lv.lower() == str(want).lower()), None)
            if match is None:
                raise ToolError(f"No group '{want}' in '{g}'. Groups: {', '.join(levels)}.")
            chosen.append(match)
    elif len(levels) == 2:
        chosen = levels
    else:
        raise ToolError(f"'{g}' has {len(levels)} groups ({', '.join(levels)}): say which two with groups, "
                        "or use anova to compare them all.")
    a = df.loc[keys == chosen[0], val].dropna().astype(float)
    b = df.loc[keys == chosen[1], val].dropna().astype(float)
    if len(a) < 2 or len(b) < 2:
        raise ToolError("Each group needs at least two values.")

    res = stats.ttest_ind(a, b, equal_var=equal_var)
    ci = res.confidence_interval(0.95)
    pooled = math.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
    d = (a.mean() - b.mean()) / pooled if pooled else float("nan")
    p = float(res.pvalue)
    diff = a.mean() - b.mean()
    sig = p < 0.05
    direction = "higher" if diff > 0 else "lower"
    interpretation = (
        f"{val} is {direction} in {chosen[0]} than in {chosen[1]} by {abs(diff):.3g} on average "
        f"({_fmt_p(p)}): {'a significant difference' if sig else 'not a significant difference'} at the 5% level."
    )
    out = {
        "test": "Student's t-test" if equal_var else "Welch's t-test",
        "column": val,
        "group": g,
        "groups": [{"group": chosen[0], "n": len(a), "mean": _json_value(a.mean()), "sd": _json_value(a.std(ddof=1))},
                   {"group": chosen[1], "n": len(b), "mean": _json_value(b.mean()), "sd": _json_value(b.std(ddof=1))}],
        "mean_difference": _json_value(diff),
        "t": _json_value(res.statistic),
        "df": _json_value(res.df),
        "p": _json_value(p),
        "ci95": [_json_value(ci.low), _json_value(ci.high)],
        "cohens_d": _json_value(d),
        "significant": sig,
        "interpretation": interpretation,
    }
    _record("ttest", dataset, f"{val}: {chosen[0]} vs {chosen[1]} ({_fmt_p(p)})", out)
    return out


@mcp.tool()
async def anova(
    dataset: Annotated[str, Field(description="The dataset id from load_csv or list_datasets.")],
    column: Annotated[str, Field(description="The number column to compare, e.g. height_cm.")],
    group: Annotated[str, Field(description="The column with the groups, e.g. treatment.")],
) -> dict[str, Any]:
    """Compare the means of two or more groups with a one-way ANOVA, then Tukey's HSD for every pair.

    Returns {test, column, group, groups: [{group, n, mean, sd}], F, p, eta_squared, significant (p < 0.05),
    pairs: [{a, b, mean_difference, p, ci95, significant}], interpretation}.
    """
    df, meta = _load(dataset)
    val = _column(df, column, "column")
    _numeric(df, val, "column")
    g = _column(df, group, "group")
    parts = [(n, p[val].dropna().astype(float)) for n, p in _groups(df, g)]
    parts = [(n, v) for n, v in parts if len(v) >= 2]
    if len(parts) < 2:
        raise ToolError(f"'{g}' needs at least two groups with two or more values each.")
    samples = [v.to_numpy() for _, v in parts]
    f_stat, p = stats.f_oneway(*samples)
    allv = np.concatenate(samples)
    ss_between = sum(len(s) * (s.mean() - allv.mean()) ** 2 for s in samples)
    ss_total = ((allv - allv.mean()) ** 2).sum()
    tukey = stats.tukey_hsd(*samples)
    ci = tukey.confidence_interval(0.95)
    pairs = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            pv = float(tukey.pvalue[i, j])
            pairs.append({
                "a": parts[i][0], "b": parts[j][0],
                "mean_difference": _json_value(samples[i].mean() - samples[j].mean()),
                "p": _json_value(pv),
                "ci95": [_json_value(ci.low[i, j]), _json_value(ci.high[i, j])],
                "significant": pv < 0.05,
            })
    sig_pairs = [f"{q['a']}–{q['b']}" for q in pairs if q["significant"]]
    p = float(p)
    interpretation = (
        f"The groups of {g} differ in {val} ({_fmt_p(p)})"
        + (f"; Tukey: {', '.join(sig_pairs)} differ." if sig_pairs else "; no single pair differs in Tukey's test.")
        if p < 0.05
        else f"No significant difference in {val} between the groups of {g} ({_fmt_p(p)})."
    )
    out = {
        "test": "one-way ANOVA + Tukey HSD",
        "column": val,
        "group": g,
        "groups": [{"group": n, "n": len(v), "mean": _json_value(v.mean()), "sd": _json_value(v.std(ddof=1))}
                   for n, v in parts],
        "F": _json_value(f_stat),
        "p": _json_value(p),
        "eta_squared": _json_value(ss_between / ss_total if ss_total else float("nan")),
        "significant": p < 0.05,
        "pairs": pairs,
        "interpretation": interpretation,
    }
    _record("anova", dataset, f"{val} by {g} ({_fmt_p(p)})", out)
    return out


@mcp.tool()
def history(
    limit: Annotated[int, Field(ge=1, le=200, description="How many results, newest first.")] = 30,
    dataset: Annotated[str | None, Field(description="Optional: only this dataset's results.")] = None,
) -> list[dict[str, Any]]:
    """The latest results (loads, summaries, charts, tests), newest first: [{id, at, tool, dataset, title, result,
    plot}]. A chart's image comes from get_plot with its `plot` id."""
    items = _history(2000 if dataset else limit)
    if dataset:
        items = [h for h in items if h.get("dataset") == dataset][:limit]
    return items


@mcp.tool(structured_output=False)
def get_plot(plot: Annotated[str, Field(description="A plot id (from plot or history), like g1a2b3c4d.")]) -> Image:
    """A chart drawn earlier, as a PNG image."""
    if not re.fullmatch(r"g[0-9a-f]{8}", plot or ""):
        raise ToolError(f"'{plot}' is not a plot id.")
    path = PLOTS / f"{plot}.png"
    if not path.exists():
        raise ToolError(f"No plot '{plot}'.")
    return Image(data=path.read_bytes(), format="png")


@mcp.tool()
def remove_dataset(dataset: Annotated[str, Field(description="The dataset id to forget.")]) -> dict[str, Any]:
    """Forget a loaded dataset (its table file goes; results in the history stay)."""
    meta_file = _meta_path(dataset)
    if not meta_file.exists():
        raise ToolError(f"No dataset '{dataset}'.")
    meta_file.unlink()
    (DATASETS / f"{dataset}.csv").unlink(missing_ok=True)
    _cache.pop(dataset, None)
    return {"removed": dataset}


sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab import ref_tools  # noqa: E402
from lab import tools as lab_tools  # noqa: E402
from lab.events import Emitter  # noqa: E402

HUB = _Hub()
EVENTS = Emitter(HUB.send, _hub_known)
LAB = lab_tools.register(mcp, DATA, lambda topic, data: _emit_soon(data, topic))
ref_tools.register(mcp, LAB)


def _warm() -> None:
    """Read the slow parts once in the background (Jedi's pandas, the first built-in method's flow): the first
    completion and the first flow otherwise take seconds."""
    from lab import codeintel, flow

    try:
        codeintel.warm()
        first = next((m for m in LAB.list_methods() if m.get("builtin")), None)
        if first:
            flow.graph(LAB.get_method(first["id"])["source"])
    except Exception as e:  # only a head start; the tools do the same work when asked
        log.info("warming up failed: %s", e)


if __name__ == "__main__":
    log.info("research backend starting; data in %s", DATA)
    import threading

    threading.Thread(target=_warm, daemon=True).start()

    async def _serve() -> None:
        EVENTS.use_loop(asyncio.get_running_loop())  # events sent from worker threads go through this loop
        await mcp.run_stdio_async()

    asyncio.run(_serve())

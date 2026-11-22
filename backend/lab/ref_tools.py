"""References to things in Research from other plugins (a Wiki page): an experiment, a result, one number, a step
with a run's values, a function of a method, a method.

A reference is an ordinary layr link, `layr://research/open/<ref>`, where `ref` is a query string (keys: experiment,
method, result, row, column, function, run, was) put through `encodeURIComponent` and with `( ) ! ' *` encoded too
(a raw `)` would end a Markdown link). Research's layr.json opens such a link in the Experiments panel with the
param `ref`. A reference always shows the experiment's latest finished run; `run` and `was` only say what it was
when the reference was made.

The pure core (`Ref`, `parse_ref`, `build_ref`, `encode_link`, `decode_link`, `fmt`) is used by both tools;
`register(mcp, store, api)` adds `reference_get` (what a reference shows now) and `reference_make` (a reference
and its readable Markdown link, for "Copy link"). It uses nothing from `api`.
"""

from __future__ import annotations

import base64
import json
import math
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from . import explain, latex, methodfile, sources
from .errors import LabError
from .runner import approved_corrections, experiment_inputs, resolve
from .store import ID as METHOD_ID
from .store import jsonable

PREFIX = "layr://research/open/"
KEYS = ("experiment", "method", "result", "row", "column", "function", "run", "was")
TABLE_ROWS = 8  # rows and columns of a table's preview
TABLE_COLUMNS = 8
TEXT_CHARS = 1500  # characters of a text's preview
VALUES = 12  # names with their values shown for a step
LINK_TEXT = 90  # characters of a Markdown link's text
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------------------------------- the reference


@dataclass
class Ref:
    """A reference's keys. `row` is the dict of the columns that pick one row of a table."""

    experiment: str | None = None
    method: str | None = None
    result: str | None = None
    row: dict[str, Any] | None = None
    column: str | None = None
    function: str | None = None
    run: str | None = None
    was: str | None = None

    @property
    def kind(self) -> str:
        """What the keys say it is (a result naming a single number is told apart only by looking at the run)."""
        if self.method:
            return "function" if self.function else "method"
        if self.function:
            return "step"
        if self.row is not None:
            return "number"
        if self.result:
            return "number" if self.was is not None else "result"
        return "experiment"

    def canonical(self) -> Ref:
        """The same thing without `run` and `was` (what the link of the thing itself is)."""
        return Ref(self.experiment, self.method, self.result, self.row, self.column, self.function)


def row_text(row: dict[str, Any]) -> str:
    """A row's identifying columns as compact JSON with sorted keys: the same row always gives the same text."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scalar(v: Any) -> bool:
    return isinstance(v, (str, bool, int, float)) and not (isinstance(v, float) and not math.isfinite(v))


def check_ref(ref: Ref) -> Ref:
    """The reference itself, or a plain error saying what is wrong with its keys."""
    if bool(ref.experiment) == bool(ref.method):
        raise LabError("A reference names an experiment or a method (one of them).")
    if ref.experiment and not re.fullmatch(r"e[0-9a-f]{8}", ref.experiment):
        raise LabError(f"'{ref.experiment}' is not an experiment id (they look like e1a2b3c4d).")
    if ref.method and not METHOD_ID.match(ref.method):
        raise LabError(f"'{ref.method}' is not a method id (lowercase letters, digits and dashes).")
    if ref.run is not None and not re.fullmatch(r"r[0-9a-f]{8}", ref.run):
        raise LabError(f"'{ref.run}' is not a run id (they look like r1a2b3c4d).")
    if ref.method and (ref.result or ref.row is not None or ref.column):
        raise LabError("A method has no results: a result, row or column needs an experiment.")
    if ref.function is not None and not ref.function.isidentifier():
        raise LabError(f"'{ref.function}' is not a function's name.")
    if ref.function and ref.result:
        raise LabError("A reference names a result or a function, not both.")
    if (ref.row is None) != (ref.column is None):
        raise LabError("A number in a table needs both the row and the column.")
    if ref.row is not None:
        if not ref.result:
            raise LabError("A row needs the result (table) it is in.")
        if not isinstance(ref.row, dict) or not ref.row:
            raise LabError("The row must be a JSON object of the columns that pick it, e.g. {\"group\": \"B\"}.")
        bad = [k for k, v in ref.row.items() if not _scalar(v)]
        if bad:
            raise LabError(f"The row's values must be texts, numbers or true/false (not {', '.join(bad)}).")
    for name in ("result", "column"):
        v = getattr(ref, name)
        if v is not None and not v.strip():
            raise LabError(f"The reference's {name} is empty.")
    return ref


def build_ref(experiment: str | None = None, method: str | None = None, result: str | None = None,
              row: dict[str, Any] | None = None, column: str | None = None, function: str | None = None,
              run: str | None = None, was: str | None = None) -> str:
    """The reference as a query string, keys in a fixed order (row as compact JSON with sorted keys)."""
    ref = check_ref(Ref(experiment, method, result, row, column, function, run, was))
    return ref_string(ref)


def ref_string(ref: Ref) -> str:
    pairs = []
    for k in KEYS:
        v = getattr(ref, k)
        if v is None:
            continue
        pairs.append((k, row_text(v) if k == "row" else str(v)))
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote_plus, safe="")


def parse_ref(text: str) -> Ref:
    """A reference from its query string (as `URLSearchParams` reads it; a whole `layr://research/open/...` link
    works too). A malformed one is a plain error."""
    text = (text or "").strip()
    if text.startswith("layr://"):
        text = decode_link(text)
    text = text.removeprefix("?")
    if not text:
        raise LabError("The reference is empty.")
    found: dict[str, str] = {}
    for k, v in urllib.parse.parse_qsl(text, keep_blank_values=True):
        if k not in KEYS:
            raise LabError(f"A reference has no key '{k}'. Keys: {', '.join(KEYS)}.")
        if k in found:
            raise LabError(f"The reference names '{k}' twice.")
        found[k] = v
    if not found:
        raise LabError("The reference has no keys.")
    row = None
    if "row" in found:
        try:
            row = json.loads(found["row"])
        except json.JSONDecodeError:
            raise LabError(f"The reference's row isn't JSON: {found['row'][:80]}") from None
    return check_ref(Ref(found.get("experiment") or None, found.get("method") or None, found.get("result"), row,
                         found.get("column"), found.get("function"), found.get("run") or None, found.get("was")))


def encode_link(ref: str | Ref) -> str:
    """`layr://research/open/` + the reference encoded like `encodeURIComponent`, and `( ) ! ' *` too."""
    text = ref_string(ref) if isinstance(ref, Ref) else ref
    return PREFIX + urllib.parse.quote(text, safe="")


def decode_link(url: str) -> str:
    """The reference (its query string) in a `layr://research/open/...` link."""
    url = (url or "").strip()
    if not url.startswith(PREFIX):
        raise LabError(f"Not a link to something in Research (they start with {PREFIX}).")
    return urllib.parse.unquote(url[len(PREFIX):].split("#", 1)[0])


# ---------------------------------------------------------------------------------------------------- words, numbers


def fmt(v: Any) -> str:
    """A value as the panel writes it (`fmt` in panels/lab.js): 4 significant digits, an exponent for very small or
    large numbers, whole numbers as they are."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        f = float(v)
        if not math.isfinite(f):
            return ""
        if f.is_integer() and abs(f) < 1e21:
            return str(int(f))
        a = abs(f)
        if a < 1e-3 or a >= 1e6:
            mantissa, exp = f"{f:.3e}".split("e")
            return f"{mantissa}e{'-' if int(exp) < 0 else '+'}{abs(int(exp))}"
        short = float(f"{f:.4g}")
        return str(int(short)) if short.is_integer() else repr(short)
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def same_text(was: str, now: str) -> bool:
    """Whether a value written when a reference was made still reads the same (equal texts, or the same number at
    the panel's precision)."""
    if was.strip() == now.strip():
        return True
    try:
        a, b = float(was), float(now)
    except ValueError:
        return False
    return math.isfinite(a) and math.isfinite(b) and fmt(a) == fmt(b)


def human(name: str) -> str:
    """A function's or result's name for people: underscores as spaces, the first letter capital."""
    s = str(name or "").replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def label_unit(name: str) -> tuple[str, str | None]:
    """A column's words (underscores as spaces) and the unit read from the end of its name."""
    words, unit = latex.split_unit(name)
    return (" ".join(words) or name.replace("_", " ")), unit


def when(iso: str | None) -> str:
    """A time for people, in this PC's time zone: 21 Nov 2026 14:29."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    return f"{d.day} {MONTHS[d.month - 1]} {d.year} {d:%H:%M}"


def chip(v: Any) -> str:
    """A recorded value as the short text of the panel's chips (`valueChip` in panels/lab.js)."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return fmt(v)
    if isinstance(v, str):
        return json.dumps(v if len(v) <= 40 else v[:40] + "…", ensure_ascii=False)
    if not isinstance(v, dict):
        return fmt(v)
    k = v.get("kind")
    if k == "table":
        return f"table {v.get('rows')} × {len(v.get('columns') or [])}"
    if k == "number":
        return v.get("text") or fmt(v.get("value"))
    if k == "column":
        return f"column of {v.get('length')}"
    if k == "array":
        return "array " + " × ".join(str(x) for x in v.get("shape") or [])
    if k in ("list", "dict"):
        n = v.get("length") or 0
        word = ("item", "items") if k == "list" else ("entry", "entries")
        return f"{n} {word[0] if n == 1 else word[1]}"
    if k == "record":
        return v.get("type") or "record"
    if k == "figure":
        return "figure"
    if k == "function":
        return f"function {v.get('name') or ''}".strip()
    return v.get("text") or k or "…"


def link_text(head: str, where: str | None = None) -> str:
    """A Markdown link's text: no brackets or line breaks, at most about LINK_TEXT characters (the part in
    parentheses is shortened first)."""
    def clean(s: str) -> str:
        return re.sub(r"\s+", " ", s.replace("[", "(").replace("]", ")")).strip()

    head = clean(head)
    where = clean(where) if where else ""
    text = f"{head} ({where})" if where else head
    if len(text) > LINK_TEXT and where:
        room = LINK_TEXT - len(head) - 4
        if room >= 12:
            return f"{head} ({where[:room - 1].rstrip()}…)"
        text = head
    return text if len(text) <= LINK_TEXT else text[:LINK_TEXT - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------------- what it shows


class Missing(Exception):
    """The thing a reference names is gone; the message says so in plain words."""


def latest_done_run(store, e: dict[str, Any]) -> dict[str, Any] | None:
    """The experiment's latest finished (done) run, not a trial run."""
    last = e.get("last_run") or {}
    if last.get("status") == "done":
        try:
            return store.get_run(last["id"])
        except LabError:
            pass
    for r in store.list_runs(e["id"]):
        if r.get("status") == "done":
            try:
                return store.get_run(r["id"])
            except LabError:
                continue
    return None


def is_stale(store, e: dict[str, Any], run: dict[str, Any]) -> bool:
    """Whether the experiment changed since the run: its method's code, its settings, its raw files, its approved
    corrections or what it reads of other experiments (lab/sources.py: a newer run with other numbers, other picks)
    are no longer what the run used."""
    try:
        source = store.get_method(e["method"], e.get("method_version"))["source"]
    except LabError:
        return True
    if source != explain.run_source(store, run):
        return True
    try:
        info = resolve(store, e, source)
    except LabError:
        return True
    settings = {k: v for k, v in info["args"].items() if not methodfile.is_input(k)}
    if jsonable(settings) != jsonable(run.get("settings") or {}):
        return True
    files = experiment_inputs(e, list(info["inputs"]))
    used = {k: (v or {}).get("sha256") for k, v in (run.get("inputs") or {}).items()}
    if {k: v.get("sha256") for k, v in files.items()} != used:
        return True
    if jsonable(approved_corrections(e)) != jsonable(run.get("corrections") or []):
        return True
    if not sources.picks(e) and not run.get("sources"):
        return False
    # out of date because of its sources, judged against this run (the one the reference shows)
    by_id = {x["id"]: x for x in store.list_experiments()}
    return bool(sources.outdated(store, {**e, "last_run": {**(e.get("last_run") or {}), "id": run["id"]}}, by_id))


def flag_counts(flags: dict[str, int]) -> str:
    """A run's quality flag counts in words: 1 error, 3 warnings, 2 notes."""
    words = {"error": ("error", "errors"), "warning": ("warning", "warnings"), "info": ("note", "notes")}
    order = sorted(flags, key=lambda level: (list(words).index(level) if level in words else 9, level))
    parts = []
    for level in order:
        n = flags[level]
        one, many = words.get(level, (level, level))
        parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts) or "none"


def _row_where(row: dict[str, Any]) -> str:
    return ", ".join(f"{k} {fmt(v) if not isinstance(v, str) else v}" for k, v in row.items())


class View:
    """What one reference shows now (see the reference_get tool)."""

    def __init__(self, store, ref: Ref, making: bool = False):
        self.store = store
        self.ref = ref
        self.making = making  # for reference_make: words for a thing being picked now, not one that changed
        self.exp_title = ""
        self.kind = ref.kind
        self.title = ""
        self.subtitle = ""
        self.markdown = ""  # the readable text of a Markdown link to it
        self.run: dict[str, Any] | None = None
        self.stale = False
        self.body: dict[str, Any] = {}
        self.text: str | None = None  # a number's text now (for `was`)

    # -------------------------------------------------- the parts every kind uses

    def experiment(self) -> dict[str, Any]:
        try:
            e = self.store.get_experiment(self.ref.experiment)
        except LabError:
            self.title = self.title or f"Experiment {self.ref.experiment}"
            raise Missing("This experiment no longer exists.") from None
        self.exp_title = e.get("title") or e["id"]
        return e

    def finished_run(self, e: dict[str, Any]) -> dict[str, Any]:
        run = latest_done_run(self.store, e)
        if run is None:
            last = e.get("last_run") or {}
            if last.get("status") in ("failed", "stopped"):
                raise Missing(f"This experiment has no finished calculation: its last run {last['status']}.")
            raise Missing("This experiment hasn't been calculated yet.")
        self.run = run
        self.stale = is_stale(self.store, e, run)
        return run

    def method_name(self, method_id: str, version: int | None = None) -> str:
        try:
            return self.store.get_method(method_id, version)["name"]
        except LabError:
            return f"{method_id} (missing)"

    # -------------------------------------------------- each kind

    def show(self) -> None:
        getattr(self, f"show_{'result' if self.kind == 'number' else self.kind}")()

    def show_experiment(self) -> None:
        e = self.experiment()
        self.title = self.exp_title
        mname = self.method_name(e["method"], e.get("method_version"))
        self.subtitle = mname
        self.markdown = link_text(self.exp_title)
        run = latest_done_run(self.store, e)
        if run is not None:
            self.run = run
            self.stale = is_stale(self.store, e, run)
        last = e.get("last_run")
        items = [{"label": "Method", "value": mname}]
        files = [v.get("name") for v in (e.get("inputs") or {}).values() if isinstance(v, dict) and v.get("name")]
        items.append({"label": "Files", "value": ", ".join(files) if files else "none"})
        if last:
            state = {"done": "calculated", "failed": "failed", "stopped": "stopped"}.get(last.get("status"),
                                                                                         last.get("status") or "")
            items.append({"label": "Last run", "value": f"{when(last.get('finished') or last.get('started'))} · "
                                                        f"{state}"})
        else:
            items.append({"label": "Last run", "value": "not calculated yet"})
        if run is not None:
            flags = (run.get("summary") or {}).get("flags")
            if flags is not None:
                items.append({"label": "Quality flags", "value": flag_counts(flags)})
        waiting = sum(1 for c in e.get("corrections") or [] if c.get("status") == "proposed")
        items.append({"label": "Proposed corrections waiting", "value": str(waiting)})
        self.body = {"type": "facts", "items": items}

    def show_result(self) -> None:
        ref = self.ref
        name = ref.result
        is_step = name.startswith("step:")
        self.title = human(name[5:]) + " table" if is_step else human(name)
        if ref.column:
            self.title = label_unit(ref.column)[0]
        e = self.experiment()
        self.subtitle = self.exp_title
        run = self.finished_run(e)
        out = self.store.output_file(run, name)
        if out is None:
            raise Missing(f"The step {name[5:]} made no table in the latest run." if is_step else
                          f"The experiment's latest run no longer returns “{name}”.")
        if not is_step and out.get("label") and not ref.column:
            self.title = out["label"]
        kind = out.get("kind")
        if ref.row is not None:
            if kind != "table":
                raise Missing(f"“{name}” is no longer a table.")
            self.number_in_table(run)
        elif kind == "table":
            self.kind = "result"
            self.table(run)
        elif kind == "figure":
            self.kind = "result"
            self.figure(run, out)
        elif kind == "text":
            self.kind = "result"
            text = (self.store.run_dir(run["id"]) / out["file"]).read_text("utf-8")
            self.body = {"type": "text", "text": text[:TEXT_CHARS]}
            if len(text) > TEXT_CHARS:
                self.body["more"] = True
        elif kind == "value":
            self.value(out)
        else:
            raise Missing(f"“{name}” can't be shown.")
        if not self.markdown:
            self.markdown = link_text(self.title, self.exp_title)

    def table(self, run: dict[str, Any]) -> None:
        df = self.store.run_table(run["id"], self.ref.result)
        cols = [str(c) for c in df.columns[:TABLE_COLUMNS]]
        rows = [[jsonable(v) for v in r] for r in df.iloc[:TABLE_ROWS, :TABLE_COLUMNS].itertuples(index=False)]
        self.body = {"type": "table", "columns": cols, "rows": rows, "total": len(df),
                     "total_columns": len(df.columns)}

    def figure(self, run: dict[str, Any], out: dict[str, Any]) -> None:
        path = self.store.run_dir(run["id"]) / out["file"]
        if not path.exists():
            raise Missing(f"The figure “{self.ref.result}” of the latest run is gone.")
        self.body = {"type": "image", "mime": "image/png", "data": base64.b64encode(path.read_bytes()).decode(),
                     "alt": f"{self.title} ({self.exp_title})"}

    def value(self, out: dict[str, Any]) -> None:
        v = out.get("value")
        if isinstance(v, dict) and v.get("kind") == "number":
            v = v.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            label, unit = label_unit(self.ref.result)
            label = out.get("label") or label
            self.kind = "number"
            self.number(v, label, unit)
            self.markdown = link_text(f"{label} {self.text}{' ' + unit if unit else ''}", self.exp_title)
            return
        self.kind = "result"
        shown = explain.show(v)
        text = shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False, indent=1)
        self.body = {"type": "text", "text": text[:TEXT_CHARS]}
        if len(text) > TEXT_CHARS:
            self.body["more"] = True

    def number_in_table(self, run: dict[str, Any]) -> None:
        ref = self.ref
        self.kind = "number"
        where = _row_where(ref.row)
        self.subtitle = f"{where} · {self.exp_title}"
        df = self.store.run_table(run["id"], ref.result)
        gone = [c for c in [*ref.row, ref.column] if c not in df.columns]
        if gone:
            raise Missing(f"The table “{ref.result}” no longer has the column {', '.join(dict.fromkeys(gone))}.")
        picked = df
        for c, v in ref.row.items():
            picked = picked[explain.equals(picked[c], v)]
        if len(picked) == 0:
            raise Missing(f"No row of “{ref.result}” has {where}." if self.making else
                          f"No row of “{ref.result}” has {where} any more.")
        if len(picked) > 1:
            if self.making:
                raise Missing(f"{len(picked)} rows of “{ref.result}” have {where}: give more columns in the row to "
                              "pick exactly one.")
            raise Missing(f"{len(picked)} rows of “{ref.result}” now have {where}, so this number can no longer be "
                          "picked.")
        v = jsonable(picked.iloc[0][ref.column])
        label, unit = label_unit(ref.column)
        self.number(v, label, unit)
        self.markdown = link_text(f"{label} {self.text}{' ' + unit if unit else ''} · {where}", self.exp_title)

    def number(self, v: Any, label: str, unit: str | None) -> None:
        number = isinstance(v, (int, float)) and not isinstance(v, bool)
        self.text = fmt(v) or "(blank)"
        self.title = label
        self.body = {"type": "number", "text": self.text, "value": v if number else None, "unit": unit,
                     "label": label}

    def show_step(self) -> None:
        from .inside_tools import inside_view

        ref = self.ref
        self.title = human(ref.function)
        e = self.experiment()
        self.subtitle = self.exp_title
        run = self.finished_run(e)
        source = explain.run_source(self.store, run) or ""
        fn = methodfile.parse(source).function(ref.function) if source else None
        if fn is None:
            raise Missing(f"The method no longer has a function {ref.function} (it was renamed or removed).")
        self.subtitle = f"{fn.kind} · {self.exp_title}"
        self.markdown = link_text(self.title, self.exp_title)
        self.body = code_body(fn)
        self.body["values"] = step_values(inside_view(self.store, run, ref.function))

    def show_function(self) -> None:
        ref = self.ref
        self.title = human(ref.function)
        m = self.method()
        fn = methodfile.parse(m["source"]).function(ref.function)
        if fn is None:
            raise Missing(f"The method no longer has a function {ref.function} (it was renamed or removed).")
        self.subtitle = f"{fn.kind} · {m['name']}"
        self.markdown = link_text(self.title, m["name"])
        self.body = code_body(fn)

    def method(self) -> dict[str, Any]:
        try:
            m = self.store.get_method(self.ref.method)
        except LabError:
            self.title = self.title or self.ref.method
            raise Missing("This method no longer exists.") from None
        return m

    def show_method(self) -> None:
        m = self.method()
        self.title = m["name"]
        self.subtitle = f"Method · version {m['version']}"
        self.markdown = link_text(f"{m['name']} (method)")
        try:
            s = methodfile.summary(methodfile.parse(m["source"]))
        except SyntaxError:
            s = {"steps": 0, "inputs": [], "settings": []}
        used = sum(1 for e in self.store.list_experiments() if e.get("method") == m["id"])
        items = [{"label": "Name", "value": m["name"]}]
        if m.get("description"):
            items.append({"label": "Description", "value": m["description"]})
        items += [{"label": "Version", "value": str(m["version"])},
                  {"label": "Steps", "value": str(s["steps"])},
                  {"label": "Inputs", "value": ", ".join(i["label"] for i in s["inputs"]) or "none"},
                  {"label": "Settings", "value": str(len(s["settings"]))},
                  {"label": "Used by", "value": f"{used} experiment{'' if used == 1 else 's'}"}]
        self.body = {"type": "facts", "items": items}


def code_body(fn: methodfile.Function) -> dict[str, Any]:
    return {"type": "code", "language": "python", "code": fn.source, "sentence": fn.doc.sentence,
            "formulas": [f["latex"] for f in explain.function_formulas(fn)]}


def step_values(inside: dict[str, Any]) -> list[dict[str, str]]:
    """What a function's first call gave its names in a run (in a loop: the last round kept) and what it returned,
    as the panel's chips write them; the call's arguments when it gave no names (a formula). At most VALUES."""
    found: dict[str, str] = {}
    for st in inside.get("statements") or []:
        rounds = st.get("rounds") or []
        if not rounds:
            continue
        last = rounds[-1].get("values") or {}
        n = st.get("n") or len(rounds)
        for name in st.get("names") or []:
            if name in last:
                found.pop(name, None)  # keep the order of the last line that gave it
                found[name] = chip(last[name]) + (f" (last of {n})" if n > 1 else "")
    if not found:
        for name, v in ((inside.get("call") or {}).get("args") or {}).items():
            found[name] = chip(v)
    items = [{"name": k, "text": v} for k, v in found.items()]
    if "returned" in inside:
        items = items[:VALUES - 1] + [{"name": "returned", "text": chip(inside["returned"])}]
    return items[:VALUES]


def reference_view(store, ref: Ref, making: bool = False) -> dict[str, Any]:
    """What a reference shows now: the reference_get tool's answer (never raises for a well-formed reference)."""
    v = View(store, ref, making)
    link = encode_link(ref.canonical())
    try:
        v.show()
    except (Missing, LabError) as gone:
        return {"ok": False, "kind": v.kind, "title": v.title or ref.function or ref.result or ref.experiment
                or ref.method, "subtitle": v.subtitle or None, "link": link, "missing": str(gone)}
    changed = None
    if v.kind == "number" and ref.was is not None and v.text is not None and not same_text(ref.was, v.text):
        changed = {"was": ref.was, "now": v.text}
    run = {"id": v.run["id"], "finished": v.run.get("finished")} if v.run else None
    return {"ok": True, "kind": v.kind, "title": v.title, "subtitle": v.subtitle, "link": link, "run": run,
            "stale": bool(v.stale) if run else False, "changed": changed, "body": v.body, "_markdown": v.markdown,
            "_text": v.text}


def make_reference(store, experiment: str | None = None, method: str | None = None, result: str | None = None,
                   row: dict[str, Any] | None = None, column: str | None = None, function: str | None = None
                   ) -> dict[str, Any]:
    """A reference to something that exists now, with `run` and `was` for a number, and its Markdown link."""
    ref = check_ref(Ref(experiment or None, method or None, result, row, column, function))
    got = reference_view(store, ref, making=True)
    if not got["ok"]:
        raise LabError(got["missing"])
    if got["kind"] == "number":
        ref.run = got["run"]["id"]
        ref.was = got["_text"]
    text = got["_markdown"]
    link = encode_link(ref)
    return {"ref": ref_string(ref), "link": link, "text": text, "markdown": f"[{text}]({link})"}


# ---------------------------------------------------------------------------------------------------- the tools


def register(mcp, store, api=None) -> None:
    @mcp.tool()
    def reference_get(
        ref: Annotated[str, Field(description="The reference: the query string inside a layr://research/open/… "
                                              "link, decoded (e.g. experiment=e1a2b3c4d&result=scores).")],
    ) -> dict[str, Any]:
        """What a reference to something in Research shows now (a Wiki page draws it as a card). The reference is a
        query string with the keys experiment (id), method (id, when no experiment is meant), result (a returned
        result's name, or step:<function> for a step's table), row (JSON object of the columns that pick one row),
        column, function, run and was (the run and the value when it was made: information only). Kinds by the
        keys: experiment; result (experiment+result); number (experiment+result+row+column, or a result that is a
        single number); step (experiment+function: its code with the latest run's values); function
        (method+function: its code); method. Always the experiment's latest finished run.
        Gives {ok: true, kind, title, subtitle, link (without run and was), run: {id, finished} | null, stale (the
        experiment changed since that run), changed: {was, now} | null, body} where body is one of {type: number,
        text, value, unit, label}, {type: table, columns, rows (first 8 × 8), total, total_columns}, {type: image,
        mime, data (base64), alt}, {type: text, text, more?}, {type: code, language, code, sentence, formulas
        (LaTeX), values?: [{name, text}]}, {type: facts, items: [{label, value}]}. When the thing is gone:
        {ok: false, kind, title, link, missing (a sentence)}. A malformed reference is an error."""
        out = reference_view(store, parse_ref(ref))
        return {k: v for k, v in out.items() if not k.startswith("_")}

    @mcp.tool()
    def reference_make(
        experiment: Annotated[str | None, Field(description="Experiment id.")] = None,
        method: Annotated[str | None, Field(description="Or a method id (a method, or with function its code).")]
        = None,
        result: Annotated[str | None, Field(description="A returned result's name, or step:<function> for a "
                                                        "step's table.")] = None,
        row: Annotated[dict[str, Any] | None, Field(description="Columns that pick one row of that table, e.g. "
                                                                "{\"group\": \"B\"}.")] = None,
        column: Annotated[str | None, Field(description="The number's column in that row.")] = None,
        function: Annotated[str | None, Field(description="A function's name: with experiment its step with the "
                                                          "run's values, with method its code.")] = None,
    ) -> dict[str, Any]:
        """A reference to something in Research that exists now, for "Copy link": {ref (the query string; keys
        experiment, method, result, row, column, function, run, was, read with URLSearchParams), link
        (layr://research/open/<ref encoded>), text (readable, e.g. "mean score 3.42 · group B (Attitude
        survey)"), markdown ([text](link))}. A number also records the latest run's id and its value as `was`.
        An error says plainly what doesn't exist (or that a row isn't picked by the columns given)."""
        return make_reference(store, experiment, method, result, row, column, function)

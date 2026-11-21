"""The values a run saw, each stored once by its content, and read back for display.

While a run records what happens inside the method's functions (lab/recorder.py), every table it meets is kept
once in the run's folder, however many lines and wires show it:

    runs/<id>/values/<token>.pack            many tables, one after the other, each a gzip-compressed CSV (a pack
                                             can be cut into its tables with the offsets in index.json; each part
                                             is readable by any program, years later)
    runs/<id>/values/index.json              for each table: its pack, offset and length, rows, columns, column
                                             types, whether it is complete
    runs/<id>/values/<fingerprint>.png       a figure's picture (a file of its own)

A table's name (`values/<fingerprint>.csv.gz`) stays the same whichever pack holds it. Packs, not one file per table:
creating a file costs about 5 ms on Windows, and a remembered step takes over its packs from the step cache with one
hard link each. A run made before packs (one `values/<fingerprint>.csv.gz` file per table) still reads.

A filtered view inside a step (`table[table.role == "sample"]`) has other rows, so it is a table of its own. A table
changed in place (`table["x"] = ...`) has another content after the change, so it is stored again.

A value everywhere else in the record is a small summary (`summary`): numbers as they are, text, lists and dicts
with their first items, a column with its first values; a table or a long column is {"kind": "table", "rows",
"columns", "file"}. `file` is opaque to everyone but this module and the tool `value_get` (`read`), which also read
the files a run wrote before 0.4: `steps/<n>_<step>.csv` and `outputs/<name>.csv|png`.

A run has a size budget (`BUDGET`): once its tables take more, further tables are kept as their first rows only
("stored": "sampled"), and past twice the budget they are left out ("stored": "left_out"). The summary says so, and
so does the record's "values" part.

`fingerprint` is the content's fingerprint: the same for the same content, different otherwise. It names the files
here and makes the keys of remembered steps (lab/recorder.py); a value it can't fingerprint safely gives None.
"""

from __future__ import annotations

import base64
import dataclasses
import gzip
import hashlib
import io
import json
import math
import os
import queue
import shutil
import sys
import threading
import types
import uuid
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np
import pandas as pd

from .errors import LabError

BUDGET = 200_000_000  # bytes of stored tables per run, before they are sampled
SAMPLE_ROWS = 200  # rows kept of a table past the budget
MAX_CELLS = 5_000_000  # a single table larger than this is kept as its first rows
HEAD = 10  # first values of a column, or items of a dict, in a summary
LIST_HEAD = 3  # first items of a list in a summary (and its last one: `rows.append(...)` shows the new row)
MAX_FINGERPRINT_BYTES = 2_000_000_000  # larger values aren't fingerprinted (their step isn't remembered)
COLUMN_FILE = 20  # a column or array longer than this is stored as a file as well (when it is a name's own value)
COLUMN_FILE_MAX = 200_000  # ...and not longer than this (a raw trace is in the raw file already)
PACK_FLUSH = 16_000_000  # bytes of tables held in memory before they are written as a pack


# ---------------------------------------------------------------------------------------------------- fingerprints


class _TooHard(Exception):
    """The value can't be fingerprinted safely."""


def fingerprint(v: Any, limit: int = MAX_FINGERPRINT_BYTES) -> str | None:
    """A fingerprint of a value's content (40 hex digits), or None when it can't be made safely (a figure, an
    open file, an object of unknown kind, or more than `limit` bytes)."""
    h = hashlib.sha256()  # the fastest here (processors compute it in hardware)
    state = {"bytes": 0, "limit": limit, "seen": set(), "alive": []}
    try:
        _feed(h, v, state, 0)
    except (_TooHard, RecursionError, TypeError, ValueError):
        return None
    return h.hexdigest()[:40]


def _put(h, state: dict, data: bytes | memoryview) -> None:
    state["bytes"] += len(data)
    if state["bytes"] > state["limit"]:
        raise _TooHard
    h.update(data)


def _tag(h, state: dict, text: str) -> None:
    _put(h, state, text.encode("utf-8", "surrogatepass") + b"\x00")


def _column(h, state: dict, s: pd.Series) -> None:
    """One column's values (not its index)."""
    _tag(h, state, str(s.dtype))
    if isinstance(s.dtype, np.dtype):
        arr = s.to_numpy()
        if arr.dtype.kind in "biufcmM":  # numbers, booleans, times: their bytes as they are in memory
            _put(h, state, memoryview(np.ascontiguousarray(arr)).cast("B"))
            return
        if arr.dtype == object:
            kind = pd.api.types.infer_dtype(arr, skipna=False)
            if kind == "string":  # the texts one after the other, and each one's length (so the split is known)
                _put(h, state, np.fromiter(map(len, arr), dtype=np.int64, count=len(arr)).tobytes())
                _put(h, state, "".join(arr).encode("utf-8", "surrogatepass"))
                return
            if kind not in ("empty", "bytes"):  # mixed objects: one by one, so 1 and "1" differ
                if len(arr) > 1_000_000:
                    raise _TooHard
                for x in arr.tolist():
                    _feed(h, x, state, 1)
                return
    elif isinstance(s.dtype, pd.StringDtype):  # texts (pandas' own kind): which are missing, then as above
        missing = s.isna().to_numpy(dtype=bool)
        arr = s.to_numpy(dtype=object, na_value="")
        _put(h, state, missing.tobytes())
        _put(h, state, np.fromiter(map(len, arr), dtype=np.int64, count=len(arr)).tobytes())
        _put(h, state, "".join(arr).encode("utf-8", "surrogatepass"))
        return
    _put(h, state, pd.util.hash_pandas_object(s, index=False).to_numpy().tobytes())


def _index(h, state: dict, ix: pd.Index) -> None:
    if isinstance(ix, pd.RangeIndex):
        _tag(h, state, f"range {ix.start} {ix.stop} {ix.step} {ix.name!r}")
        return
    _tag(h, state, f"index {ix.dtype} {list(ix.names)!r}")
    if isinstance(ix, pd.MultiIndex):
        for level in range(ix.nlevels):
            _column(h, state, pd.Series(ix.get_level_values(level)))
    else:
        _column(h, state, pd.Series(ix))


def _feed(h, v: Any, state: dict, depth: int) -> None:
    if depth > 30:
        raise _TooHard
    if v is None or isinstance(v, (bool, int, float, complex, str)):
        _tag(h, state, f"{type(v).__name__}:{v!r}")
        return
    if isinstance(v, (bytes, bytearray)):
        _tag(h, state, f"bytes {len(v)}")
        _put(h, state, bytes(v))
        return
    if isinstance(v, np.generic):
        _tag(h, state, f"np {v.dtype.str} {v!r}")
        return
    if isinstance(v, (pd.Timestamp, pd.Timedelta)) or v is pd.NaT or v is pd.NA:
        _tag(h, state, f"{type(v).__name__}:{v!r}")
        return
    if isinstance(v, (types.FunctionType, types.BuiltinFunctionType, type)) or hasattr(v, "__layr_original__"):
        _function(h, state, v)
        return
    key = id(v)
    if key in state["seen"]:  # the same object again inside itself or elsewhere in the value
        _tag(h, state, f"again {type(v).__name__}")
        return
    state["seen"].add(key)
    state["alive"].append(v)  # an id stays this object's only while it lives
    if isinstance(v, np.ndarray):
        _tag(h, state, f"array {v.dtype.str} {v.shape}")
        if v.dtype == object:
            if v.size > 1_000_000:
                raise _TooHard
            for x in v.ravel().tolist():
                _feed(h, x, state, depth + 1)
        else:
            _put(h, state, memoryview(np.ascontiguousarray(v)).cast("B"))
        return
    if isinstance(v, pd.DataFrame):
        _tag(h, state, f"table {v.shape}")
        _feed(h, [str(type(c).__name__) + ":" + str(c) for c in v.columns], state, depth + 1)
        _index(h, state, v.index)
        _tag(h, state, " ".join(str(t) for t in v.dtypes))
        for _, column in v.items():
            _column(h, state, column)
        _tag(h, state, repr(sorted(map(str, v.attrs))) if v.attrs else "")
        return
    if isinstance(v, pd.Series):
        _tag(h, state, f"column {v.name!r} {len(v)}")
        _index(h, state, v.index)
        _column(h, state, v)
        return
    if isinstance(v, pd.Index):
        _index(h, state, v)
        return
    if isinstance(v, tuple) and hasattr(v, "_fields"):
        _tag(h, state, f"record {type(v).__module__}.{type(v).__qualname__} {len(v)}")
        for x in v:
            _feed(h, x, state, depth + 1)
        return
    if isinstance(v, (list, tuple)):
        _tag(h, state, f"{type(v).__name__} {len(v)}")
        for x in v:
            _feed(h, x, state, depth + 1)
        return
    if isinstance(v, (set, frozenset)):
        parts = sorted(fingerprint(x, state["limit"]) or "?" for x in v)
        if "?" in parts:
            raise _TooHard
        _tag(h, state, f"{type(v).__name__} {len(v)} " + " ".join(parts))
        return
    if isinstance(v, dict):
        _tag(h, state, f"dict {type(v).__qualname__} {len(v)}")
        for k, x in v.items():
            _feed(h, k, state, depth + 1)
            _feed(h, x, state, depth + 1)
        return
    if dataclasses.is_dataclass(v):
        _tag(h, state, f"dataclass {type(v).__module__}.{type(v).__qualname__}")
        for f in dataclasses.fields(v):
            _tag(h, state, f.name)
            _feed(h, getattr(v, f.name), state, depth + 1)
        return
    if isinstance(v, (Path, range, slice)) or type(v).__module__ in ("datetime", "decimal", "fractions"):
        _tag(h, state, f"{type(v).__name__}:{v!r}")
        return
    import enum

    if isinstance(v, enum.Enum):
        _tag(h, state, f"enum {type(v).__qualname__}.{v.name}")
        return
    raise _TooHard


def _function(h, state: dict, fn: Any) -> None:
    """A function by what it does: its code for a function of the method, its name for a library's."""
    fn = getattr(fn, "__layr_original__", fn)
    code = getattr(fn, "__code__", None)
    name = f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', repr(fn))}"
    if code is None or (getattr(fn, "__module__", "") or "").startswith(("toolbox", "numpy", "pandas", "scipy",
                                                                        "math", "builtins")):
        _tag(h, state, f"fn {name}")
        return
    _tag(h, state, f"fn {name}")  # not its file: every run has its own copy of the method, with the same code
    _code(h, state, code)
    if fn.__defaults__:
        _feed(h, fn.__defaults__, state, 1)
    if getattr(fn, "__closure__", None):
        raise _TooHard  # a function that keeps values from where it was made: what it does isn't in its code


def _code(h, state: dict, code: types.CodeType) -> None:
    _put(h, state, code.co_code)
    _tag(h, state, repr(code.co_names) + repr(code.co_varnames))
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _code(h, state, c)
        else:
            _tag(h, state, repr(c))


# ---------------------------------------------------------------------------------------------------- sizes


def approx_size(v: Any, depth: int = 0) -> int:
    """About how many bytes a value takes in memory (arrays and tables counted fully, the rest roughly)."""
    if depth > 6:
        return 0
    if isinstance(v, np.ndarray):
        return int(v.nbytes)
    if isinstance(v, (pd.DataFrame, pd.Series)):
        try:
            return int(v.memory_usage(deep=False, index=True).sum()) if isinstance(v, pd.DataFrame) else \
                int(v.memory_usage(deep=False, index=True))
        except Exception:
            return 0
    if isinstance(v, (str, bytes, bytearray)):
        return len(v)
    if isinstance(v, dict):
        return sum(approx_size(k, depth + 1) + approx_size(x, depth + 1) for k, x in v.items()) + 64
    if isinstance(v, (list, tuple, set, frozenset)):
        return sum(approx_size(x, depth + 1) for x in v) + 8 * len(v)
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return sum(approx_size(getattr(v, f.name), depth + 1) for f in dataclasses.fields(v))
    return 32


# ---------------------------------------------------------------------------------------------------- storing


def _plain(x: Any) -> Any:
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    if x is None or isinstance(x, str):
        return x if not isinstance(x, str) or len(x) <= 200 else x[:200] + "…"
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    text = str(x)
    return text if len(text) <= 200 else text[:200] + "…"


class Later:
    """File work done by a thread of its own while the run goes on (creating a file costs about 5 ms on Windows):
    tasks run one after the other in the order given; `wait` before anyone reads what they write. Without a Later
    (`None` where one is taken) the work is done at once."""

    def __init__(self) -> None:
        self.queue: queue.SimpleQueue | None = None
        self.thread: threading.Thread | None = None
        self.failed = 0

    def __call__(self, fn: Callable, *args: Any) -> None:
        if self.thread is None:
            self.queue = queue.SimpleQueue()
            self.thread = threading.Thread(target=self._work, name="layr research files", daemon=True)
            self.thread.start()
        self.queue.put((fn, args))

    def _work(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception as e:  # a file that couldn't be written: the record or the step cache lacks it
                self.failed += 1
                print(f"couldn't write a file: {type(e).__name__}: {e}", file=sys.stderr)

    def wait(self) -> None:
        if self.thread is not None:
            self.queue.put(None)
            self.thread.join()
            self.thread = None


def do(later: Later | None, fn: Callable, *args: Any) -> None:
    """`fn(*args)` by `later`, or at once."""
    if later is None:
        fn(*args)
    else:
        later(fn, *args)


def link_or_copy(src: Path, dest: Path) -> None:
    """Make `dest` the same file as `src` (a hard link, or a copy where the disk can't link); its folder is made."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        os.link(src, dest)
    except FileNotFoundError:
        raise
    except OSError:  # a copy, whole or not at all (a run stopped half way must not leave half a file here)
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)


class ValueStore:
    """The tables of one run, each kept once in packs in `<run folder>/values/` (see the module's docstring)."""

    def __init__(self, run_dir: Path, budget: int = BUDGET, later: Later | None = None):
        self.run_dir = Path(run_dir)
        self.later = later
        self.dir = self.run_dir / "values"
        self.budget = budget
        self.index: dict[str, dict[str, Any]] = {}  # name -> facts
        self.bytes = 0
        self.sampled = 0
        self.left_out = 0
        self.pending: list[tuple[str, bytes]] = []  # (name, gzip bytes) not written yet
        self.pending_bytes = 0
        self.packs: set[str] = set()  # packs in the run's folder
        # Set by the recorder while it may: a fingerprint known already for this very object, and where to tell a
        # fingerprint just made (lab/recorder.py, StepCache.known / learn).
        self.lookup: Callable[[Any], str | None] | None = None
        self.learn: Callable[[Any, str], None] | None = None
        self.touched: set[str] | None = None  # the names of the tables summarized since the recorder last asked
        self.figs: dict[int, tuple[Any, dict[str, Any]]] = {}  # id → (a figure, its summary): drawn since the last
        #                                                        step started (worker.py clears it then)
    # --------------------------------------------------------------- summaries

    def summary(self, v: Any, depth: int = 0, files: bool = True) -> Any:
        """A small JSON description of a value for the record; tables (and long columns) get a file, unless they
        are fields of a record (an injection's peak table: that is raw data, not a step's work)."""
        if v is None or isinstance(v, (bool, np.bool_)):
            return None if v is None else bool(v)
        if isinstance(v, str):
            return v if len(v) <= 300 else v[:300] + "…"
        if isinstance(v, (int, np.integer)):
            return int(v)
        if isinstance(v, (float, np.floating)):
            f = float(v)
            if math.isnan(f):
                return {"kind": "number", "value": None, "text": "NaN"}
            if math.isinf(f):
                return {"kind": "number", "value": None, "text": "inf" if f > 0 else "-inf"}
            return f
        if isinstance(v, pd.DataFrame):
            if not files:
                return {"kind": "table", "rows": int(len(v)), "columns": [str(c) for c in v.columns]}
            return self.table(v)
        if isinstance(v, pd.Series):
            out = {"kind": "column", "name": None if v.name is None else str(v.name), "length": int(len(v)),
                   "dtype": str(v.dtype), "head": [_plain(x) for x in v.iloc[:HEAD].tolist()]}
            if depth == 0 and COLUMN_FILE < len(v) <= COLUMN_FILE_MAX:
                out["file"] = self.table(v.to_frame(name="value" if v.name is None else v.name), True).get("file")
            elif not isinstance(v.index, pd.RangeIndex):
                out["index"] = [_plain(x) for x in v.index[:HEAD].tolist()]
            return out
        if isinstance(v, np.ndarray):
            out = {"kind": "array", "shape": list(v.shape), "dtype": str(v.dtype),
                   "head": [_plain(x) for x in v.ravel()[:HEAD].tolist()]}
            if depth == 0 and COLUMN_FILE < v.size <= COLUMN_FILE_MAX and v.ndim in (1, 2):
                try:
                    out["file"] = self.table(pd.DataFrame(v if v.ndim == 2 else {"value": v}), True).get("file")
                except (ValueError, TypeError):
                    pass
            return out
        if isinstance(v, tuple) and hasattr(v, "_fields"):
            return {"kind": "record", "type": type(v).__name__,
                    "fields": {f: self.summary(getattr(v, f), depth + 1, False) for f in v._fields}}
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            names = [f.name for f in dataclasses.fields(v)]
            if depth > 2:
                return {"kind": "record", "type": type(v).__name__, "text": _short(v)}
            return {"kind": "record", "type": type(v).__name__,
                    "fields": {n: self.summary(getattr(v, n), depth + 1, False) for n in names[:HEAD]}}
        if isinstance(v, dict):
            out = {"kind": "dict", "length": len(v)}
            if depth <= 2:
                items = list(v.items())
                out["items"] = {str(k): self.summary(x, depth + 1, files) for k, x in items[:HEAD]}
                if len(items) > HEAD:
                    k, x = items[-1]
                    out["last"] = {"key": str(k), "value": self.summary(x, depth + 1, files)}
            return out
        if isinstance(v, (list, tuple, set, frozenset)):
            seq = list(v) if not isinstance(v, (set, frozenset)) else sorted(v, key=repr)
            out = {"kind": "list", "length": len(seq)}
            if isinstance(v, tuple):
                out["type"] = "tuple"
            if depth <= 2:
                out["items"] = [self.summary(x, depth + 1, files) for x in seq[:LIST_HEAD]]
                if len(seq) > LIST_HEAD:
                    out["last"] = self.summary(seq[-1], depth + 1, files)
            return out
        if isinstance(v, (bytes, bytearray)):
            return {"kind": "bytes", "size": len(v)}
        if type(v).__name__ == "Figure":
            return self.figure(v) if files and hasattr(v, "savefig") else {"kind": "figure"}
        if isinstance(v, pd.Timestamp):
            return v.isoformat()
        if isinstance(v, (types.FunctionType, types.BuiltinFunctionType)) or hasattr(v, "__layr_original__"):
            return {"kind": "function", "name": getattr(v, "__name__", None) or _short(v)}
        return {"kind": "object", "type": type(v).__name__, "text": _short(v)}

    def table(self, df: pd.DataFrame, made_here: bool = False) -> dict[str, Any]:
        """A table's summary, with the name it is kept under (stored when it is new). `made_here`: a table made just
        for storing (a column as a table): no fingerprint of it is looked up or told."""
        out: dict[str, Any] = {"kind": "table", "rows": int(len(df)), "columns": [str(c) for c in df.columns]}
        fp = self.lookup(df) if self.lookup is not None and not made_here else None
        if fp is None:
            fp = fingerprint(df)
            if fp is not None and self.learn is not None and not made_here:
                self.learn(df, fp)
        if fp is None:
            out["stored"] = "left_out"
            out["why"] = "This table holds values that can't be stored."
            return out
        name = f"values/{fp[:24]}.csv.gz"
        if self.touched is not None:
            self.touched.add(name)
        facts = self.index.get(name)
        if facts is None:
            facts = self._write(df, name)
        out["file"] = name
        if facts.get("stored", "full") != "full":
            out["stored"] = facts["stored"]
            if facts["stored"] == "left_out":
                out.pop("file")
        return out

    def figure(self, fig: Any, png: Path | None = None) -> dict[str, Any]:
        """A figure's summary, its picture (PNG) kept once by its content as values/<fingerprint>.png: `png` is the
        picture made already (a remembered step's), else it is drawn now. The same figure isn't drawn twice until
        the next step starts (`figs`)."""
        got = self.figs.get(id(fig))
        if got is not None and got[0] is fig:
            return dict(got[1])
        data = None
        if png is not None:
            try:
                data = png.read_bytes()
            except OSError:
                data = None
        if data is None:
            try:
                buf = io.BytesIO()
                fig.savefig(buf, format="png")
                data = buf.getvalue()
            except Exception as e:  # a figure that can't be drawn: the record says so
                return {"kind": "figure", "stored": "left_out", "why": f"{type(e).__name__}: {e}"[:200]}
        name = f"values/{hashlib.sha256(data).hexdigest()[:24]}.png"
        if self.touched is not None:
            self.touched.add(name)
        if name not in self.index:
            self.index[name] = {"kind": "figure", "file": True, "bytes": len(data), "stored": "full"}
            self.bytes += len(data)
            self._write_pack(self.run_dir / name, [data])  # now: an output may be linked to it at once
        out = {"kind": "figure", "file": name}
        self.figs[id(fig)] = (fig, out)
        return dict(out)

    def _write(self, df: pd.DataFrame, name: str) -> dict[str, Any]:
        facts: dict[str, Any] = {"rows": int(len(df)), "columns": [str(c) for c in df.columns],
                                 "dtypes": {str(c): str(t) for c, t in zip(df.columns, df.dtypes)}, "stored": "full"}
        part = df
        if self.bytes > 2 * self.budget:
            facts["stored"] = "left_out"
            self.left_out += 1
            self.index[name] = facts
            return facts
        cells = len(df) * max(1, df.shape[1])
        if self.bytes > self.budget or cells > MAX_CELLS:
            keep = SAMPLE_ROWS if self.bytes > self.budget else max(1, MAX_CELLS // max(1, df.shape[1]))
            if keep < len(df):
                part = df.iloc[:keep]
                facts["stored"] = "sampled"
                facts["kept_rows"] = int(keep)
                self.sampled += 1
        if not isinstance(part.index, pd.RangeIndex) or part.index.start != 0 or part.index.step != 1:
            names = [n if n is not None else ("index" if part.index.nlevels == 1 else f"index_{i}")
                     for i, n in enumerate(part.index.names)]
            if not any(n in part.columns for n in names):
                part = part.rename_axis(names).reset_index()
                facts["index"] = names
        try:
            data = gzip.compress(part.to_csv(index=False).encode("utf-8"), compresslevel=1, mtime=0)
        except Exception as e:  # odd column names or values: the record says so instead
            facts["stored"] = "left_out"
            facts["why"] = f"{type(e).__name__}: {e}"[:200]
            self.left_out += 1
            self.index[name] = facts
            return facts
        facts["bytes"] = len(data)
        self.bytes += len(data)
        self.index[name] = facts
        self.pending.append((name, data))
        self.pending_bytes += len(data)
        if self.pending_bytes > PACK_FLUSH:
            self.flush()
        return facts

    def flush(self) -> None:
        """Write the tables held in memory as one pack."""
        if not self.pending:
            return
        pack = f"values/{uuid.uuid4().hex[:16]}.pack"
        offset = 0
        for name, data in self.pending:
            facts = self.index[name]
            facts["pack"], facts["offset"], facts["length"] = pack, offset, len(data)
            offset += len(data)
        do(self.later, self._write_pack, self.run_dir / pack, [data for _, data in self.pending])
        self.packs.add(pack)
        self.pending = []
        self.pending_bytes = 0

    @staticmethod
    def _write_pack(path: Path, parts: list[bytes]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            for data in parts:
                f.write(data)

    # --------------------------------------------------------------- files from a remembered step

    def adopt(self, folder: Path, files: dict[str, dict[str, Any]]) -> None:
        """Take over the tables of a remembered step: its packs come over as hard links (copies when the disk can't
        link). Raises FileNotFoundError when a pack is gone (the step is then run again)."""
        new = {name: facts for name, facts in files.items() if name not in self.index}
        for name, facts in new.items():
            if facts.get("kind") == "figure" and facts.get("file") and not (self.run_dir / name).exists():
                link_or_copy(Path(folder) / name, self.run_dir / name)  # a picture is a file of its own
        packs = {f["pack"] for f in new.values() if f.get("pack") and f.get("stored") != "left_out"}
        for pack in packs - self.packs:
            src = Path(folder) / pack
            dest = self.run_dir / pack
            if not dest.exists():
                self.dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(src, dest)
                except FileNotFoundError:
                    raise
                except OSError:
                    shutil.copyfile(src, dest)
            self.packs.add(pack)
        for name, facts in new.items():
            if facts.get("stored") != "left_out" and not facts.get("pack") and facts.get("kind") != "figure":
                raise FileNotFoundError(name)  # remembered before packs
            self.bytes += facts.get("bytes", 0)
            self.index[name] = dict(facts)

    def export(self, names: set[str], folder: Path) -> dict[str, dict[str, Any]]:
        """Put the packs holding these tables into a remembered step's folder; gives the tables' facts."""
        self.flush()
        out = {}
        packs = set()
        for name in names:
            facts = self.index.get(name)
            if facts is None:
                continue
            out[name] = facts
            if facts.get("pack"):
                packs.add(facts["pack"])
            elif facts.get("kind") == "figure" and facts.get("file"):
                do(self.later, link_or_copy, self.run_dir / name, Path(folder) / name)
        for pack in packs:
            do(self.later, link_or_copy, self.run_dir / pack, Path(folder) / pack)
        return out

    def report(self) -> dict[str, Any]:
        return {"dir": "values", "tables": len(self.index), "bytes": self.bytes, "budget": self.budget,
                "sampled": self.sampled, "left_out": self.left_out}

    def save_index(self) -> None:
        self.flush()
        if not self.index:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "index.json.tmp"
        tmp.write_text(json.dumps(self.index, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.dir / "index.json")


def _short(v: Any) -> str:
    try:
        text = repr(v)
    except Exception:
        text = f"<{type(v).__name__}>"
    return text if len(text) <= 120 else text[:120] + "…"


def files_in(summary: Any, out: set[str] | None = None) -> set[str]:
    """Every values/ file a summary (or a record made of summaries) refers to."""
    out = set() if out is None else out
    if isinstance(summary, dict):
        f = summary.get("file")
        if isinstance(f, str) and f.startswith("values/"):
            out.add(f)
        for x in summary.values():
            if isinstance(x, (dict, list)):
                files_in(x, out)
    elif isinstance(summary, list):
        for x in summary:
            if isinstance(x, (dict, list)):
                files_in(x, out)
    return out


# ---------------------------------------------------------------------------------------------------- reading


@lru_cache(maxsize=16)
def _index_of(path: str, mtime: float) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def index_of(store, run_id: str) -> dict[str, Any]:
    p = store.run_dir(run_id) / "values" / "index.json"
    return _index_of(str(p), p.stat().st_mtime) if p.exists() else {}


def _safe(store, run_id: str, file: str) -> Path:
    rdir = store.run_dir(run_id).resolve()
    rel = (file or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/") or not rel.startswith(("values/", "steps/", "outputs/")):
        raise LabError(f"'{file}' is not a value of run {run_id}: give the `file` of a value summary.")
    p = (rdir / rel).resolve()
    if rdir not in p.parents:
        raise LabError(f"'{file}' is not a value of run {run_id}.")
    return p


@lru_cache(maxsize=8)
def _read_table(path: str, mtime: float, dtypes: tuple, offset: int = -1, length: int = 0) -> pd.DataFrame:
    """A stored table: a whole file, or (offset >= 0) one gzip part of a pack."""
    text_columns = {c: str for c, t in dtypes if t in ("object", "string", "str")}
    try:
        if offset < 0:
            return pd.read_csv(path, dtype=text_columns or None, keep_default_na=True)
        with open(path, "rb") as f:
            f.seek(offset)
            data = gzip.decompress(f.read(length))
        return pd.read_csv(io.BytesIO(data), dtype=text_columns or None, keep_default_na=True)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def read(store, run_id: str, file: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """Rows of a stored value for display: {run, file, kind, total, offset, columns, rows, stored, ...}.

    `file` is a value summary's `file`: values/… (0.4), or a run's steps/….csv and outputs/….csv|png (every run
    since 0.3). A figure comes back as PNG data (base64)."""
    store.get_run(run_id)  # a clear message for a run that doesn't exist
    p = _safe(store, run_id, file)
    rdir = store.run_dir(run_id)
    rel = p.relative_to(rdir.resolve()).as_posix()
    facts = index_of(store, run_id).get(rel, {}) if rel.startswith("values/") else {}
    where, part = p, (-1, 0)
    if facts.get("pack"):
        where, part = _safe(store, run_id, facts["pack"]), (int(facts["offset"]), int(facts["length"]))
    if not where.exists():
        if rel.startswith("values/") and not (rdir / "values").exists():
            raise LabError(f"Run {run_id} no longer keeps the values inside its steps (they are cleared from older "
                           "runs to save space): run the experiment again to see them.")
        if facts.get("stored") == "left_out":
            raise LabError(f"Run {run_id} didn't keep the table {rel}: {facts.get('why') or 'too big'}.")
        raise LabError(f"Run {run_id} has no file {rel}.")
    if p.suffix.lower() == ".png":
        return {"run": run_id, "file": rel, "kind": "figure", "format": "png",
                "data": base64.b64encode(p.read_bytes()).decode("ascii")}
    dtypes = tuple(sorted((facts.get("dtypes") or {}).items()))
    try:
        df = _read_table(str(where), where.stat().st_mtime, dtypes, *part)
    except (OSError, EOFError, ValueError, zlib.error) as e:  # cut short or damaged (pandas' errors are ValueErrors)
        raise LabError(f"Run {run_id} can't show the table {rel}: its file is damaged. Run everything again to see "
                       "it.") from e
    from .store import records

    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 50), 2000))
    rows = df.iloc[offset: offset + limit]
    out = {"run": run_id, "file": rel, "kind": "table", "total": int(facts.get("rows", len(df))),
           "offset": offset, "columns": [str(c) for c in df.columns], "rows": records(rows),
           "stored": facts.get("stored", "full")}
    if facts.get("stored") == "sampled":
        out["kept_rows"] = int(len(df))
        out["note"] = (f"Only the first {len(df)} of {facts.get('rows')} rows were kept: the run's values passed "
                       "their size limit.")
    if facts.get("index"):
        out["index_columns"] = facts["index"]
    return out


@lru_cache(maxsize=8)
def _inside(path: str, mtime: float) -> dict[str, Any]:
    return json.loads(Path(path).read_text("utf-8"))


def inside_of(store, run_id: str) -> dict[str, Any] | None:
    """A run's record of the values inside its functions (inside.json, see lab/recorder.py's `Inside.report`), or
    None for a run from before 0.4 or one whose values inside were cleared."""
    p = store.run_dir(run_id) / "inside.json"
    if not p.exists():
        return None
    try:
        return _inside(str(p), p.stat().st_mtime)
    except (OSError, json.JSONDecodeError):
        return None

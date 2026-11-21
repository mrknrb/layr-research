"""Help while writing a method: completion, what a name is, a call's inputs, where a name is defined, renaming and
tidying the layout. Every function is pure (the source in, an answer out) and never runs the method.

Jedi reads the method's source as if it were `backend/method.py`, so `from toolbox import …` resolves to the real
toolbox and pandas to the installed pandas. Positions: `line` counts from 1, `column` from 0 (characters in the line).

- The first answer that needs pandas takes seconds (Jedi reads pandas once); `warm()` does that in the background
  when the server starts, later answers take tens of milliseconds.
- Columns of the tables known from the last run (`columns={"peaks": ["injection", "area", …]}`) come first after
  `peaks.` and are the only answers inside `peaks["`.
- The method's own functions and the toolbox come before everything else; names starting with `_` only when typed.
- Code Python can't read yet (she is typing) never fails a call: the answer says what couldn't be read.
"""

from __future__ import annotations

import ast
import inspect
import keyword
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .errors import LabError

BACKEND = Path(__file__).resolve().parent.parent
METHOD_PATH = BACKEND / "method.py"  # where Jedi thinks the method is (no such file is written)
LIMIT = 60  # completions returned at most
LINE_LENGTH = 120

_LOCK = threading.RLock()  # Jedi isn't safe to use from two threads at once
_JEDI: dict[str, Any] = {}
WARM: dict[str, Any] = {"state": "cold", "ms": None, "error": None}

KINDS = {"function": "function", "class": "class", "module": "module", "keyword": "keyword", "instance": "variable",
         "statement": "variable", "param": "variable", "property": "property", "path": "file"}
GROUPS = ["column", "own", "toolbox", "keyword", "builtin", "library"]
# What a method uses most of pandas and numpy: shown before the rest of a library's names.
COMMON = {"groupby", "merge", "copy", "sort_values", "loc", "iloc", "columns", "index", "isin", "between", "mean",
          "std", "sum", "count", "min", "max", "median", "round", "rename", "drop", "dropna", "fillna", "notna", "isna",
          "unique", "nunique", "empty", "assign", "agg", "apply", "query", "head", "values", "to_numpy", "astype",
          "reset_index", "set_index", "concat", "DataFrame", "Series", "read_csv", "log", "exp", "sqrt", "abs", "nan",
          "array", "where", "pi"}
WARM_SOURCE = '''import numpy as np
import pandas as pd

from toolbox import fit_line, read_table


def step(table):
    """A step."""
    rows = table[table.role == "sample"]
    return rows.groupby("compound").mean()


def recipe(table_file):
    """The recipe."""
    table = read_table(table_file)
    return {"result": step(table)}
'''


# ---------------------------------------------------------------------------------------------------- Jedi


def _jedi():
    """Jedi, its project (backend/ on the path) and the running interpreter, made once."""
    with _LOCK:
        if not _JEDI:
            import jedi

            data = os.environ.get("LAYR_DATA_DIR")
            if data:
                jedi.settings.cache_directory = str(Path(data) / "jedi-cache")
            _JEDI["jedi"] = jedi
            _JEDI["project"] = jedi.Project(path=str(BACKEND), added_sys_path=[str(BACKEND)], smart_sys_path=False)
            _JEDI["env"] = jedi.InterpreterEnvironment()
        return _JEDI


def _script(source: str):
    j = _jedi()
    return j["jedi"].Script(source, path=str(METHOD_PATH), project=j["project"], environment=j["env"])


def warm(wait: bool = False) -> threading.Thread | None:
    """Read pandas, numpy and the toolbox into Jedi in a background thread, so the first real answer is quick.
    Called once when the server starts; later calls do nothing. `WARM` says how it went and how long it took."""
    with _LOCK:
        if WARM["state"] != "cold":
            return None
        WARM["state"] = "warming"

    def run() -> None:
        t0 = time.perf_counter()
        try:
            with _LOCK:
                lines = WARM_SOURCE.splitlines()
                at = next(i for i, ln in enumerate(lines, 1) if "table.role" in ln)
                _script(WARM_SOURCE).complete(at, lines[at - 1].index("table.") + 6)
                _script(WARM_SOURCE + "\nnp.").complete(len(lines) + 2, 3)
                _script(WARM_SOURCE + "\npd.DataFrame().").complete(len(lines) + 2, 15)
                gc = BACKEND / "lab" / "methods" / "gc-activity-assay.py"
                if gc.exists():  # the built-in method: its untyped table inputs are the slow case
                    text = gc.read_text("utf-8")
                    at = next((i for i, ln in enumerate(text.splitlines(), 1) if "peaks[peaks." in ln), None)
                    if at:
                        _script(text).complete(at, text.splitlines()[at - 1].index("peaks[peaks.") + 12)
                import toolbox  # noqa: F401  (its signatures are read from the functions themselves)
            WARM.update(state="warm", ms=round((time.perf_counter() - t0) * 1000))
        except Exception as e:  # a speed-up only: the first answer is then slow, nothing else
            WARM.update(state="failed", error=f"{type(e).__name__}: {e}", ms=round((time.perf_counter() - t0) * 1000))

    thread = threading.Thread(target=run, name="jedi-warm", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return thread


# ---------------------------------------------------------------------------------------------------- the source


def _lines(source: str) -> list[str]:
    return source.replace("\r\n", "\n").split("\n")


def _place(source: str, line: int, column: int) -> tuple[str, int, int]:
    """The source with plain line ends, and the position moved inside it (a line past the end is the last line)."""
    source = source.replace("\r\n", "\n")
    lines = _lines(source)
    line = min(max(int(line), 1), len(lines))
    column = min(max(int(column), 0), len(lines[line - 1]))
    return source, line, column


class _File:
    """What can be read from the source without Jedi: own functions, toolbox imports, whether Python can read it."""

    def __init__(self, source: str):
        self.functions: dict[str, ast.FunctionDef] = {}
        self.names: set[str] = set()  # every name defined at the top of the file
        self.toolbox: set[str] = set()  # names imported from the toolbox
        self.note: str | None = None
        self.method = None
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            self.note = (f"Line {e.lineno} can't be read by Python yet ({e.msg}), so this help may be incomplete."
                         if e.lineno else "Python can't read the file yet, so this help may be incomplete.")
            self._guess(source)
            return
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.names.add(node.name)
                if not isinstance(node, ast.ClassDef):
                    self.functions[node.name] = node
            elif isinstance(node, ast.Assign):
                self.names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "toolbox":
                self.toolbox |= {a.asname or a.name for a in node.names}
        try:
            from . import methodfile

            self.method = methodfile.parse(source)
        except Exception:  # the help works without it
            self.method = None

    def _guess(self, source: str) -> None:
        for m in re.finditer(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)|^(\w+)\s*=", source, re.MULTILINE):
            self.names.add(m.group(1) or m.group(2) or m.group(3))
        for m in re.finditer(r"^from\s+toolbox\S*\s+import\s+\(?([^)\n]*)", source, re.MULTILINE):
            self.toolbox |= {n.split(" as ")[-1].strip() for n in m.group(1).split(",") if n.strip()}

    def sentence(self, name: str) -> str:
        fn = self.method.function(name) if self.method else None
        if fn is not None:
            return fn.doc.sentence
        node = self.functions.get(name)
        doc = ast.get_docstring(node) if node else None
        return doc.strip().splitlines()[0] if doc and doc.strip() else ""

    def signature(self, name: str) -> str:
        node = self.functions.get(name)
        if node is None:
            return name
        return f"{name}({ast.unparse(node.args)})"

    def recipe_params(self) -> set[str]:
        fn = self.method.recipe_function if self.method else None
        return set(fn.params) if fn else set()


def _toolbox_object(name: str):
    try:
        import toolbox

        return getattr(toolbox, name, None) if name in toolbox.__all__ else None
    except Exception:  # the toolbox is the plugin's own code; help without it rather than fail
        return None


def _first_sentence(doc: str | None) -> str:
    text = (doc or "").strip()
    return text.splitlines()[0].strip() if text else ""


def _toolbox_info(name: str) -> tuple[str, str] | None:
    """A toolbox function's signature and sentence, from the function itself."""
    obj = _toolbox_object(name)
    if obj is None:
        return None
    return _signature_text(name, obj), _first_sentence(inspect.getdoc(obj))


def _signature_text(name: str, obj) -> str:
    """`fit_lines(table: DataFrame, x: str, y: str, by: str | list[str] | None = None) -> DataFrame`, as written."""
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return name

    def note(a) -> str:
        text = a if isinstance(a, str) else getattr(a, "__forward_arg__", None) or getattr(a, "__name__", str(a))
        return text.replace("pd.DataFrame", "DataFrame")

    parts = []
    for p in sig.parameters.values():
        text = ("*" if p.kind == p.VAR_POSITIONAL else "**" if p.kind == p.VAR_KEYWORD else "") + p.name
        if p.annotation is not p.empty:
            text += f": {note(p.annotation)}"
        if p.default is not p.empty:
            text += f" = {p.default!r}" if p.annotation is not p.empty else f"={p.default!r}"
        parts.append(text)
    tail = f" -> {note(sig.return_annotation)}" if sig.return_annotation is not sig.empty else ""
    return f"{name}({', '.join(parts)}){tail}"


# ---------------------------------------------------------------------------------------------------- completion

_ATTRIBUTE = re.compile(r"([A-Za-z_]\w*)\s*\.\s*(\w*)$")
_SUBSCRIPT = re.compile(r"([A-Za-z_]\w*)\s*\[\s*(['\"])([^'\"]*)$")


def _matches(word: str, prefix: str) -> bool:
    return word.lower().startswith(prefix.lower())


def complete(source: str, line: int, column: int, columns: dict[str, list[str]] | None = None,
             limit: int = LIMIT) -> dict[str, Any]:
    """What can be typed here: {items: [{label, insert, kind, detail, info?, group}], from (the column where the
    typed part starts: the editor replaces from there to the cursor), prefix, note?}.

    kind: column, function, class, module, keyword, variable, property, file. group: column, own (the method's own),
    toolbox, keyword, builtin, library; the items come in that order."""
    source, line, column = _place(source, line, column)
    columns = columns or {}
    before = _lines(source)[line - 1][:column]
    after = _lines(source)[line - 1][column:]
    f = _File(source)
    out: dict[str, Any] = {"items": [], "from": column, "prefix": ""}
    if f.note:
        out["note"] = f.note
    sub = _SUBSCRIPT.search(before)
    if sub:  # inside table["…: only that table's columns
        name, quote, prefix = sub.groups()
        out.update(prefix=prefix, **{"from": column - len(prefix)})
        close = "" if after.startswith(quote) else quote + ("" if after.lstrip(quote).startswith("]") else "]")
        out["items"] = [{"label": c, "insert": c + close, "kind": "column", "detail": f"column of {name}",
                         "group": "column"} for c in columns.get(name, []) if _matches(str(c), prefix)][:limit]
        if name not in columns:
            out["note"] = (out.get("note") or "") + (" " if out.get("note") else "") + \
                f"The columns of `{name}` are known after a run."
        return out
    attr = _ATTRIBUTE.search(before)
    prefix = attr.group(2) if attr else (re.search(r"\w*$", before).group(0))
    out.update(prefix=prefix, **{"from": column - len(prefix)})
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    if attr and attr.group(1) in columns:
        for c in columns[attr.group(1)]:
            c = str(c)
            if c.isidentifier() and not keyword.iskeyword(c) and _matches(c, prefix) and c not in seen:
                seen.add(c)
                items.append({"label": c, "insert": c, "kind": "column", "detail": f"column of {attr.group(1)}",
                              "group": "column"})
    try:
        with _LOCK:
            if attr and attr.group(1) in columns:  # a known table: complete it as a table, without reading the file
                table = attr.group(1)
                stub = f"import pandas as pd\n{table} = pd.DataFrame()\n{table}.{prefix}"
                found = _script(stub).complete(3, len(table) + 1 + len(prefix))
            else:
                found = _script(source).complete(line, column)
            ranked = []
            for i, c in enumerate(found):
                name = c.name
                if name in seen or (name.startswith("_") and not prefix.startswith("_")):
                    continue
                group = _group(c, f, attribute=bool(attr))
                ranked.append((GROUPS.index(group), name not in COMMON, i, c, group))
            ranked.sort(key=lambda r: r[:3])
            for *_, c, group in ranked[:max(limit - len(items), 0)]:
                seen.add(c.name)
                items.append(_item(c, group, f))
    except Exception as e:  # Jedi can fail on code it can't read; say so instead of failing
        out["note"] = (out.get("note") or "") + (" " if out.get("note") else "") + \
            f"Completion stopped early: {type(e).__name__}."
    out["items"] = items[:limit]
    return out


def _group(c, f: _File, attribute: bool) -> str:
    module = c.module_name or ""
    if c.type == "keyword":
        return "keyword"
    if attribute:
        if module.split(".")[0] == "toolbox":
            return "toolbox"
        return "own" if module == "method" else "builtin" if module == "builtins" else "library"
    if c.name in f.toolbox:
        return "toolbox"
    if module == "method":
        return "own"
    if module == "builtins":
        return "builtin"
    return "library"


def _item(c, group: str, f: _File) -> dict[str, Any]:
    kind = KINDS.get(c.type, "variable")
    item = {"label": c.name, "insert": c.name, "kind": kind, "group": group, "detail": ""}
    if group == "toolbox" and (info := _toolbox_info(c.name)):
        item["detail"], item["info"] = info
        if kind == "variable":
            item["kind"] = "function" if callable(_toolbox_object(c.name)) else kind
    elif group == "own" and c.name in f.functions:
        item["detail"], item["info"] = f.signature(c.name), f.sentence(c.name)
    elif kind == "module":
        item["detail"] = "module"
    elif kind == "keyword":
        item["detail"] = "Python keyword"
    elif kind in ("function", "class", "property"):
        try:
            item["detail"] = _first_sentence(c.docstring(raw=True))[:120]
        except Exception:  # a detail only
            pass
    else:
        item["detail"] = (c.description or "")[:80]
    return item


# ---------------------------------------------------------------------------------------------------- hover


def _word_at(text: str, column: int) -> tuple[str, int, int]:
    start = column
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    end = column
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return text[start:end], start, end


def _column_at(text: str, column: int, columns: dict[str, list[str]]) -> dict[str, Any] | None:
    """`peaks.area` or `peaks["area"]` under the cursor, when `area` is a known column of `peaks`."""
    for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*(['\"])([^'\"]+)\3\s*\])", text):
        table, col = m.group(1), m.group(2) or m.group(4)
        col_start = m.start(2) if m.group(2) else m.start(4)
        if col_start <= column <= col_start + len(col) and col in [str(c) for c in columns.get(table, [])]:
            return {"name": col, "kind": "column", "table": table, "signature": f'{table}["{col}"]',
                    "sentence": f"A column of `{table}` (from the last run)."}
    return None


def hover(source: str, line: int, column: int, columns: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """What the name under the cursor is: {name, kind, signature, sentence, module, type?, line?, note?}; {} (with a
    note when the file can't be read) when there is nothing to say."""
    source, line, column = _place(source, line, column)
    text = _lines(source)[line - 1]
    f = _File(source)
    found = _column_at(text, column, columns or {})
    if found:
        return found
    word, start, _ = _word_at(text, column)
    if not word or word[0].isdigit():
        return {"note": f.note} if f.note else {}
    out: dict[str, Any] = {"name": word}
    if f.note:
        out["note"] = f.note
    if keyword.iskeyword(word):
        return {**out, "kind": "keyword", "signature": word, "sentence": "A Python keyword (see the Python guide)."}
    try:
        with _LOCK:
            script = _script(source)
            defs = script.infer(line, start) or script.goto(line, start)
            if not defs:
                return out
            d = defs[0]
            return {**out, **_describe(d, f, word)}
    except Exception as e:  # say what went wrong rather than fail the editor
        return {**out, "note": (out.get("note") or "") + f" No help here: {type(e).__name__}."}


def _describe(d, f: _File, word: str) -> dict[str, Any]:
    """A Jedi definition in plain words: its kind, signature, sentence and where it comes from."""
    module = d.module_name or ""
    kind = KINDS.get(d.type, "variable")
    own = module == "method"
    out: dict[str, Any] = {"kind": kind, "module": "this method" if own else module}
    if own and d.line:
        out["line"] = d.line
    if own and d.name in f.functions and d.type == "function":
        kind_word = f.method.function(d.name).kind if f.method and f.method.function(d.name) else "function"
        return {**out, "signature": f.signature(d.name), "sentence": f.sentence(d.name), "function_kind": kind_word}
    if module.split(".")[0] == "toolbox" and (info := _toolbox_info(d.name)):
        return {**out, "module": module, "signature": info[0], "sentence": info[1]}
    if d.type == "instance":  # a value: say what it is (e.g. a DataFrame)
        out.update(kind="variable", type=d.name, signature=f"{word}: {d.name}")
        if d.name == "DataFrame":
            out["sentence"] = "A table (pandas DataFrame)."
        else:
            out["sentence"] = _first_sentence(d.docstring(raw=True))[:200]
        return out
    try:
        sigs = d.get_signatures()
    except Exception:  # a signature only
        sigs = []
    out["signature"] = sigs[0].to_string() if sigs else d.name
    out["sentence"] = _first_sentence(d.docstring(raw=True))[:200]
    return out


# ---------------------------------------------------------------------------------------------------- signature


def signature(source: str, line: int, column: int) -> dict[str, Any]:
    """The function being called at the cursor and which of its inputs is being typed: {signatures: [{name, label,
    params: [{name, text}], index, sentence}], note?}."""
    source, line, column = _place(source, line, column)
    f = _File(source)
    out: dict[str, Any] = {"signatures": []}
    if f.note:
        out["note"] = f.note
    try:
        with _LOCK:
            for s in _script(source).get_signatures(line, column):
                module = s.module_name or ""
                if module == "method" and s.name in f.functions:
                    sentence = f.sentence(s.name)
                elif module.split(".")[0] == "toolbox" and (info := _toolbox_info(s.name)):
                    sentence = info[1]
                else:
                    sentence = _first_sentence(s.docstring(raw=True))[:200]
                out["signatures"].append({"name": s.name, "label": s.to_string(), "index": s.index,
                                          "params": [{"name": p.name, "text": p.to_string()} for p in s.params],
                                          "sentence": sentence})
    except Exception as e:  # never fail the editor
        out["note"] = (out.get("note") or "") + f" No signature here: {type(e).__name__}."
    return out


# ---------------------------------------------------------------------------------------------------- definition


def definition(source: str, line: int, column: int) -> dict[str, Any]:
    """Where the name under the cursor is defined: {definitions: [{name, kind, in_file, line, column, module,
    sentence}], note?}. `in_file`: in this method (go to the line); otherwise e.g. a toolbox function (`module`
    toolbox.fit: show it with toolbox_docs)."""
    source, line, column = _place(source, line, column)
    f = _File(source)
    out: dict[str, Any] = {"definitions": []}
    if f.note:
        out["note"] = f.note
    try:
        with _LOCK:
            for d in _script(source).goto(line, column, follow_imports=True):
                module = d.module_name or ""
                own = module == "method"
                item = {"name": d.name, "kind": KINDS.get(d.type, "variable"), "in_file": own, "line": d.line,
                        "column": d.column, "module": "this method" if own else module}
                if own and d.name in f.functions:
                    item["sentence"] = f.sentence(d.name)
                elif module.split(".")[0] == "toolbox" and (info := _toolbox_info(d.name)):
                    item["sentence"] = info[1]
                    item["toolbox"] = d.name
                out["definitions"].append(item)
    except Exception as e:  # never fail the editor
        out["note"] = (out.get("note") or "") + f" Not found: {type(e).__name__}."
    return out


# ---------------------------------------------------------------------------------------------------- rename


def rename(source: str, line: int, column: int, new_name: str) -> dict[str, Any]:
    """Rename the name under the cursor everywhere it means the same thing in the file: {source, old_name, new_name,
    lines (the line numbers that changed), warning?}. Raises LabError when it can't be done."""
    source, line, column = _place(source, line, column)
    new_name = (new_name or "").strip()
    if not new_name.isidentifier() or keyword.iskeyword(new_name):
        raise LabError(f"`{new_name}` can't be a name in Python: use letters, digits and _, not starting with a "
                       "digit (e.g. conc_mM).")
    text = _lines(source)[line - 1]
    old, start, _ = _word_at(text, column)
    if not old or keyword.iskeyword(old):
        raise LabError("Put the cursor on the name to rename.")
    f = _File(source)
    if f.note:
        raise LabError(f"{f.note.split(',')[0]}: fix that first, then rename.")
    if old in f.toolbox:
        raise LabError(f"`{old}` comes from the toolbox: copy it into the method first (toolbox_copy) to rename it.")
    if new_name == old:
        return {"source": source, "old_name": old, "new_name": new_name, "lines": []}
    nodes = list(ast.walk(ast.parse(source)))
    taken = f.names | {n.id for n in nodes if isinstance(n, ast.Name)} | \
        {n.arg for n in nodes if isinstance(n, ast.arg)}
    if new_name in taken:
        raise LabError(f"`{new_name}` is already used in this method: pick another name.")
    with _LOCK:
        script = _script(source)
        try:
            refactoring = script.rename(line, start, new_name=new_name)
        except Exception as e:  # Jedi says why in its own words
            raise LabError(f"`{old}` can't be renamed here: {e}") from e
        changed = refactoring.get_changed_files()
        others = [p for p in changed if Path(p).resolve() != METHOD_PATH.resolve()]
        if others:
            raise LabError(f"`{old}` is defined outside this method ({Path(others[0]).name}): only the method's own "
                           "names can be renamed.")
        if not changed:
            raise LabError(f"`{old}` isn't a name this method defines.")
        new = next(iter(changed.values())).get_new_code()
    before, after = _lines(source), _lines(new)
    out = {"source": new, "old_name": old, "new_name": new_name,
           "lines": [i for i, (a, b) in enumerate(zip(before, after), 1) if a != b]}
    if old in f.recipe_params():
        out["warning"] = (f"`{old}` is a setting or input of the recipe: experiments keep their settings by name, so "
                          f"those that set `{old}` lose that value.")
    return out


# ---------------------------------------------------------------------------------------------------- format


def format_source(source: str) -> dict[str, Any]:
    """The source laid out by Ruff (lines up to 120 characters): {source, changed, error?}. Code Python can't read
    comes back unchanged with the reason. Only when she asks: never on its own."""
    try:
        from ruff.__main__ import find_ruff_bin

        ruff = find_ruff_bin()
    except Exception as e:  # ruff is a dependency; still never fail the editor
        return {"source": source, "changed": False, "error": f"The formatter (Ruff) isn't available: {e}"}
    try:
        done = subprocess.run([ruff, "format", "--isolated", "--line-length", str(LINE_LENGTH), "--stdin-filename",
                               "method.py", "-"], input=source.encode("utf-8"), capture_output=True, timeout=30,
                              check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"source": source, "changed": False, "error": f"The formatter didn't run: {e}"}
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        where = next((ln for ln in detail if ":" in ln), detail[0] if detail else "")
        m = re.search(r":(\d+):(\d+):", where)
        reason = f"line {m.group(1)} can't be read by Python" if m else "Python can't read the file"
        return {"source": source, "changed": False, "error": f"Not formatted: {reason}. Fix it first."}
    new = done.stdout.decode("utf-8").replace("\r\n", "\n")
    return {"source": new, "changed": new != source.replace("\r\n", "\n")}

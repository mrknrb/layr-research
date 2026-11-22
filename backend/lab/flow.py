"""The recipe drawn as a flow, and every change made in the flow as a small edit of the code.

The flow is computed from the method's Python file every time; nothing of it is stored.
- `name = step(arguments)` (also `a, b = step(...)`, or a call whose result isn't kept) is a **call node**. Its ports
  are the callee's parameters; an argument is a **wire** (a name an earlier node made), a **setting** or an **input**
  (a recipe parameter), or a **value** written in place (`2`, `g(x)`: its code is shown).
- Any other statement (an `if`, a loop, arithmetic) is a **code node** that shows its lines: its ports are the names
  it reads that an earlier node, a setting or an input made; its outputs are the names it gives a value to.
- A comment line `# --- Title` in the recipe starts a group of nodes; the next such line, or `# ---`, ends it.
- The returned dict's entries are the results. The nodes whose results reach nothing but the `flags` result are the
  quality lane, found from the wiring alone.

`graph(method, run, experiment)` gives the flow; `edit(source, op)` gives the new source after one change made in the
flow: only the lines it has to touch change (comments and everything else stay byte for byte), and new or changed
statements are wrapped at 120 columns the way the method files are. Edits refuse, in plain words, what would break
the method. `new_method(name, description)` gives a starting file.
"""

from __future__ import annotations

import ast
import builtins
import functools
import hashlib
import inspect
import io
import json
import keyword
import math
import re
import textwrap
import tokenize
from typing import Any

from . import methodfile
from .errors import LabError

WIDTH = 120
QUALITY_OUTPUT = "flags"
OPS = ("add_step", "remove_step", "connect", "rename_wire", "add_setting", "remove_setting", "set_default", "add_input",
       "add_result", "remove_result", "new_function", "group", "ungroup", "add_code", "disconnect", "remove_input")
FLAGS_MAKERS = {"flags_table", "combine_flags"}
MASK_METHODS = {"isin", "between", "notna", "isna", "notnull", "isnull", "duplicated", "contains", "startswith",
                "endswith", "match", "fullmatch", "eq", "ne", "gt", "ge", "lt", "le", "any", "all"}
COLUMN_FIELDS = ("column", "columns")  # palette field kinds whose value names columns of the tables in `of`
SAME_COLUMNS = {"copy", "sort_values", "dropna", "reset_index", "query", "head", "tail", "drop_duplicates", "fillna",
                "sample", "nlargest", "nsmallest", "sort_index", "set_index", "astype", "round", "abs", "replace",
                "rename", "assign", "pipe", "convert_dtypes", "infer_objects", "interpolate", "ffill", "bfill"}
COLUMN_ARGS = {"sort_values": ("by", 0), "drop_duplicates": ("subset", 0), "set_index": ("keys", 0),
               "nlargest": ("columns", 1), "nsmallest": ("columns", 1), "dropna": ("subset", None),
               "groupby": ("by", 0)}
CHANGING = {"append", "extend", "insert", "pop", "remove", "clear", "update", "setdefault", "popitem", "sort",
            "reverse", "add", "discard"}
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
_GROUP = re.compile(r"^[ \t]*#[ \t]*-{3,}(?P<title>.*)$")
_SECTION_NAMES = {"inputs": "Inputs", "settings": "Settings", "returns": "Returns", "outputs": "Returns"}


# ---------------------------------------------------------------------------------------------------- source text


class _Src:
    """A file's text as lines, with ast positions (UTF-8 byte columns) turned into text."""

    def __init__(self, text: str):
        self.text = text
        self.raw = _LINE.findall(text)
        self.lines = [ln.rstrip("\r\n") for ln in self.raw]
        self.nl = "\r\n" if "\r\n" in text else "\n"

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 0 < n <= len(self.lines) else ""

    def col(self, n: int, offset: int) -> int:
        line = self.line(n)
        return offset if line.isascii() else len(line.encode("utf-8")[:offset].decode("utf-8", "replace"))

    def seg(self, node: ast.AST) -> str:
        l0, l1 = node.lineno, node.end_lineno or node.lineno
        c0, c1 = self.col(l0, node.col_offset), self.col(l1, node.end_col_offset or 0)
        if l0 == l1:
            return self.line(l0)[c0:c1]
        return "\n".join([self.line(l0)[c0:]] + self.lines[l0:l1 - 1] + [self.line(l1)[:c1]])

    def span(self, node: ast.AST) -> tuple[int, int, int, int]:
        """(first line, first column, last line, end column) in characters."""
        l0, l1 = node.lineno, node.end_lineno or node.lineno
        return l0, self.col(l0, node.col_offset), l1, self.col(l1, node.end_col_offset or 0)


def _lead(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _apply(src: _Src, patches: list[tuple[int, int, list[str]]]) -> str:
    """Replace whole lines: each patch is (first line, last line, new lines); last = first - 1 inserts before first."""
    raw = list(src.raw)
    missing_end = bool(raw) and not raw[-1].endswith(("\n", "\r"))
    if missing_end:
        raw[-1] += src.nl
    for first, last, new in sorted(patches, key=lambda p: (p[0], p[1]), reverse=True):
        raw[first - 1:last] = [ln + src.nl for ln in new]
    text = "".join(raw)
    if missing_end and text.endswith(src.nl):
        text = text[:-len(src.nl)]
    return text


def _wrap(head: str, items: list[str], tail: str, width: int = WIDTH) -> list[str]:
    """`head`, the items separated by commas, then `tail`; wrapped at `width` with every continuation line aligned
    under the first item (the way the method files wrap calls, signatures and imports)."""
    if not items:
        return [head + tail]
    lines: list[str] = []
    cur = head
    pad = " " * len(head)
    for i, item in enumerate(items):
        piece = item + ("," if i < len(items) - 1 else tail)
        if i == 0:
            cur += piece
        elif len(cur) + 1 + len(piece) <= width:
            cur += " " + piece
        else:
            lines.append(cur)
            cur = pad + piece
    lines.append(cur)
    return lines


def _relayout(old_lines: list[str], old_items: list[tuple[str, int]], new_items: list[str], head: str, tail: str,
              width: int = WIDTH) -> list[str]:
    """Like `_wrap`, but keeping the old lines wherever nothing changed: the lines before the first changed item stay
    as they were, the changed part is wrapped again, and once the wrapping meets an old line start after the change
    the old lines are taken again. `old_items` are (text, index of the old line it is on)."""
    n, m = len(old_items), len(new_items)
    if not old_items or any("\n" in t for t, _ in old_items) or any("\n" in t for t in new_items):
        return _wrap(head, new_items, tail, width)
    k = 0
    while k < min(n, m) and old_items[k][0] == new_items[k]:
        k += 1
    if k == n == m:
        return list(old_lines)
    s = 0
    while s < min(n, m) - k and old_items[n - 1 - s][0] == new_items[m - 1 - s]:
        s += 1
    line = old_items[min(k, n - 1)][1]
    start = next(i for i, (_, ln) in enumerate(old_items) if ln == line)
    pad = " " * len(head)
    cur = head if line == 0 else pad
    if not old_lines[line].startswith(cur) or start >= m:
        return _wrap(head, new_items, tail, width)
    starts = {j for j in range(n) if j == 0 or old_items[j][1] != old_items[j - 1][1]}
    out = list(old_lines[:line])
    for i in range(start, m):
        piece = new_items[i] + ("," if i < m - 1 else tail)
        if i == start:
            cur += piece
        elif len(cur) + 1 + len(piece) <= width:
            cur += " " + piece
        else:
            j = i - m + n
            out.append(cur)
            if i >= m - s and j in starts and old_items[j][1] > line:
                return out + list(old_lines[old_items[j][1]:])
            cur = pad + piece
    out.append(cur)
    return out


def _wrap_text(first: str, rest_indent: str, width: int = WIDTH) -> list[str]:
    """A docstring line wrapped by words; continuation lines get `rest_indent`."""
    lead = _lead(first)
    lines: list[str] = []
    cur = lead
    for w in first.split():
        if cur.strip() and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = rest_indent + w
        else:
            cur = (cur + " " + w) if cur.strip() else cur + w
    lines.append(cur)
    return lines


def _has_comment(text: str) -> bool:
    if "#" not in text:
        return False
    try:
        return any(t.type == tokenize.COMMENT for t in tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return True


def _comments_kept(old_lines: list[str], new_lines: list[str]) -> list[str]:
    """`new_lines` (a signature or an import written again) with the line-end comments of `old_lines` it dropped put
    back: each at the end of the new line holding the code it followed (else the last line)."""
    try:
        found = [t for t in tokenize.generate_tokens(io.StringIO("\n".join(old_lines) + "\n").readline)
                 if t.type == tokenize.COMMENT]
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return new_lines
    out = list(new_lines)
    for t in found:
        if any(t.string in ln for ln in out):
            continue
        code = old_lines[t.start[0] - 1][:t.start[1]].strip().rstrip(",:)").strip()
        piece = code.split(",")[-1].strip()
        at = next((i for i, ln in enumerate(out) if piece and piece in ln), len(out) - 1)
        out[at] = out[at].rstrip() + "  " + t.string
    return out


def _humanize(name: str) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])", " ", name).replace("_", " ").split()
    if not words:
        return name
    out = [words[0][:1].upper() + words[0][1:]]
    out += [w.lower() if w[:1].isupper() and w[1:].islower() else w for w in words[1:]]
    return " ".join(out)


def _py(value: Any) -> str:
    """A plain value (from JSON) written as Python."""
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise LabError("A setting's value must be a plain number, not NaN or infinity.")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_py(v) for v in value)
        return f"({inner}{',' if len(value) == 1 else ''})" if isinstance(value, tuple) else f"[{inner}]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_py(str(k))}: {_py(v)}" for k, v in value.items()) + "}"
    raise LabError(f"{value!r} can't be written as a plain value.")


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "group"


# ---------------------------------------------------------------------------------------------------- statements


def _is_doc(st: ast.stmt) -> bool:
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def _call_parts(st: ast.stmt) -> tuple[list[str], ast.AST | None, ast.Call] | None:
    """For `name = f(...)`, `a, b = f(...)` or a bare `f(...)` (f a plain name): (output names, target, call)."""
    target = None
    if isinstance(st, ast.Assign) and len(st.targets) == 1:
        target, value = st.targets[0], st.value
    elif isinstance(st, ast.AnnAssign) and st.value is not None and st.simple:
        target, value = st.target, st.value
    elif isinstance(st, ast.Expr):
        value = st.value
    else:
        return None
    if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
        return None
    if target is None:
        return [], None, value
    if isinstance(target, ast.Name):
        return [target.id], target, value
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for e in target.elts:
            e = e.value if isinstance(e, ast.Starred) else e
            if not isinstance(e, ast.Name):
                return None
            names.append(e.id)
        return names, target, value
    return None


def _root(t: ast.AST) -> ast.AST:
    while isinstance(t, (ast.Subscript, ast.Attribute, ast.Starred)):
        t = t.value
    return t


def _assigned(st: ast.stmt) -> list[str]:
    """The names a statement gives a value to, at the recipe's level (also `table["x"] = ...`, `rows.append(...)`,
    a loop's variable, names set inside an `if`)."""
    out: list[str] = []

    def target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, (ast.Subscript, ast.Attribute, ast.Starred)):
            r = _root(t)
            if isinstance(r, ast.Name):
                out.append(r.id)

    def walk(s: ast.stmt) -> None:
        if isinstance(s, ast.Assign):
            for t in s.targets:
                target(t)
        elif isinstance(s, (ast.AugAssign, ast.AnnAssign)):
            target(s.target)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            target(s.target)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                if item.optional_vars is not None:
                    target(item.optional_vars)
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(s.name)
            return
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            out.extend((a.asname or a.name).split(".")[0] for a in s.names)
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and isinstance(s.value.func, ast.Attribute) \
                and s.value.func.attr in CHANGING:
            target(s.value.func.value)
        elif isinstance(s, ast.Try):
            for h in s.handlers:
                if h.name:
                    out.append(h.name)
        for n in ast.walk(s):
            if isinstance(n, ast.NamedExpr):
                target(n.target)
        for field in ("body", "orelse", "finalbody", "handlers"):
            for child in getattr(s, field, []) or []:
                if isinstance(child, ast.ExceptHandler):
                    for c in child.body:
                        walk(c)
                elif isinstance(child, ast.stmt):
                    walk(child)

    walk(st)
    return list(dict.fromkeys(out))


def _loads(node: ast.AST) -> list[str]:
    """Names read in a statement or expression, in the order they appear (not those bound inside comprehensions or
    lambdas)."""
    bound: set[str] = set()
    found: list[tuple[int, int, str]] = []
    for n in ast.walk(node):
        if isinstance(n, ast.comprehension):
            bound.update(x.id for x in ast.walk(n.target) if isinstance(x, ast.Name))
        elif isinstance(n, ast.Lambda):
            bound.update(a.arg for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            found.append((n.lineno, n.col_offset, n.id))
    return list(dict.fromkeys(name for _, _, name in sorted(found) if name not in bound))


def _calls_in(node: ast.AST) -> set[str]:
    return {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


# ---------------------------------------------------------------------------------------------------- callees


class _Param:
    __slots__ = ("name", "kind", "default", "ann")

    def __init__(self, name: str, kind: str, default: str | None = None, ann: str | None = None):
        self.name, self.kind, self.default, self.ann = name, kind, default, ann


class _Callee:
    __slots__ = ("name", "kind", "params", "returns", "sentence", "function", "obj", "opens_as", "module")

    def __init__(self, name: str, kind: str, params: list[_Param] | None = None, returns: str | None = None,
                 sentence: str = "", function: methodfile.Function | None = None, obj: Any = None,
                 opens_as: str | None = None, module: str | None = None):
        self.name, self.kind, self.params, self.returns, self.sentence = name, kind, params, returns, sentence
        self.function, self.obj, self.opens_as = function, obj, opens_as
        self.module = module  # a toolbox function's module to import it from: toolbox or toolbox.<add-on>


_TOOLBOX: dict[str, _Callee | None] = {}


def _ann_text(a: Any) -> str | None:
    if a is inspect.Parameter.empty or a is inspect.Signature.empty or a is None:
        return None
    return a if isinstance(a, str) else inspect.formatannotation(a)


def _signature_params(obj: Any) -> tuple[list[_Param] | None, str | None]:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None, None
    kinds = {inspect.Parameter.POSITIONAL_ONLY: "pos", inspect.Parameter.POSITIONAL_OR_KEYWORD: "pos",
             inspect.Parameter.VAR_POSITIONAL: "var", inspect.Parameter.KEYWORD_ONLY: "kw",
             inspect.Parameter.VAR_KEYWORD: "varkw"}
    params = [_Param(p.name, kinds[p.kind], None if p.default is inspect.Parameter.empty else repr(p.default),
                     _ann_text(p.annotation)) for p in sig.parameters.values()]
    return params, _ann_text(sig.return_annotation)


def _toolbox(name: str) -> _Callee | None:
    """A function or class of the toolbox or one of its add-ons, read with inspect (once per process)."""
    if name in _TOOLBOX:
        return _TOOLBOX[name]
    info = None
    try:
        from toolbox import registry

        module, obj = registry.find(name) or (None, None)
    except Exception:  # the toolbox is the plugin's own code; a broken import means "not known" here
        module, obj = None, None
    if obj is not None and callable(obj):
        params, returns = _signature_params(obj)
        doc = (inspect.getdoc(obj) or "").strip()
        info = _Callee(name, "class" if inspect.isclass(obj) else "toolbox", params, returns,
                       doc.splitlines()[0] if doc else "", obj=obj, opens_as="code", module=module)
    _TOOLBOX[name] = info
    return info


def _fn_params(node: ast.FunctionDef) -> list[_Param]:
    a = node.args
    pos = a.posonlyargs + a.args
    defaults: list[ast.AST | None] = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    out = [_Param(p.arg, "pos", ast.unparse(d) if d is not None else None,
                  ast.unparse(p.annotation) if p.annotation else None) for p, d in zip(pos, defaults)]
    if a.vararg:
        out.append(_Param(a.vararg.arg, "var"))
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        out.append(_Param(p.arg, "kw", ast.unparse(d) if d is not None else None,
                          ast.unparse(p.annotation) if p.annotation else None))
    if a.kwarg:
        out.append(_Param(a.kwarg.arg, "varkw"))
    return out


def _only_calls(node: ast.FunctionDef) -> bool:
    """A function whose body is only calls kept under names, and a return: it can be opened as a flow too."""
    from .latex import body_statements

    body = body_statements(node)
    if not body:
        return False
    for st in body:
        if isinstance(st, ast.Return):
            continue
        if _call_parts(st) is None:
            return False
    return True


# ---------------------------------------------------------------------------------------------------- the analysis


class _Node:
    def __init__(self, index: int, stmt: ast.stmt):
        self.index = index
        self.stmt = stmt
        self.id = ""
        self.kind = "code"
        self.fn: str | None = None
        self.callee: _Callee | None = None
        self.call: ast.Call | None = None
        self.target: ast.AST | None = None
        self.outs: list[str] = []
        self.deps: dict[str, int | None] = {}  # name read -> the node that made it (None: a recipe parameter)
        self.ports: list[dict[str, Any]] = []
        self.port_args: list[ast.AST | None] = []  # the argument behind each port (None: not passed)
        self.calls: set[str] = set()  # functions called inside it, other than its own callee

    @property
    def line(self) -> int:
        return self.stmt.lineno

    @property
    def end_line(self) -> int:
        return self.stmt.end_lineno or self.stmt.lineno


class _Analysis:
    """A method's recipe read as nodes, with what an edit needs to know about the text around them."""

    def __init__(self, src: _Src, method: methodfile.Method):
        self.src = src
        self.method = method
        fn = method.recipe_function
        if fn is None:
            raise LabError("The method has no recipe (the function that calls the steps), so there is no flow to "
                           "draw. Add one at the end of the file.")
        self.fn = fn
        self.node = fn.node
        body = list(fn.node.body)
        self.doc = body[0] if body and _is_doc(body[0]) else None
        rest = body[1:] if self.doc else body
        self.ret = rest[-1] if rest and isinstance(rest[-1], ast.Return) else None
        self.stmts = rest[:-1] if self.ret else rest
        self.indent = _lead(src.line(body[0].lineno)) if body else "    "
        self.params = list(fn.params)
        self.results = methodfile.result_inputs(method)  # inputs that take results of other experiments
        self.inputs = [p for p in self.params if methodfile.is_input(p, self.results)]
        self.settings = [p for p in self.params if not methodfile.is_input(p, self.results)]
        self.functions = {f.name for f in method.functions}
        self.classes = {n.name for n in method.tree.body if isinstance(n, ast.ClassDef)}
        self.module_names: set[str] = set()
        self.toolbox_imported: set[str] = set()
        self.toolbox_module: dict[str, str] = {}  # a toolbox name -> the module it is imported from
        for n in method.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.module_names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                self.module_names.update(_assigned(n))
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    self.module_names.add((a.asname or a.name).split(".")[0])
                    if isinstance(n, ast.ImportFrom) and _is_toolbox(n.module) and not n.level:
                        self.toolbox_imported.add(a.asname or a.name)
                        self.toolbox_module[a.asname or a.name] = n.module
        self._callees: dict[str, _Callee] = {}
        self.nodes: list[_Node] = []
        self.made: dict[str, int] = {}  # name -> the last node that made it
        self.messages: list[dict[str, Any]] = []
        self._build()
        self.outputs = self._outputs()
        self.groups = self._groups()

    # -------------------------------------------------------------- callees

    def callee(self, name: str) -> _Callee:
        if name in self._callees:
            return self._callees[name]
        f = self.method.function(name)
        if f is not None:
            c = _Callee(name, "recipe" if f.kind == "recipe" else "step" if f.kind == "step" else "helper",
                        _fn_params(f.node), ast.unparse(f.node.returns) if f.node.returns else None,
                        f.doc.sentence, function=f, opens_as="flow" if _only_calls(f.node) else "code")
        elif name in self.classes:
            c = _Callee(name, "class")
        else:
            c = _toolbox(name)
            if c is None:
                obj = getattr(builtins, name, None)
                if obj is not None and callable(obj):
                    params, returns = _signature_params(obj)
                    c = _Callee(name, "builtin", params, returns, obj=obj)
                else:
                    c = _Callee(name, "unknown")
        self._callees[name] = c
        return c

    # -------------------------------------------------------------- nodes

    def source_of(self, name: str, made: dict[str, int]) -> str | None:
        if name in made:
            return "wire"
        if name in self.inputs:
            return "input"
        if name in self.settings:
            return "setting"
        return None

    def _port(self, name: str, arg: ast.AST | None, keyword: bool, made: dict[str, int],
              param: _Param | None = None) -> dict[str, Any]:
        p: dict[str, Any] = {"name": name, "source": None, "wire": None, "from": None, "setting": None, "input": None,
                             "function": None, "code": None, "type": None, "columns_read": [], "missing": [],
                             "keyword": keyword, "reads": []}
        if arg is None:
            p["source"] = "default" if param is not None and param.default is not None else "empty"
            p["code"] = param.default if param is not None else None
            return p
        p["code"] = self.src.seg(arg)
        source = self.source_of(arg.id, made) if isinstance(arg, ast.Name) else None
        p["source"] = source or "value"
        if source == "wire":
            p["wire"] = arg.id
            p["from"] = made[arg.id]
        elif source == "setting":
            p["setting"] = arg.id
        elif source == "input":
            p["input"] = arg.id
        elif isinstance(arg, ast.Name) and arg.id in self.functions:
            f = self.method.function(arg.id)
            p["source"] = "formula" if f.kind == "formula" else "function"
            p["function"] = arg.id
        else:
            p["reads"] = [n for n in _loads(arg) if self.source_of(n, made)]
        return p

    def _call_ports(self, node: _Node, made: dict[str, int]) -> None:
        call = node.call
        params = node.callee.params if node.callee else None
        positional = [p for p in params or [] if p.kind == "pos"]
        var = next((p for p in params or [] if p.kind == "var"), None)
        bound: set[str] = set()
        starred = False
        for i, a in enumerate(call.args):
            if isinstance(a, ast.Starred):
                name, starred, param = "*" + self.src.seg(a.value), True, None
            elif starred:
                name, param = f"arg{i + 1}", None
            elif i < len(positional):
                param = positional[i]
                name = param.name
            elif var is not None:
                name, param = f"{var.name}[{i - len(positional)}]", None
            else:
                name = a.id if params is None and isinstance(a, ast.Name) else f"arg{i + 1}"
                param = None
            bound.add(name)
            node.ports.append(self._port(name, a, False, made, param))
            node.port_args.append(a)
        for kw in call.keywords:
            name = kw.arg or "**" + self.src.seg(kw.value)
            bound.add(name)
            param = next((p for p in params or [] if p.name == kw.arg), None)
            node.ports.append(self._port(name, kw.value, True, made, param))
            node.port_args.append(kw)
        if params is not None and not starred:
            for p in params:
                if p.kind in ("pos", "kw") and p.name not in bound:
                    node.ports.append(self._port(p.name, None, p.kind == "kw", made, p))
                    node.port_args.append(None)

    def _build(self) -> None:
        made: dict[str, int] = {}
        for i, st in enumerate(self.stmts):
            node = _Node(i, st)
            parts = _call_parts(st)
            loads = _loads(st)
            node.deps = {n: made.get(n) for n in loads if self.source_of(n, made)}
            if parts is not None:
                node.kind = "call"
                node.outs, node.target, node.call = parts
                node.fn = node.call.func.id
                node.callee = self.callee(node.fn)
                for a in list(node.call.args) + [k.value for k in node.call.keywords]:
                    node.calls |= _calls_in(a)
                if node.fn in made:
                    node.deps.pop(node.fn, None)
                self._call_ports(node, made)
                if node.callee.kind == "unknown":
                    self.messages.append({"line": st.lineno, "node": None, "message":
                                          f"`{node.fn}` is not a function of this method or of the toolbox."})
                elif node.callee.kind in ("toolbox", "class") and node.callee.obj is not None \
                        and node.fn not in self.toolbox_imported and node.fn not in self.module_names:
                    self.messages.append({"line": st.lineno, "node": None, "message":
                                          f"`{node.fn}` is a toolbox function but isn't imported: add it to the "
                                          f"`from {node.callee.module or 'toolbox'} import` line."})
            else:
                node.outs = _assigned(st)
                node.calls = _calls_in(st)
                for name in node.deps:
                    node.ports.append(self._port(name, ast.Name(id=name, ctx=ast.Load(), lineno=st.lineno,
                                                               col_offset=0, end_lineno=st.lineno,
                                                               end_col_offset=0), False, made))
                    node.ports[-1]["code"] = name
            for o in node.outs:
                made[o] = i
            self.nodes.append(node)
        self.made = made
        seen: dict[str, int] = {}
        for node in self.nodes:
            if node.outs:
                base = node.outs[0]
            elif node.kind == "call":
                base = f"call-{node.fn}"
            else:
                base = "code-" + _short_hash(ast.dump(node.stmt))
            seen[base] = seen.get(base, 0) + 1
            node.id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        for node in self.nodes:  # node indexes in the ports become node ids
            for p in node.ports:
                if isinstance(p["from"], int):
                    p["from"] = self.nodes[p["from"]].id

    def producer(self, name: str, before: int | None = None) -> int | None:
        """The node that last made `name` before node `before` (default: anywhere)."""
        best = None
        for node in self.nodes[:before] if before is not None else self.nodes:
            if name in node.outs:
                best = node.index
        return best

    def users(self, index: int) -> dict[str, list[_Node]]:
        """For each output of a node: the later nodes that read it (before it gets a new value)."""
        out: dict[str, list[_Node]] = {}
        for name in self.nodes[index].outs:
            out[name] = [n for n in self.nodes[index + 1:] if n.deps.get(name) == index]
        return out

    def descendants(self, index: int) -> set[int]:
        found = {index}
        for node in self.nodes[index + 1:]:
            if any(d in found for d in node.deps.values() if d is not None):
                found.add(node.index)
        found.discard(index)
        return found

    def find(self, node_id: str) -> _Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        known = ", ".join(n.id for n in self.nodes) or "none"
        raise LabError(f"There is no step `{node_id}` in the recipe. Steps: {known}.")

    def title(self, node: _Node) -> str:
        if node.kind == "call":
            return _humanize(node.fn)
        first = self.src.seg(node.stmt).splitlines()[0].strip().rstrip(":")
        return first if len(first) <= 60 else first[:59] + "…"

    def code(self, node: _Node) -> str:
        lines = self.src.lines[node.line - 1:node.end_line]
        return textwrap.dedent("\n".join(lines))

    # -------------------------------------------------------------- results and groups

    def _outputs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        labels = self.fn.doc.returns
        if self.ret is None:
            if not any(isinstance(n, ast.Return) for st in self.stmts for n in ast.walk(st)):
                self.messages.append({"line": self.fn.line, "node": None,
                                      "message": "The recipe returns nothing: add a result."})
            return out
        v = self.ret.value
        if isinstance(v, ast.Dict) and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in v.keys):
            for k, val in zip(v.keys, v.values):
                name = k.value
                item = {"id": name, "label": labels.get(name) or _humanize(name), "wire": None, "from": None,
                        "code": self.src.seg(val), "type": None}
                if isinstance(val, ast.Name) and val.id in self.made:
                    item["wire"] = val.id
                    item["from"] = self.nodes[self.made[val.id]].id
                else:
                    self.messages.append({"line": val.lineno, "node": None, "message":
                                          f"The result `{name}` is not a name a step made, so it is drawn without a "
                                          "wire."})
                out.append(item)
        else:
            self.messages.append({"line": self.ret.lineno, "node": None, "message":
                                  "The recipe's return is not a dict of named results (`return {\"name\": name}`), "
                                  "so its results can't be drawn."})
        return out

    def _groups(self) -> list[dict[str, Any]]:
        start = (self.doc.end_lineno or self.doc.lineno) + 1 if self.doc else self.node.body[0].lineno
        heads: list[tuple[int, str]] = []
        for ln in range(start, self.fn.end_line + 1):
            line = self.src.line(ln)
            if "#" in line:
                m = _GROUP.match(line)
                if m:
                    heads.append((ln, m.group("title").strip().rstrip("-").strip()))
        groups: list[dict[str, Any]] = []
        used: dict[str, int] = {}
        for i, (ln, title) in enumerate(heads):
            if not title:
                continue
            nxt = heads[i + 1] if i + 1 < len(heads) else None
            end = nxt[0] - 1 if nxt else self.fn.end_line
            slug = "group-" + _slug(title)
            used[slug] = used.get(slug, 0) + 1
            gid = slug if used[slug] == 1 else f"{slug}-{used[slug]}"
            members = [n.id for n in self.nodes if ln < n.line <= end]
            groups.append({"id": gid, "title": title, "line": ln, "end_line": end, "nodes": members,
                           "closed_by": nxt[0] if nxt and not nxt[1] else None})
        return groups

    def group_of(self, node: _Node) -> dict[str, Any] | None:
        return next((g for g in self.groups if node.id in g["nodes"]), None)


def _analyze(source: str | methodfile.Method | dict) -> _Analysis:
    if isinstance(source, dict):
        source = source.get("source") or ""
    if isinstance(source, methodfile.Method):
        method = source
        fn = method.recipe_function
        text = ("\n" * (fn.line - 1) + fn.source + "\n") if fn else ""
        return _Analysis(_Src(text), method)
    try:
        method = methodfile.parse(source)
    except SyntaxError as e:
        raise LabError(f"The method can't be read (line {e.lineno}): {e.msg}. Fix it in the code first.") from e
    return _Analysis(_Src(source), method)


# ---------------------------------------------------------------------------------------------------- columns read


_ATTRS: dict[str, set[str]] = {}


def _attrs(kind: str) -> set[str]:
    if not _ATTRS:
        import pandas as pd

        _ATTRS["table"] = set(dir(pd.DataFrame))
        _ATTRS["row"] = set(dir(pd.Series))
    return _ATTRS[kind]


def _strings(node: ast.AST | None) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts and \
            all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]
    return None


class _Columns:
    """Which columns a function reads from each of its parameters, from its code: `table.col`, `table["col"]`,
    `row["col"]` of the table's rows, grouping and sorting columns, also through the rows it picks, the copies it makes
    and the helpers it passes them to. A column the function makes itself (assigns, renames into) before reading it
    is not counted; a merged table isn't followed (its columns could come from either side)."""

    def __init__(self, method: methodfile.Method):
        self.method = method
        self.memo: dict[str, dict[str, tuple[list[str], set[str]]] | None] = {}
        self.busy: set[str] = set()

    def params(self, name: str) -> list[str] | None:
        f = self.method.function(name)
        if f is not None:
            return [p.name for p in _fn_params(f.node) if p.kind in ("pos", "kw")]
        tb = _toolbox(name)
        if tb is not None and tb.kind == "toolbox" and tb.params is not None:
            return [p.name for p in tb.params if p.kind in ("pos", "kw")]
        return None

    def of(self, name: str) -> dict[str, tuple[list[str], set[str]]] | None:
        if name in self.memo:
            return self.memo[name]
        if name in self.busy:
            return None
        node = None
        f = self.method.function(name)
        if f is not None:
            node = f.node
        else:
            tb = _toolbox(name)
            if tb is not None and tb.kind == "toolbox":
                try:
                    node = ast.parse(textwrap.dedent(inspect.getsource(tb.obj))).body[0]
                except (OSError, TypeError, SyntaxError, IndexError):
                    node = None
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.memo[name] = None
            return None
        self.busy.add(name)
        try:
            result = _FnColumns(self, node).run()
        finally:
            self.busy.discard(name)
        self.memo[name] = result
        return result


class _FnColumns:
    def __init__(self, reader: _Columns, node: ast.FunctionDef):
        self.reader = reader
        self.node = node
        a = node.args
        self.names = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
        self.alias: dict[str, tuple[str, str]] = {p: (p, "table") for p in self.names}
        self.reads: dict[str, list[str]] = {p: [] for p in self.names}
        self.made: dict[str, set[str]] = {p: set() for p in self.names}
        self.masks: set[str] = set()  # names holding a row condition (`keep = table.x > 0`), see `condition`
        self.guard = 0

    def run(self) -> dict[str, tuple[list[str], set[str]]]:
        self.body(self.node.body)
        return {p: (self.reads[p], self.made[p]) for p in self.names}

    def read(self, p: str, col: str) -> None:
        if not self.guard and col not in self.made[p] and col not in self.reads[p]:
            self.reads[p].append(col)

    # -------------------------------------------------------------- statements

    def body(self, stmts: list[ast.stmt]) -> None:
        for st in stmts:
            self.stmt(st)

    def stmt(self, st: ast.stmt) -> None:
        if isinstance(st, ast.Assign):
            o = self.ev(st.value)
            mask = self.condition(st.value) and not isinstance(st.value, ast.Slice)
            for t in st.targets:
                self.bind(t, o)
                if isinstance(t, ast.Name):
                    (self.masks.add if mask else self.masks.discard)(t.id)
        elif isinstance(st, ast.AnnAssign):
            self.bind(st.target, self.ev(st.value) if st.value is not None else None)
        elif isinstance(st, ast.AugAssign):
            self.ev(st.value)
            if isinstance(st.target, ast.Name):
                self.alias.pop(st.target.id, None)
            else:
                self.bind(st.target, None)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self.loop(st.target, self.ev(st.iter))
            self.body(st.body)
            self.body(st.orelse)
        elif isinstance(st, ast.While):
            self.ev(st.test)
            self.body(st.body)
            self.body(st.orelse)
        elif isinstance(st, ast.If):
            guarded = any(isinstance(n, ast.Attribute) and n.attr == "columns" for n in ast.walk(st.test))
            self.guard += guarded
            self.ev(st.test)
            self.body(st.body)
            self.body(st.orelse)
            self.guard -= guarded
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                self.ev(item.context_expr)
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, None)
            self.body(st.body)
        elif isinstance(st, ast.Try):
            self.body(st.body)
            for h in st.handlers:
                self.body(h.body)
            self.body(st.orelse)
            self.body(st.finalbody)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        else:
            for child in ast.iter_child_nodes(st):
                if isinstance(child, ast.expr):
                    self.ev(child)

    def bind(self, t: ast.AST, origin: tuple[str, str] | None) -> None:
        if isinstance(t, ast.Name):
            if origin is not None and origin[1] in ("table", "row", "record", "records", "groupby", "iterrows"):
                self.alias[t.id] = origin
            else:
                self.alias.pop(t.id, None)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self.bind(e, None)
        elif isinstance(t, ast.Starred):
            self.bind(t.value, None)
        elif isinstance(t, ast.Subscript):
            o = self.ev(t.value)
            if o is not None and o[1] == "table":
                for c in _strings(t.slice) or []:
                    self.made[o[0]].add(c)
                if _strings(t.slice) is None:
                    self.ev(t.slice)
            elif o is not None and o[1].startswith("indexer") and isinstance(t.slice, ast.Tuple) \
                    and len(t.slice.elts) == 2:
                self.ev(t.slice.elts[0])
                for c in _strings(t.slice.elts[1]) or []:
                    self.made[o[0]].add(c)
            else:
                self.ev(t.slice)
        elif isinstance(t, ast.Attribute):
            o = self.ev(t.value)
            if o is not None and o[1] == "table":
                self.made[o[0]].add(t.attr)

    def loop(self, t: ast.AST, origin: tuple[str, str] | None) -> None:
        if origin is None:
            self.bind(t, None)
            return
        p, kind = origin
        if kind == "records" and isinstance(t, ast.Name):
            self.alias[t.id] = (p, "record")
        elif kind in ("iterrows", "groupby") and isinstance(t, ast.Tuple) and len(t.elts) == 2 \
                and isinstance(t.elts[1], ast.Name):
            self.bind(t.elts[0], None)
            self.alias[t.elts[1].id] = (p, "row" if kind == "iterrows" else "table")
        else:
            self.bind(t, None)

    # -------------------------------------------------------------- expressions

    def ev(self, e: ast.AST | None) -> tuple[str, str] | None:
        """Notes the columns an expression reads; gives what it is when it is (part of) a parameter's table."""
        if e is None:
            return None
        if isinstance(e, ast.Name):
            return self.alias.get(e.id)
        if isinstance(e, ast.Attribute):
            o = self.ev(e.value)
            if o is None:
                return None
            p, kind = o
            if kind == "table":
                if e.attr in ("loc", "iloc"):
                    return (p, "indexer:" + e.attr)
                if e.attr not in _attrs("table"):
                    self.read(p, e.attr)
            elif kind == "row" and e.attr not in _attrs("row"):
                self.read(p, e.attr)
            return None
        if isinstance(e, ast.Subscript):
            return self.subscript(e)
        if isinstance(e, ast.Call):
            return self.call(e)
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            saved = dict(self.alias)
            for g in e.generators:
                self.loop(g.target, self.ev(g.iter))
                for c in g.ifs:
                    self.ev(c)
            if isinstance(e, ast.DictComp):
                self.ev(e.key)
                self.ev(e.value)
            else:
                self.ev(e.elt)
            self.alias = saved
            return None
        if isinstance(e, ast.Lambda):
            return None
        for child in ast.iter_child_nodes(e):
            if isinstance(child, ast.expr):
                self.ev(child)
            elif isinstance(child, ast.keyword):
                self.ev(child.value)
        return None

    def subscript(self, e: ast.Subscript) -> tuple[str, str] | None:
        o = self.ev(e.value)
        s = e.slice
        if o is None:
            self.ev(s)
            return None
        p, kind = o
        cols = _strings(s)
        if kind == "table":
            if cols is not None:
                for c in cols:
                    self.read(p, c)
                return (p, "table") if isinstance(s, ast.List) else None
            self.ev(s)
            if self.condition(s):
                return (p, "table")  # rows picked by a condition: the same columns
            # `table[column]` with a column name held in a variable: one column, whose name isn't known here (the
            # call gives it); `.str`, `.dt`, `.astype(...)` on it are never columns
            return (p, "column")
        if kind in ("row", "record"):
            if cols is not None and isinstance(s, ast.Constant):
                self.read(p, cols[0])
            else:
                self.ev(s)
            return None
        if kind.startswith("indexer"):
            if isinstance(s, ast.Tuple) and len(s.elts) == 2:
                self.ev(s.elts[0])
                c = _strings(s.elts[1])
                for x in c or []:
                    self.read(p, x)
                if c is None:
                    self.ev(s.elts[1])
                return (p, "table") if isinstance(s.elts[1], ast.List) else None
            self.ev(s)
            if kind == "indexer:iloc" and isinstance(s, (ast.Constant, ast.UnaryOp)):
                return (p, "row")
            return (p, "table")
        if kind == "groupby":
            for c in cols or []:
                self.read(p, c)
            return None
        self.ev(s)
        return None

    def condition(self, s: ast.AST) -> bool:
        """Whether a subscript picks rows (a comparison, a mask kept under a name, `.isin(...)`, a slice) rather than
        a column whose name is in a variable."""
        if isinstance(s, (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.Slice)):
            return True
        if isinstance(s, ast.BinOp):
            return isinstance(s.op, (ast.BitAnd, ast.BitOr, ast.BitXor))
        if isinstance(s, ast.Call):
            return isinstance(s.func, ast.Attribute) and s.func.attr in MASK_METHODS
        if isinstance(s, ast.Name):
            return s.id in self.masks
        if isinstance(s, (ast.Attribute, ast.Subscript)):  # a column of a table used as a mask: `group[group.used]`
            root = _root(s)
            return isinstance(root, ast.Name) and (self.alias.get(root.id) or ("", ""))[1] in ("table", "row")
        return False

    def call(self, e: ast.Call) -> tuple[str, str] | None:
        f = e.func
        if isinstance(f, ast.Attribute):
            o = self.ev(f.value)
            if o is not None and o[1] == "table":
                return self.table_method(o[0], f.attr, e)
            self.args(e)
            return None
        if isinstance(f, ast.Name):
            origins = [self.ev(a) for a in e.args]
            kws = {k.arg: self.ev(k.value) for k in e.keywords}
            sub =self.reader.of(f.id) if any(origins) or any(kws.values()) else None
            if sub:
                names = self.reader.params(f.id) or []
                pairs = list(zip(names, origins)) + [(k, o) for k, o in kws.items() if k]
                for name, o in pairs:
                    if o is not None and o[1] in ("table", "row", "record") and name in sub:
                        for c in sub[name][0]:
                            self.read(o[0], c)
            return None
        self.ev(f)
        self.args(e)
        return None

    def args(self, e: ast.Call) -> None:
        for a in e.args:
            self.ev(a)
        for k in e.keywords:
            self.ev(k.value)

    def table_method(self, p: str, m: str, e: ast.Call) -> tuple[str, str] | None:
        kw = {k.arg: k.value for k in e.keywords if k.arg}
        if m in COLUMN_ARGS:
            name, pos = COLUMN_ARGS[m]
            node = kw.get(name) if name in kw else (e.args[pos] if pos is not None and len(e.args) > pos else None)
            for c in _strings(node) or []:
                self.read(p, c)
        if m == "merge":
            for c in _strings(kw.get("on")) or _strings(kw.get("left_on")) or []:
                self.read(p, c)
            for a in e.args:
                o = self.ev(a)
                if o is not None and o[1] == "table":
                    for c in _strings(kw.get("on")) or _strings(kw.get("right_on")) or []:
                        self.read(o[0], c)
            return None
        if m == "rename":
            cols = kw.get("columns")
            if isinstance(cols, ast.Dict):
                for v in cols.values:
                    for c in _strings(v) or []:
                        self.made[p].add(c)
        if m == "assign":
            for k in kw:
                self.made[p].add(k)
        self.args(e)
        if m == "groupby":
            return (p, "groupby")
        if m == "to_dict":
            orient = kw.get("orient") or (e.args[0] if e.args else None)
            return (p, "records") if _strings(orient) == ["records"] else None
        if m == "iterrows":
            return (p, "iterrows")
        if m in SAME_COLUMNS:
            return (p, "table")
        return None


# ---------------------------------------------------------------------------------------------------- types


def _type_of_annotation(ann: str | None) -> str | None:
    if not ann:
        return None
    s = ann.replace("typing.", "")
    low = s.lower()
    if "flag" in low:
        return "flags"
    if "dataframe" in low or re.search(r"\bTable\b", s):
        return "table"
    if "figure" in low:
        return "figure"
    if re.match(r"^(float|int|complex|Number|np\.float64)\b", s):
        return "number"
    if re.match(r"^(str|Path)\b", s):
        return "text"
    if low in ("none", "bool"):
        return "data"
    return "data"


def type_of_value(v: Any) -> str | None:
    """A recorded value summary's type: table, flags, figure, data, number or text."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "data"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, dict):
        kind = v.get("kind")
        if kind == "table":
            cols = set(v.get("columns") or [])
            return "flags" if {"level", "message"} <= cols else "table"
        if kind == "figure":
            return "figure"
        if kind == "number":
            return "number"
        if kind in ("list", "dict", "record", "array", "column", "object", "bytes", "value"):
            return "data"
    return None


def _static_type(A: _Analysis, name: str, depth: int = 0) -> str | None:
    """A guess without a run: what a function returns, from its annotation or the last thing it returns."""
    c = A.callee(name)
    t = _type_of_annotation(c.returns)
    if name in FLAGS_MAKERS:
        return "flags"
    if t or c.function is None or depth > 3:
        if t is None and c.sentence and re.search(r"\b(figure|plot|chart)\b", c.sentence, re.IGNORECASE):
            return "figure"
        return t
    node = c.function.node
    assigns: dict[str, ast.AST] = {}
    rets: list[ast.AST] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            assigns.setdefault(n.targets[0].id, n.value)
        elif isinstance(n, ast.Return) and n.value is not None:
            rets.append(n.value)

    def expr_type(e: ast.AST, hops: int = 0) -> str | None:
        if isinstance(e, ast.Name) and e.id in assigns and hops < 3:
            return expr_type(assigns[e.id], hops + 1)
        if isinstance(e, ast.Call):
            if isinstance(e.func, ast.Name):
                if e.func.id in ("float", "int", "round", "len", "abs", "sum"):
                    return "number"
                if e.func.id in ("str",):
                    return "text"
                if e.func.id in ("list", "dict", "tuple", "set", "sorted"):
                    return "data"
                return _static_type(A, e.func.id, depth + 1)
            if isinstance(e.func, ast.Attribute):
                base = e.func.value
                if isinstance(base, ast.Name) and base.id in ("pd", "pandas") and e.func.attr in ("DataFrame",
                                                                                                  "concat", "merge"):
                    return "table"
                if e.func.attr in ("merge", "copy", "sort_values", "reset_index", "drop_duplicates", "pivot_table",
                                   "rename", "assign", "dropna", "reindex"):
                    return "table"
        if isinstance(e, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.ListComp, ast.DictComp)):
            return "data"
        if isinstance(e, ast.Constant):
            return "text" if isinstance(e.value, str) else "number" if isinstance(e.value, (int, float)) else None
        if isinstance(e, ast.BinOp):
            return "number" if methodfile.is_arithmetic(e) else None
        return None

    types = {expr_type(r) for r in rets} - {None}
    if len(types) == 1:
        return types.pop()
    if c.function.formula_shaped:
        return "number"
    return None


_NO_VALUE = object()


@functools.lru_cache(maxsize=1)
def _palette() -> dict[str, dict[str, Any]]:
    """The ready-made steps by function name (toolbox.blocks.blocks()); {} when the toolbox can't say."""
    try:
        from toolbox.blocks import blocks

        return {b["function"]: b for b in blocks()}
    except Exception:  # the toolbox is the plugin's own code; without its palette no call-site columns are known
        return {}


def _port_value(port: dict[str, Any], setting_values: dict[str, Any]) -> Any:
    """What a port gets at this call when it can be known without running: a value written in place (or its
    default), or a setting's value. _NO_VALUE otherwise (a wire, an input, code that isn't a plain value)."""
    if port["source"] == "setting":
        return setting_values.get(port["setting"], _NO_VALUE)
    if port["source"] in ("value", "default") and port.get("code") is not None:
        try:
            return ast.literal_eval(port["code"])
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return _NO_VALUE
    return _NO_VALUE


def _call_columns(A: _Analysis, n: _Node, ports: list[dict[str, Any]], setting_values: dict[str, Any]
                  ) -> dict[str, list[str]] | None:
    """For a ready-made step: the columns each table input needs at this call, from the values of its column fields
    (`pick_rows(ratios, "role", "sample")`: {"table": ["role"]}); a formula field adds the formula's parameters as
    columns of the step's first table. None for any other function."""
    block = _palette().get(n.fn) if n.callee is not None and n.callee.kind == "toolbox" else None
    if block is None:
        return None
    by_name = {p["name"]: p for p in ports}
    tables = [f["name"] for f in block["fields"] if f["kind"] == "table"]
    needs: dict[str, list[str]] = {t: [] for t in tables}

    def add(where: list[str], cols: list[str]) -> None:
        for t in where:
            for c in cols:
                if c not in needs.setdefault(t, []):
                    needs[t].append(c)

    for f in block["fields"]:
        port = by_name.get(f["name"])
        if port is None:
            continue
        if f["kind"] in COLUMN_FIELDS and f.get("of"):
            v = _port_value(port, setting_values)
            cols = [v] if isinstance(v, str) else [c for c in v if isinstance(c, str)] \
                if isinstance(v, (list, tuple)) else []
            add(list(f["of"]), cols)
        elif f["kind"] == "formula" and tables and port["source"] == "value":
            fn = A.method.function((port.get("code") or "").strip())
            if fn is not None:
                add(tables[:1], [p.name for p in _fn_params(fn.node) if p.kind == "pos" and p.default is None])
    return needs


def _sources_text(chosen: list[dict[str, Any]]) -> str | None:
    """A result input's value as its box shows it: "Survey in spring · scores", "3 experiments · scores"."""
    if not chosen:
        return None
    results = ", ".join(dict.fromkeys(str(s.get("result")) for s in chosen))
    if len(chosen) == 1:
        return f"{chosen[0].get('title') or chosen[0].get('experiment')} · {results}"
    return f"{len(chosen)} experiments · {results}"


def _setting_type(setting: dict[str, Any]) -> str:
    return {"number": "number", "integer": "number", "text": "text"}.get(setting.get("type"), "data")


# ---------------------------------------------------------------------------------------------------- the graph


def graph(method: str | methodfile.Method | dict, run: dict | None = None, experiment: dict | None = None
          ) -> dict[str, Any]:
    """The recipe as a flow: nodes (one per recipe statement, in order), their ports, edges, the inputs, settings,
    results, groups and the quality lane; with a run, each node's status, time, error and values.

    `method` is the method's source (or its parsed `methodfile.Method`, or a dict with `source`). A run is a run dict;
    give it `source` (the code the run ran, its `method.py`) so a changed method can be matched: when the sources
    differ, nodes are matched by their statement's code, nodes the run doesn't have are `not_run`, and the graph says
    `stale: true`. Give it `lines` too (the recipe call's statements from the run's inside.json, see
    `flow_tools.run_lines`) and every wire gets its value, also from a code box, a class call or a remembered step."""
    A = _analyze(method)
    columns = _Columns(A.method)
    settings = methodfile.settings(A.method)
    by_setting = {s["id"]: s for s in settings}
    nodes: list[dict[str, Any]] = []
    quality = _quality(A)
    results = {}
    for o in A.outputs:
        if o["from"] and o["from"] not in results:
            results[o["from"]] = {"id": o["id"], "label": o["label"]}
    for n in A.nodes:
        group = A.group_of(n)
        c = n.callee
        node = {"id": n.id, "kind": n.kind, "fn": n.fn, "fn_kind": c.kind if c else None,
                "title": A.title(n), "sentence": c.sentence if c else "", "line": n.line, "end_line": n.end_line,
                "code": A.code(n), "outs": list(n.outs), "ports": n.ports,
                "opens_as": c.opens_as if c else None, "group": group["id"] if group else None,
                "quality": n.id in quality, "result": results.get(n.id), "types": {},
                "status": None, "ms": None, "error": None, "values": {}}
        if n.kind == "call" and n.outs:
            t = _static_type(A, n.fn)
            node["types"] = {o: t if len(n.outs) == 1 else None for o in n.outs}
            node["_annotated"] = _type_of_annotation(c.returns) is not None and len(n.outs) == 1
        else:
            node["types"] = {o: None for o in n.outs}
        nodes.append(node)
    by_id = {nd["id"]: nd for nd in nodes}
    run_info = None
    stale = False
    if run is not None:
        stale, run_info = _attach_run(A, nodes, run)
    # types: annotation, else the run, else the guess; a table that carries level and message is flags
    for nd in nodes:
        annotated = nd.pop("_annotated", False)
        for o in nd["outs"]:
            seen = type_of_value(nd["values"].get(o))
            t = nd["types"].get(o)
            if seen and (not annotated or (t == "table" and seen == "flags")):
                nd["types"][o] = seen
    exp_values = (experiment or {}).get("params") or {}
    run_values = (run or {}).get("settings") or (run or {}).get("params") or {}
    setting_values = {s["id"]: exp_values.get(s["id"], run_values.get(s["id"], s.get("default"))) for s in settings}
    # the table each result input delivered in this run (lab/sources.py): its stacked table, as on any wire
    delivered: dict[str, Any] = {}
    for name, rec in ((run or {}).get("sources") or {}).items():
        if name in A.results and isinstance(rec, dict):
            delivered[name] = {"kind": "table", "rows": rec.get("rows"), "columns": rec.get("columns") or [],
                               "file": rec.get("file")}
    for n, nd in zip(A.nodes, nodes):
        params = {p.name: p for p in n.callee.params or []} if n.callee else {}
        at_call = _call_columns(A, n, nd["ports"], setting_values) if n.kind == "call" else None
        for port in nd["ports"]:
            src = port["source"]
            param = params.get(port["name"])
            t = _type_of_annotation(param.ann) if param is not None else None
            result_input = src == "input" and port["input"] in A.results
            if src == "wire":
                made = by_id[port["from"]]["types"].get(port["wire"])
                t = made if (t is None or (t == "table" and made == "flags")) and made else t
            elif src == "setting":
                t = t or (_setting_type(by_setting[port["setting"]]) if port["setting"] in by_setting else None)
            elif src == "input":
                t = t or ("data" if port["input"] == "corrections" else "table" if result_input else "text")
            seen = port.pop("_seen", None)
            if t is None and seen is not None:
                t = type_of_value(seen)
            port["type"] = t
            if result_input and run is not None:
                if port["input"] not in delivered and isinstance(seen, dict) and seen.get("kind") == "table":
                    delivered[port["input"]] = seen
                port["value"] = delivered.get(port["input"])
            if (src == "wire" or result_input) and n.kind == "call" and t in (None, "table", "flags"):
                if at_call is not None:  # a ready-made step: the columns named at this call
                    read, made_cols = at_call.get(port["name"], []), set()
                else:
                    cols = columns.of(n.fn)
                    read, made_cols = cols.get(port["name"], ([], set())) if cols else ([], set())
                port["columns_read"] = list(read)
                value = by_id[port["from"]]["values"].get(port["wire"]) if src == "wire" \
                    else delivered.get(port["input"])
                if read and isinstance(value, dict) and value.get("kind") == "table":
                    have = set(value.get("columns") or [])
                    port["missing"] = [x for x in read if x not in have and x not in made_cols]
    edges = []
    for n, nd in zip(A.nodes, nodes):
        for port in nd["ports"]:
            names = [port["wire"]] if port["source"] == "wire" else port["reads"] if port["source"] == "value" else []
            for name in names:
                frm = n.deps.get(name)
                if frm is not None:
                    edges.append({"from": A.nodes[frm].id, "to": n.id, "wire": name, "port": port["name"]})
    for o in A.outputs:
        if o["from"]:
            o["type"] = by_id[o["from"]]["types"].get(o["wire"])
            if run is not None and o["from"] in by_id:
                o["value"] = by_id[o["from"]]["values"].get(o["wire"])
                saved = (run.get("outputs") or {}).get(o["id"]) or {}
                if isinstance(o["value"], dict) and o["value"].get("kind") == "figure" and not o["value"].get("file") \
                        and saved.get("kind") == "figure" and saved.get("file"):
                    o["value"] = {**o["value"], "file": saved["file"]}  # a run from before figures were kept
    used_by: dict[str, list[str]] = {}
    for n in A.nodes:
        for name, frm in n.deps.items():
            if frm is None:
                used_by.setdefault(name, [])
                if n.id not in used_by[name]:
                    used_by[name].append(n.id)
    exp_params = (experiment or {}).get("params") or {}
    run_params = (run or {}).get("settings") or (run or {}).get("params") or {}
    for s in settings:
        if s["id"] in exp_params:
            s["value"], s["set"] = exp_params[s["id"]], True
        elif experiment is None and s["id"] in run_params:
            s["value"], s["set"] = run_params[s["id"]], True
        else:
            s["value"], s["set"] = s.get("default"), False
        s["used_by"] = used_by.get(s["id"], [])
    file_inputs = {i["id"]: i for i in methodfile.inputs(A.method)}
    exp_inputs = (experiment or {}).get("inputs") or {}
    inputs = []
    for p in A.inputs:
        if p == "corrections":
            item = {"id": p, "kind": "corrections", "label": A.fn.doc.inputs.get(p) or "Corrections", "types": [],
                    "optional": False}
            if experiment is not None:
                item["value"] = sum(1 for x in experiment.get("corrections") or [] if x.get("status") == "approved")
        elif p in A.results:
            item = {**file_inputs[p], "types": []}
            if experiment is not None:
                item["value"] = _sources_text(((experiment.get("sources") or {}).get(p)) or [])
                item["sources"] = list(((experiment.get("sources") or {}).get(p)) or [])
            if run is not None:
                item["table"] = delivered.get(p)
                item["read"] = ((run.get("sources") or {}).get(p) or {}).get("from") or []
        else:
            item = {**file_inputs.get(p, {"id": p, "kind": "file", "label": _humanize(p), "types": [],
                                          "optional": False})}
            f = exp_inputs.get(p)
            if isinstance(f, dict):
                item["value"] = f.get("name")
        item["used_by"] = used_by.get(p, [])
        inputs.append(item)
    return {"method": {"name": A.method.name, "recipe": A.fn.name, "line": A.fn.line, "end_line": A.fn.end_line},
            "nodes": nodes, "edges": edges, "inputs": inputs, "settings": settings, "outputs": A.outputs,
            "groups": A.groups, "quality": [n.id for n in A.nodes if n.id in quality], "messages": A.messages,
            "run": run_info, "stale": stale}


def _quality(A: _Analysis) -> set[str]:
    """The nodes whose results reach nothing but the `flags` result (found from the wiring alone)."""
    if not any(o["id"] == QUALITY_OUTPUT for o in A.outputs):
        return set()
    ends: list[set[str]] = [set() for _ in A.nodes]
    for o in A.outputs:
        if o["from"]:
            idx = next(n.index for n in A.nodes if n.id == o["from"])
            ends[idx].add(o["id"])
    users: list[set[int]] = [set() for _ in A.nodes]
    for n in A.nodes:
        for frm in n.deps.values():
            if frm is not None:
                users[frm].add(n.index)
    for n in reversed(A.nodes):
        for u in users[n.index]:
            ends[n.index] |= ends[u]
    return {n.id for n in A.nodes if ends[n.index] == {QUALITY_OUTPUT}}


# ---------------------------------------------------------------------------------------------------- runs


def _match_steps(A: _Analysis, steps: list[dict]) -> dict[int, list[tuple[dict, bool]]]:
    """Which recorded steps (calls made by the recipe, in order) belong to which node: {node index: [(step, outer)]}.
    `outer` is the node's own call; the others were made inside it (a nested call, a call in a code box)."""
    out: dict[int, list[tuple[dict, bool]]] = {}
    done: set[int] = set()
    at = 0
    for s in steps:
        fn = s.get("function")
        for node in A.nodes[at:]:
            outer = node.kind == "call" and node.fn == fn and node.index not in done
            if outer or fn in node.calls:
                out.setdefault(node.index, []).append((s, outer))
                if outer:
                    done.add(node.index)
                at = node.index
                break
    return out


def _attach_run(A: _Analysis, nodes: list[dict], run: dict) -> tuple[bool, dict]:
    source = run.get("source")
    stale = bool(source) and source != A.src.text
    R = A
    if stale:
        try:
            R = _analyze(source)
        except LabError:
            R = None
    steps = run.get("steps") or []
    status = run.get("status")
    error = run.get("error") or {}
    info = {"id": run.get("id"), "status": status, "started": run.get("started"), "ms": run.get("ms"),
            "error": error.get("message"), "stale": stale}
    if R is None:
        for nd in nodes:
            nd["status"] = "not_run"
        return stale, info
    matched = _match_steps(R, steps)
    # the run's own nodes: status, time, error, values
    rnodes: list[dict[str, Any]] = []
    failed_at = None
    for n in R.nodes:
        got = matched.get(n.index, [])
        own = next((s for s, outer in got if outer), None)
        st = {"status": None, "ms": None, "error": None, "values": {}, "inputs": {}, "seen": bool(got)}
        if got:
            failed = [s for s, _ in got if s.get("status") == "failed" or s.get("error")]
            if failed:
                st["status"] = "failed"
                st["error"] = error.get("message") or failed[0].get("error")
                failed_at = n.index if failed_at is None else failed_at
            elif own is not None and (own.get("cached") or own.get("status") == "cached"):
                st["status"] = "cached"
            else:
                st["status"] = "done"
            st["ms"] = own.get("ms") if own is not None else round(sum(s.get("ms") or 0 for s, _ in got), 2)
            if own is not None:
                st["inputs"] = own.get("inputs") or {}
                out = own.get("output")
                if len(n.outs) == 1:
                    st["values"][n.outs[0]] = out
                elif len(n.outs) > 1 and isinstance(out, dict) and isinstance(out.get("items"), list):
                    for name, item in zip(n.outs, out["items"]):
                        st["values"][name] = item
        rnodes.append(st)
    if failed_at is None and status in ("failed", "stopped") and error.get("line"):
        line = error["line"]
        failed_at = next((n.index for n in R.nodes if n.line <= line <= n.end_line), None)
        if failed_at is not None:
            rnodes[failed_at]["status"] = "failed"
            rnodes[failed_at]["error"] = error.get("message")
    last_seen = max((i for i, st in enumerate(rnodes) if st["seen"]), default=-1)
    for i, st in enumerate(rnodes):
        if st["status"] is not None:
            continue
        if status == "done":
            st["status"] = "done"
        elif failed_at is not None:
            st["status"] = "done" if i < failed_at else "not_run"
        else:
            st["status"] = "done" if i < last_seen else "not_run"
    # a value no step recorded (a class call, a code box): the next step that took it as an input recorded it
    for n in R.nodes:
        for p in n.ports:
            if p["source"] == "wire":
                frm = n.deps.get(p["wire"])
                seen = rnodes[n.index]["inputs"].get(p["name"])
                if frm is not None and seen is not None and p["wire"] not in rnodes[frm]["values"]:
                    rnodes[frm]["values"][p["wire"]] = seen
    # the recipe's own statements as recorded inside it (inside.json): every name a node made, also for a code box,
    # a class call and a remembered step; the step's own record above wins
    inside: dict[int, dict[str, Any]] = {}
    for rec in sorted(run.get("lines") or [], key=lambda r: r.get("line") or 0):  # later lines give the last value
        rounds = rec.get("rounds") or []
        line = rec.get("line") or 0
        n = next((n for n in R.nodes if n.line <= line <= n.end_line), None) if rounds else None
        if n is not None:
            got = rounds[-1].get("values") or {}
            inside.setdefault(n.index, {}).update({k: v for k, v in got.items() if k in n.outs})
    for i, got in inside.items():
        for name, v in got.items():
            rnodes[i]["values"].setdefault(name, v)
    # the current nodes: the same node, or the run's node with the same statement
    if stale:
        free: dict[str, list[int]] = {}
        for n in R.nodes:
            free.setdefault(ast.dump(n.stmt), []).append(n.index)
        pairs = {}
        for n in A.nodes:
            cands = free.get(ast.dump(n.stmt))
            if cands:
                pairs[n.index] = cands.pop(0)
    else:
        pairs = {n.index: n.index for n in A.nodes}
    for n, nd in zip(A.nodes, nodes):
        j = pairs.get(n.index)
        if j is None:
            nd["status"] = "not_run"
            continue
        st = rnodes[j]
        nd.update(status=st["status"], ms=st["ms"], error=st["error"], values=st["values"])
        for p in nd["ports"]:
            if p["name"] in st["inputs"]:
                p["_seen"] = st["inputs"][p["name"]]
    return stale, info


# ---------------------------------------------------------------------------------------------------- editing


def edit(source: str, op: dict[str, Any]) -> str:
    """The method's new source after one change made in the flow. Raises LabError, in plain words, for a change
    that would break the method."""
    if not isinstance(op, dict) or not op.get("op"):
        raise LabError("A change needs an `op`: " + ", ".join(OPS) + ".")
    handler = _HANDLERS.get(op["op"])
    if handler is None:
        raise LabError(f"There is no change called `{op['op']}`. Changes: {', '.join(OPS)}.")
    A = _analyze(source)
    patches = handler(A, op)
    new = _apply(A.src, patches)
    try:
        ast.parse(new)
    except SyntaxError as e:
        raise LabError(f"This change would leave code Python can't read (line {e.lineno}): {e.msg}.") from e
    return new


def _need(op: dict, key: str, what: str) -> Any:
    v = op.get(key)
    if v is None or v == "":
        raise LabError(f"`{op['op']}` needs `{key}`: {what}.")
    return v


def _check_name(A: _Analysis, name: Any, what: str = "name") -> str:
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise LabError(f"`{name}` can't be a {what}: use letters, digits and _, not starting with a digit "
                       "(e.g. `growth_rates`).")
    why = None
    if name in A.params:
        why = "a setting or input of the recipe"
    elif name in A.functions or name in A.classes:
        why = "a function of this method"
    elif name in A.toolbox_imported:
        why = "a toolbox function"
    elif name in A.module_names:
        why = "a name defined at the top of the file"
    elif any(name in _assigned(st) for st in A.stmts) or any(name in _loads(st) for st in A.stmts):
        why = "a wire in the recipe"
    elif name in A.made:
        why = "a wire in the recipe"
    elif hasattr(builtins, name):
        why = "a name Python itself uses"
    elif _toolbox(name) is not None:
        why = "a toolbox function"
    if why:
        raise LabError(f"The name `{name}` is taken ({why}): pick another.")
    return name


def _titles(A: _Analysis, nodes: list[_Node]) -> str:
    names = list(dict.fromkeys(A.title(n) for n in nodes))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _first_line(A: _Analysis) -> int:
    """Where a new statement goes when it needs nothing made in the recipe: before the first statement."""
    if A.stmts:
        return A.stmts[0].lineno
    if A.ret is not None:
        return A.ret.lineno
    if A.doc is not None:
        return (A.doc.end_lineno or A.doc.lineno) + 1
    return A.node.body[0].lineno


def _insert_after(A: _Analysis, index: int | None) -> int:
    """The line a new statement is inserted before, to come after node `index` (None: first)."""
    return A.nodes[index].end_line + 1 if index is not None else _first_line(A)


# -------------------------------------------------------------- arguments


def _arg_text(A: _Analysis, spec: Any, before: int | None = None) -> tuple[str, str | None]:
    """An argument written from a flow's description: a wire name, {wire}, {setting}, {input}, {value} (a plain
    value) or {code} (Python). Gives (text, the wire it reads or None)."""
    if isinstance(spec, str):
        spec = {"function": spec} if spec in A.functions and spec not in A.made else {"wire": spec}
    if not isinstance(spec, dict) or len(spec) != 1:
        raise LabError("An argument is a wire name, or one of {\"wire\": name}, {\"setting\": name}, "
                       "{\"input\": name}, {\"function\": name}, {\"value\": plain value}, {\"code\": Python}.")
    (kind, v), = spec.items()
    if kind == "function":
        if v not in A.functions or v == A.fn.name:
            raise LabError(f"There is no function `{v}` in this method: make it first (new_function).")
        return v, None
    if kind == "wire":
        if v in A.settings or v in A.inputs:
            return v, None
        if A.producer(v, before) is None and A.producer(v) is None:
            known = ", ".join(sorted(A.made)) or "none yet"
            raise LabError(f"There is no wire `{v}` in the recipe. Wires: {known}.")
        return v, v
    if kind == "setting":
        if v not in A.settings:
            raise LabError(f"There is no setting `{v}`: add it first (add_setting).")
        return v, None
    if kind == "input":
        if v not in A.inputs:
            raise LabError(f"There is no input `{v}`: add it first (add_input).")
        return v, None
    if kind == "value":
        return _py(v), None
    if kind == "code":
        if not isinstance(v, str):
            raise LabError("`code` is Python text, e.g. \"2 * rate_window_min\".")
        try:
            ast.parse(v.strip(), mode="eval")
        except SyntaxError as e:
            raise LabError(f"Python can't read `{v}` as a value: {e.msg}.") from e
        return v.strip(), None
    raise LabError(f"An argument can't be `{kind}`: use wire, setting, input, value or code.")


def _stmt_text_lines(A: _Analysis, st: ast.stmt) -> list[str]:
    return A.src.lines[st.lineno - 1:(st.end_lineno or st.lineno)]


def _alone_on_lines(A: _Analysis, st: ast.stmt) -> bool:
    """The statement has its lines to itself (nothing before it on its first line, nothing after on its last)."""
    first, last = A.src.line(st.lineno), A.src.line(st.end_lineno or st.lineno)
    c0, c1 = A.src.col(st.lineno, st.col_offset), A.src.col(st.end_lineno or st.lineno, st.end_col_offset or 0)
    return not first[:c0].strip() and not last[c1:].strip()


def _spans_applied(A: _Analysis, st: ast.stmt, spans: list[tuple[int, int, int, int, str]]) -> list[str]:
    """The statement's lines with text spans replaced (absolute lines, character columns)."""
    first = st.lineno
    lines = _stmt_text_lines(A, st)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    text = "\n".join(lines)
    for l0, c0, l1, c1, new in sorted(spans, key=lambda s: (s[0], s[1]), reverse=True):
        a = starts[l0 - first] + c0
        b = starts[l1 - first] + c1
        text = text[:a] + new + text[b:]
    return text.split("\n")


def _layout(lines: list[str], indent: str) -> tuple[str, list[tuple[str, int]], str] | None:
    """A call statement (or the return of a dict) as (head, its items with the line each is on, tail); None when it
    can't be written again without losing something (a comment, an argument written over several lines)."""
    text = "\n".join(lines)
    if _has_comment(text):
        return None
    body = textwrap.dedent(text)
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return None
    if len(tree.body) != 1:
        return None
    st = tree.body[0]
    s = _Src(body)
    parts = _call_parts(st)
    if parts is not None:
        _, target, call = parts
        args = list(call.args) + list(call.keywords)
        items = [(s.seg(a), a.lineno - 1) for a in args]
        head = indent + (s.seg(target) + " = " if target is not None else "") + s.seg(call.func) + "("
        if isinstance(st, ast.AnnAssign):
            head = indent + s.seg(st.target) + ": " + s.seg(st.annotation) + " = " + s.seg(call.func) + "("
        tail = ")"
    elif isinstance(st, ast.Return) and isinstance(st.value, ast.Dict) and None not in st.value.keys:
        items = [(f"{s.seg(k)}: {s.seg(v)}", k.lineno - 1) for k, v in zip(st.value.keys, st.value.values)]
        head, tail = indent + "return {", "}"
    else:
        return None
    if any("\n" in t for t, _ in items):
        return None
    return head, items, tail


def _reflow(lines: list[str], indent: str, old_lines: list[str] | None = None) -> list[str]:
    """A changed call statement (or return) wrapped at 120 columns, keeping the old statement's lines where nothing
    changed."""
    new = _layout(lines, indent)
    if new is None:
        return lines
    head, items, tail = new
    old = _layout(old_lines, indent) if old_lines else None
    if old is not None and old[0] == head and old[2] == tail:
        return _relayout(old_lines, old[1], [t for t, _ in items], head, tail)
    return _wrap(head, [t for t, _ in items], tail)


def _statement_patch(A: _Analysis, st: ast.stmt, spans: list[tuple[int, int, int, int, str]]
                     ) -> tuple[int, int, list[str]]:
    new = _spans_applied(A, st, spans)
    if _alone_on_lines(A, st):
        new = _reflow(new, _lead(A.src.line(st.lineno)), _stmt_text_lines(A, st))
    return st.lineno, st.end_lineno or st.lineno, new


def _render_call(indent: str, outs: list[str], fn: str, items: list[str]) -> list[str]:
    head = indent + (", ".join(outs) + " = " if outs else "") + fn + "("
    return _wrap(head, items, ")")


# -------------------------------------------------------------- the toolbox import


def _is_toolbox(module: str | None) -> bool:
    """`toolbox` or one of its add-ons (`toolbox.<add-on>`)."""
    return bool(module) and (module == "toolbox" or module.startswith("toolbox."))


def _import_patch(A: _Analysis, add: list[str] = (), remove: list[str] = ()) -> list[tuple[int, int, list[str]]]:
    """Names added to (or removed from) the import line of the module each comes from: `from toolbox import` for
    the toolbox's own, `from toolbox.<add-on> import` for an add-on's (a new line when the method has none yet)."""
    by_module: dict[str, tuple[list[str], list[str]]] = {}
    for name in add:
        c = _toolbox(name)
        by_module.setdefault((c.module if c else None) or "toolbox", ([], []))[0].append(name)
    for name in remove:
        by_module.setdefault(A.toolbox_module.get(name, "toolbox"), ([], []))[1].append(name)
    patches = []
    for module, (adding, removing) in by_module.items():
        patches += _module_import_patch(A, module, adding, removing)
    return patches


def _module_import_patch(A: _Analysis, module: str, add: list[str], remove: list[str]
                         ) -> list[tuple[int, int, list[str]]]:
    head = f"from {module} import"
    imp = next((n for n in A.method.tree.body if isinstance(n, ast.ImportFrom) and n.module == module
                and not n.level), None)
    if imp is None:
        if not add:
            return []
        line = f"{head} {', '.join(sorted(add))}"
        new = [line] if len(line) <= WIDTH else _wrap(f"{head} (", sorted(add), ")")
        imports = [n for n in A.method.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        toolbox = [n for n in imports if isinstance(n, ast.ImportFrom) and _is_toolbox(n.module) and not n.level]
        if toolbox:  # an add-on's line goes right under the toolbox's own
            at = (toolbox[-1].end_lineno or toolbox[-1].lineno) + 1
            return [(at, at - 1, new)]
        if imports:
            at = (imports[-1].end_lineno or imports[-1].lineno) + 1
        else:
            first = A.method.tree.body[0] if A.method.tree.body else None
            at = (first.end_lineno or first.lineno) + 1 if first is not None and _is_doc(first) else 1
        return [(at, at - 1, ["", *new])]
    names = [A.src.seg(a) if a.asname else a.name for a in imp.names]
    plain = [a.name for a in imp.names]
    keep = sorted(plain) == plain
    for name in add:
        if name not in plain:
            if keep:
                i = next((k for k, x in enumerate(plain) if x > name), len(plain))
            else:
                i = len(plain)
            plain.insert(i, name)
            names.insert(i, name)
    for name in remove:
        if name in plain:
            i = plain.index(name)
            del plain[i], names[i]
    first, last = imp.lineno, imp.end_lineno or imp.lineno
    if not names:
        before = A.src.line(first - 1)
        after = A.src.line(last + 1)
        if first > 1 and not before.strip() and not after.strip():
            first -= 1
        return [(first, last, [])]
    one = f"{head} {', '.join(names)}"
    old_lines = A.src.lines[first - 1:last]
    if "(" in old_lines[0]:
        old = [(A.src.seg(a) if a.asname else a.name, a.lineno - first) for a in imp.names]
        new = _relayout(old_lines, old, names, f"{head} (", ")")
    else:
        new = [one] if len(one) <= WIDTH else _wrap(f"{head} (", names, ")")
    return [(first, last, _comments_kept(old_lines, new))]


def _uses_elsewhere(A: _Analysis, name: str, skip: ast.stmt) -> bool:
    for n in ast.walk(A.method.tree):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
            if not (skip.lineno <= n.lineno <= (skip.end_lineno or skip.lineno)):
                return True
    return False


# -------------------------------------------------------------- the signature and the docstring


def _signature_patch(A: _Analysis, params: list[str]) -> tuple[int, int, list[str]]:
    """The recipe's `def` line(s) written again with these parameter texts (`name`, `name=default`, `*`, ...)."""
    node = A.node
    first = node.lineno
    last = node.body[0].lineno - 1
    while last > first and (not A.src.line(last).strip() or A.src.line(last).strip().startswith("#")):
        last -= 1
    indent = _lead(A.src.line(first))
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    ret = " -> " + A.src.seg(node.returns) if node.returns is not None else ""
    head = f"{indent}{prefix}{node.name}("
    old = [(t, ln - first) for _, t, _, ln in _param_texts(A, lines=True)]
    old_lines = A.src.lines[first - 1:last]
    return first, last, _comments_kept(old_lines, _relayout(old_lines, old, params, head, f"){ret}:"))


def _param_texts(A: _Analysis, lines: bool = False) -> list[tuple]:
    """The recipe's parameters as (name, text, has a default[, line]); markers `*`, `/`, `*args`, `**kw` keep their
    place."""
    a = A.node.args
    out: list[tuple[str, str, bool, int]] = []
    pos = a.posonlyargs + a.args
    defaults: list[ast.AST | None] = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for i, (p, d) in enumerate(zip(pos, defaults)):
        text = A.src.seg(p) + ("=" + A.src.seg(d) if d is not None else "")
        if d is not None and p.annotation is not None:
            text = A.src.seg(p) + " = " + A.src.seg(d)
        out.append((p.arg, text, d is not None, p.lineno))
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            out.append(("/", "/", False, p.lineno))
    if a.vararg:
        out.append(("*" + a.vararg.arg, "*" + A.src.seg(a.vararg), False, a.vararg.lineno))
    elif a.kwonlyargs:
        out.append(("*", "*", False, a.kwonlyargs[0].lineno))
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        text = A.src.seg(p) + ("=" + A.src.seg(d) if d is not None else "")
        out.append((p.arg, text, d is not None, p.lineno))
    if a.kwarg:
        out.append(("**" + a.kwarg.arg, "**" + A.src.seg(a.kwarg), False, a.kwarg.lineno))
    return out if lines else [x[:3] for x in out]


def _insert_param(A: _Analysis, text: str, name: str, has_default: bool, first_place: bool = False
                  ) -> list[str]:
    """The parameter texts with a new one: one with a default after the last positional parameter; one without
    after the last positional parameter without a default (`first_place`: after the last input instead)."""
    params = _param_texts(A)
    end = next((i for i, (n, _, _) in enumerate(params) if n.startswith("*")), len(params))
    if has_default:
        at = end
    else:
        at = 0
        for i, (n, _, d) in enumerate(params[:end]):
            if n == "/":
                continue
            if first_place:
                if methodfile.is_input(n, A.results):
                    at = i + 1
            elif not d:
                at = i + 1
        if first_place and name.endswith("_file"):
            files = [i for i, (n, _, _) in enumerate(params[:end]) if n.endswith("_file")]
            at = files[-1] + 1 if files else 0
    params.insert(at, (name, text, has_default))
    return [t for _, t, _ in params]


class _Doc:
    """The recipe's docstring as lines, for adding and removing `Inputs:` / `Settings:` / `Returns:` entries."""

    def __init__(self, A: _Analysis):
        self.A = A
        self.base = A.indent
        if A.doc is not None:
            self.first, self.last = A.doc.lineno, A.doc.end_lineno or A.doc.lineno
            self.lines = A.src.lines[self.first - 1:self.last]
            if len(self.lines) == 1 or self.lines[-1].strip() not in ('"""', "'''"):
                q = '"""' if self.lines[-1].rstrip().endswith('"""') else "'''"
                self.lines[-1] = self.lines[-1].rstrip()[:-3]
                self.lines.append(self.base + q)
        else:
            self.first, self.last = A.node.body[0].lineno, A.node.body[0].lineno - 1
            self.lines = [f'{self.base}"""{_humanize(A.fn.name)}.', "", f'{self.base}"""']
        self.changed = False

    def patch(self) -> tuple[int, int, list[str]]:
        return self.first, self.last, self.lines

    def _indent(self, line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def section(self, name: str) -> tuple[int, int] | None:
        """(header index, end index exclusive, trailing blank lines left out)."""
        base = len(self.base)
        for i, line in enumerate(self.lines[1:-1], start=1):
            s = line.strip()
            if self._indent(line) == base and s.endswith(":") and _SECTION_NAMES.get(s[:-1].lower()) == name:
                end = i + 1
                while end < len(self.lines) - 1 and (not self.lines[end].strip()
                                                     or self._indent(self.lines[end]) > base):
                    end += 1
                while end > i + 1 and not self.lines[end - 1].strip():
                    end -= 1
                return i, end
        return None

    def ensure(self, name: str) -> tuple[int, int]:
        got = self.section(name)
        if got:
            return got
        order = ["Inputs", "Settings", "Returns"]
        at = len(self.lines) - 1
        for later in order[order.index(name) + 1:]:
            s = self.section(later)
            if s:
                at = s[0]
                while at > 1 and not self.lines[at - 1].strip():
                    at -= 1
                break
        else:
            while at > 1 and not self.lines[at - 1].strip():
                at -= 1
        self.lines[at:at] = ["", self.base + name + ":"]
        self.changed = True
        return at + 1, at + 2

    def entries(self, start: int, end: int, indent: int) -> list[tuple[str, int, int]]:
        """Entries at this indent between start and end, with their continuation lines: (name, first index, end
        index exclusive)."""
        out: list[tuple[str, int, int]] = []
        i = start
        while i < end:
            line = self.lines[i]
            if line.strip() and self._indent(line) == indent:
                j = i + 1
                while j < end and self.lines[j].strip() and self._indent(self.lines[j]) > indent:
                    j += 1
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?\s*:", line)
                out.append((m.group(1) if m else line.strip(), i, j))
                i = j
            else:
                i += 1
        return out

    def add(self, section: str, text: str, group: str | None = None) -> None:
        start, end = self.ensure(section)
        base = len(self.base)
        lines = self.lines[start + 1:end]
        groups = [i for i, ln in enumerate(lines, start=start + 1)
                  if self._indent(ln) == base + 4 and methodfile._is_group(textwrap.dedent(ln))]
        if section == "Settings" and groups:
            title = group or "Other settings"
            g = next((i for i in groups if self.lines[i].strip()[:-1].strip() == title), None)
            if g is None:
                self.lines[end:end] = [" " * (base + 4) + title + ":"]
                g, end = end, end + 1
            nxt = next((i for i in groups if i > g), end)
            at = nxt
            while at > g + 1 and not self.lines[at - 1].strip():
                at -= 1
            indent = base + 8
        elif section == "Settings" and group and not self.entries(start + 1, end, base + 4):
            self.lines[end:end] = [" " * (base + 4) + group + ":"]
            at, indent = end + 1, base + 8
        else:
            at, indent = end, base + 4
        self.lines[at:at] = _wrap_text(" " * indent + text, " " * (indent + 4))
        self.changed = True

    def remove(self, section: str, name: str) -> None:
        got = self.section(section)
        if not got:
            return
        start, end = got
        base = len(self.base)
        for indent in (base + 4, base + 8):
            for entry, a, b in self.entries(start + 1, end, indent):
                if entry == name:
                    del self.lines[a:b]
                    self.changed = True
                    self._tidy(section)
                    return

    def _tidy(self, section: str) -> None:
        """Drop a settings group left without entries, and a section left empty."""
        got = self.section(section)
        if not got:
            return
        start, end = got
        base = len(self.base)
        i = start + 1
        while i < end:
            line = self.lines[i]
            if self._indent(line) == base + 4 and methodfile._is_group(textwrap.dedent(line)):
                j = i + 1
                if j >= end or (self.lines[j].strip() and self._indent(self.lines[j]) <= base + 4):
                    del self.lines[i]
                    end -= 1
                    continue
            i += 1
        if not any(ln.strip() for ln in self.lines[start + 1:end]):
            a = start - 1 if start > 1 and not self.lines[start - 1].strip() else start
            del self.lines[a:end]


def _entry_text(name: str, label: str, help_: str = "", unit: str | None = None, kind: str | None = None) -> str:
    label = label.strip().rstrip(".")
    text = f"{name}{f' ({kind})' if kind else ''}: {label}{f' [{unit}]' if unit else ''}."
    return text + (f" {help_.strip()}" if help_ and help_.strip() else "")


# -------------------------------------------------------------- the operations


def _op_add_step(A: _Analysis, op: dict) -> list:
    fn = _need(op, "function", "the function to call, e.g. area_ratios")
    if fn == A.fn.name:
        raise LabError("The recipe can't call itself.")
    callee = A.callee(fn)
    if callee.kind == "unknown":
        raise LabError(f"There is no function `{fn}` in this method or the toolbox: make it first (new_function).")
    after = A.find(op["after"]).index if op.get("after") else None
    args = op.get("args") or {}
    positional: list[str] = []
    keywords: list[str] = []
    needs: list[int] = []
    missing: list[str] = []
    params = callee.params

    def text_of(spec: Any) -> str:
        text, wire = _arg_text(A, spec, after + 1 if after is not None else None)
        if wire is not None:
            p = A.producer(wire, after + 1 if after is not None else None)
            needs.append(p if p is not None else A.producer(wire))
        return text

    if isinstance(args, list):
        positional = [text_of(a) for a in args]
    elif params is None:
        if args:
            raise LabError(f"Python doesn't say which inputs `{fn}` takes: give the arguments as a list, in order.")
    else:
        unknown = [k for k in args if k not in {p.name for p in params}]
        if unknown and not any(p.kind == "varkw" for p in params):
            names = ", ".join(p.name for p in params if p.kind in ("pos", "kw")) or "none"
            raise LabError(f"`{fn}` has no input {', '.join(f'`{k}`' for k in unknown)}. Its inputs: {names}.")
        gap = False
        for p in params:
            if p.kind in ("var", "varkw"):
                continue
            spec = args.get(p.name)
            if spec is None and p.default is None:
                if p.name in A.made or p.name in A.params:
                    spec = p.name
                else:
                    missing.append(p.name)
                    gap = True
                    continue
            if spec is None:
                gap = True
                continue
            text = text_of(spec)
            if p.kind == "pos" and not gap and p.default is None:
                positional.append(text)
            else:
                keywords.append(f"{p.name}={text}")
        for k in unknown:
            keywords.append(f"{k}={text_of(args[k])}")
    if missing:
        raise LabError(f"{_humanize(fn)} needs {', '.join(f'`{m}`' for m in missing)}: connect "
                       f"{'it' if len(missing) == 1 else 'them'} to a wire, a setting or a value.")
    outs = op.get("outs") or ([op["out"]] if op.get("out") else None)
    if outs is None:
        outs = [_free_name(A, fn)]
    if isinstance(outs, str):
        outs = [outs]
    for o in outs:
        _check_name(A, o, "wire name")
    if len(set(outs)) != len(outs):
        raise LabError("Each output needs its own name.")
    last = max(needs) if needs else None
    if after is not None:
        if last is not None and after < last:
            raise LabError(f"The new step needs `{_needed_name(A, last, needs)}`, made by {A.title(A.nodes[last])} "
                           f"after {A.title(A.nodes[after])}: it can't go before it, or the order would loop.")
        last = after
    at = _insert_after(A, last)
    patches = [(at, at - 1, _render_call(A.indent, outs, fn, positional + keywords))]
    if callee.kind in ("toolbox", "class") and callee.obj is not None and fn not in A.toolbox_imported \
            and fn not in A.module_names:
        patches += _import_patch(A, add=[fn])
    return patches


def _needed_name(A: _Analysis, index: int, needs: list[int]) -> str:
    return A.nodes[index].outs[0] if A.nodes[index].outs else A.nodes[index].id


def _free_name(A: _Analysis, fn: str) -> str:
    words = fn.split("_")
    cands = [words[-1]] if len(words) > 1 else []
    cands += [f"{fn}_result", f"{fn}_table"] + [f"{fn}_{i}" for i in range(2, 50)]
    for c in cands:
        try:
            return _check_name(A, c)
        except LabError:
            continue
    raise LabError(f"Give the new step's output a name (out): `{fn}_result` is taken.")


def _op_remove_step(A: _Analysis, op: dict) -> list:
    node = A.find(_need(op, "node", "the step to remove"))
    for o in A.outputs:
        if o["from"] == node.id:
            raise LabError(f"`{o['wire']}` is a result ({o['label']}): remove the result first.")
    for name, users in A.users(node.index).items():
        if users:
            those = "that" if len(users) == 1 else "those"
            raise LabError(f"`{name}` is used by {_titles(A, users)}: connect {those} to something else first.")
    patches = [(node.line, node.end_line, [])]
    if node.kind == "call" and node.fn in A.toolbox_imported and not _uses_elsewhere(A, node.fn, node.stmt):
        patches += _import_patch(A, remove=[node.fn])
    return patches


def _op_connect(A: _Analysis, op: dict) -> list:
    node = A.find(_need(op, "node", "the step to connect into"))
    port_name = _need(op, "port", "the step's input to connect")
    if node.kind != "call":
        raise LabError(f"`{node.id}` is a code box: change its code in the editor.")
    spec = {k: op[k] for k in ("wire", "setting", "input", "value", "code") if k in op}
    if len(spec) != 1:
        raise LabError("Say what to connect: one of wire, setting, input, value or code.")
    ports = {p["name"]: (p, a) for p, a in zip(node.ports, node.port_args)}
    if port_name not in ports:
        names = ", ".join(p["name"] for p in node.ports) or "none"
        raise LabError(f"{A.title(node)} has no input `{port_name}`. Its inputs: {names}.")
    port, arg = ports[port_name]
    if port_name.startswith("*") or "[" in port_name:
        raise LabError(f"`{port_name}` can't be connected from the flow: change it in the code.")
    text, wire = _arg_text(A, spec, node.index)
    move_after = None
    if wire is not None:
        if wire in node.outs and A.producer(wire, node.index) is None:
            raise LabError(f"A step can't take its own result (`{wire}`).")
        p = A.producer(wire, node.index)
        if p is None:
            p = A.producer(wire)
            if p in A.descendants(node.index) or p == node.index:
                raise LabError(f"`{wire}` is made by {A.title(A.nodes[p])}, which needs the result of "
                               f"{A.title(node)}: connecting it would make the order loop.")
            between = [n for n in A.nodes[node.index + 1:p + 1] if n.index in A.descendants(node.index)]
            if between:
                raise LabError(f"`{wire}` is made after {A.title(node)}, and {_titles(A, between)} in between "
                               f"needs the result of {A.title(node)}: the order would loop. Move those first.")
            move_after = p
    spans: list[tuple[int, int, int, int, str]] = []
    if arg is not None:
        target = arg.value if isinstance(arg, ast.keyword) else arg
        l0, c0, l1, c1 = A.src.span(target)
        spans.append((l0, c0, l1, c1, text))
    else:
        positional_ok = not node.call.keywords and not any(
            p["source"] in ("default", "empty") for p in node.ports[:node.ports.index(port)])
        params = node.callee.params or []
        param = next((x for x in params if x.name == port_name), None)
        piece = text if positional_ok and param is not None and param.kind == "pos" else f"{port_name}={text}"
        everything = list(node.call.args) + list(node.call.keywords)
        if everything:
            last = max(everything, key=lambda a: (a.end_lineno, a.end_col_offset))
            _, _, l1, c1 = A.src.span(last)
            spans.append((l1, c1, l1, c1, ", " + piece))
        else:
            l1, c1 = node.call.func.end_lineno, A.src.col(node.call.func.end_lineno, node.call.func.end_col_offset)
            line = A.src.line(l1)
            c1 = line.index("(", c1) + 1
            spans.append((l1, c1, l1, c1, piece))
    first, last, new = _statement_patch(A, node.stmt, spans)
    if move_after is None:
        return [(first, last, new)]
    at = A.nodes[move_after].end_line + 1
    return [(first, last, []), (at, at - 1, new)]


def _op_disconnect(A: _Analysis, op: dict) -> list:
    """Take an input of a step back to its default: its argument goes from the call (later positional arguments are
    then written with their names, so each still reaches the same input)."""
    node = A.find(_need(op, "node", "the step whose input to disconnect"))
    port_name = _need(op, "port", "the input to take back to its default")
    if node.kind != "call":
        raise LabError(f"`{node.id}` is a code box: change its code in the editor.")
    ports = {p["name"]: (p, a) for p, a in zip(node.ports, node.port_args)}
    if port_name not in ports:
        names = ", ".join(p["name"] for p in node.ports) or "none"
        raise LabError(f"{A.title(node)} has no input `{port_name}`. Its inputs: {names}.")
    _, arg = ports[port_name]
    if arg is None:
        return []  # not connected: it uses its default already
    params = node.callee.params if node.callee else None
    if params is None:
        raise LabError(f"The inputs of `{node.fn}` aren't known: change the call in the code.")
    param = next((x for x in params if x.name == port_name), None)
    if param is None or param.default is None:
        raise LabError(f"`{port_name}` of {A.title(node)} has no default: it must be connected to something. Connect "
                       "it to something else instead.")
    call = node.call
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        raise LabError(f"The call of {A.title(node)} passes its inputs with * or **: change it in the code.")
    items: list[str] = []
    after = False
    for i, a in enumerate(call.args):
        if a is arg:
            after = True
            continue
        if after:
            if i >= len(params) or params[i].kind != "pos":
                raise LabError(f"The call of {A.title(node)} can't be written without `{port_name}`: change it in "
                               "the code.")
            items.append(f"{params[i].name}={A.src.seg(a)}")
        else:
            items.append(A.src.seg(a))
    items += [A.src.seg(k) for k in call.keywords if k is not arg]
    everything = list(call.args) + list(call.keywords)
    l0, c0, _, _ = A.src.span(min(everything, key=lambda a: (a.lineno, a.col_offset)))
    _, _, l1, c1 = A.src.span(max(everything, key=lambda a: (a.end_lineno, a.end_col_offset)))
    return [_statement_patch(A, node.stmt, [(l0, c0, l1, c1, ", ".join(items))])]


def _op_remove_input(A: _Analysis, op: dict) -> list:
    """Remove a raw file input (or `corrections`) from the recipe's signature and its docstring's Inputs."""
    name = _need(op, "input", "the input to remove")
    if name not in A.inputs:
        raise LabError(f"There is no input `{name}`. Inputs: {', '.join(A.inputs) or 'none'}.")
    users = [n for n in A.nodes if name in n.deps]
    if users:
        those = "that" if len(users) == 1 else "those"
        raise LabError(f"`{name}` is used by {_titles(A, users)}: remove {those} or connect {those} to something else "
                       "first.")
    params = [t for n, t, _ in _param_texts(A) if n != name]
    doc = _Doc(A)
    doc.remove("Inputs", name)
    patches = [_signature_patch(A, params)]
    if doc.changed:
        patches.append(doc.patch())
    return patches


def _op_rename_wire(A: _Analysis, op: dict) -> list:
    wire = _need(op, "wire", "the wire to rename")
    to = _need(op, "to", "its new name")
    makers = [n for n in A.nodes if wire in n.outs]
    if not makers:
        raise LabError(f"There is no wire `{wire}` in the recipe. Wires: {', '.join(sorted(A.made)) or 'none'}.")
    if op.get("node"):
        maker = A.find(op["node"])
        if wire not in maker.outs:
            raise LabError(f"{A.title(maker)} doesn't make `{wire}`.")
    elif len(makers) > 1:
        raise LabError(f"`{wire}` is made by {_titles(A, makers)}: say which one (node).")
    else:
        maker = makers[0]
    if to == wire:
        return []
    _check_name(A, to, "wire name")
    spans: dict[int, list[tuple[int, int, int, int, str]]] = {}

    def rename_in(st: ast.stmt, key: int, loads_only: bool) -> None:
        for n in ast.walk(st):
            if isinstance(n, ast.Name) and n.id == wire and (isinstance(n.ctx, ast.Load) or not loads_only):
                spans.setdefault(key, []).append((*A.src.span(n), to))

    rename_in(maker.target if maker.kind == "call" and maker.target is not None else maker.stmt, maker.index,
              loads_only=False)
    ended = False
    for n in A.nodes[maker.index + 1:]:
        if n.deps.get(wire) == maker.index:
            rename_in(n.stmt, n.index, loads_only=True)
        if wire in n.outs:
            ended = True
            break
    if not ended and A.ret is not None:
        rename_in(A.ret, -1, loads_only=True)
    patches = []
    for key, sp in spans.items():
        st = A.ret if key == -1 else A.nodes[key].stmt
        patches.append(_statement_patch(A, st, sp))
    return patches


def _setting_entry(op: dict, name: str) -> str:
    return _entry_text(name, op.get("label") or _humanize(name), op.get("help") or "", op.get("unit"),
                       op.get("type"))


def _op_add_setting(A: _Analysis, op: dict) -> list:
    name = _check_name(A, _need(op, "name", "the new setting's name, e.g. min_area"), "setting name")
    if methodfile.is_input(name):
        raise LabError(f"`{name}` would be an input (a name ending in _file is a raw file): use add_input.")
    if "code" in op:
        default = _arg_text(A, {"code": op["code"]})[0]
    elif "default" in op:
        default = _py(op["default"])
    else:
        default = None
    params = _insert_param(A, name + ("=" + default if default is not None else ""), name, default is not None)
    doc = _Doc(A)
    doc.add("Settings", _setting_entry(op, name), op.get("group"))
    return [_signature_patch(A, params), doc.patch()]


def _op_remove_setting(A: _Analysis, op: dict) -> list:
    name = _need(op, "setting", "the setting to remove")
    if name not in A.settings:
        raise LabError(f"There is no setting `{name}`. Settings: {', '.join(A.settings) or 'none'}.")
    users = [n for n in A.nodes if name in n.deps]
    if users:
        those = "that" if len(users) == 1 else "those"
        raise LabError(f"`{name}` is used by {_titles(A, users)}: connect {those} to something else first.")
    params = [t for n, t, _ in _param_texts(A) if n != name]
    doc = _Doc(A)
    doc.remove("Settings", name)
    patches = [_signature_patch(A, params)]
    if doc.changed:
        patches.append(doc.patch())
    return patches


def _op_set_default(A: _Analysis, op: dict) -> list:
    name = _need(op, "setting", "the setting whose default to change")
    if name not in A.settings:
        raise LabError(f"There is no setting `{name}`. Settings: {', '.join(A.settings) or 'none'}.")
    if "code" in op:
        default = _arg_text(A, {"code": op["code"]})[0]
    elif "value" in op:
        default = _py(op["value"])
    else:
        raise LabError("`set_default` needs `value` (a plain value) or `code` (Python).")
    params = _param_texts(A)
    i = next(k for k, (n, _, _) in enumerate(params) if n == name)
    had = params[i][2]
    arg = next(a for a in A.node.args.posonlyargs + A.node.args.args + A.node.args.kwonlyargs if a.arg == name)
    head = A.src.seg(arg)
    new_text = f"{head} = {default}" if arg.annotation is not None else f"{head}={default}"
    if had or name in [a.arg for a in A.node.args.kwonlyargs]:
        params[i] = (name, new_text, True)
    else:
        del params[i]
        end = next((k for k, (n, _, _) in enumerate(params) if n.startswith("*")), len(params))
        at = next((k for k, (n, _, d) in enumerate(params[:end]) if d), end)
        params.insert(at, (name, new_text, True))
    return [_signature_patch(A, [t for _, t, _ in params])]


def _op_add_input(A: _Analysis, op: dict) -> list:
    """A raw file (a name ending in _file), `corrections`, or with `kind: result` a result of other experiments
    (its docstring line ends in `[result]`, or `[result: <result>]` with `result`)."""
    if op.get("kind") == "result":
        return _add_result_input(A, op)
    name = _check_name(A, _need(op, "name", "the input's name, ending in _file (e.g. plate_file)"), "input name")
    if not methodfile.is_input(name):
        raise LabError(f"An input's name ends in _file (e.g. `{name}_file`), or is `corrections`; for results of "
                       "other experiments give `kind: result`.")
    optional = bool(op.get("optional"))
    params = _insert_param(A, name + ("=None" if optional else ""), name, optional, first_place=True)
    types = op.get("types") or []
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    types = [t if t.startswith(".") else "." + t for t in types]
    label = (op.get("label") or ("The experiment's approved corrections" if name == "corrections"
                                 else _humanize(name.removesuffix("_file")))).strip()
    text = f"{name}: {label}" + (f" [{', '.join(types)}]" if types else "")
    doc = _Doc(A)
    doc.add("Inputs", text)
    return [_signature_patch(A, params), doc.patch()]


def _add_result_input(A: _Analysis, op: dict) -> list:
    name = _check_name(A, _need(op, "name", "the input's name, e.g. scores"), "input name")
    if name.endswith("_file") or name == "corrections":
        raise LabError(f"`{name}` would be a raw file or the corrections: give a result input another name, e.g. "
                       f"`{name.removesuffix('_file') if name != 'corrections' else 'results'}`.")
    result = op.get("result")
    if result is not None and (not isinstance(result, str) or not methodfile.RESULT_NAME.match(result.strip())):
        raise LabError("`result` is the name the other experiments return the table under, one word like `scores` "
                       "(or `step:<function>`).")
    optional = bool(op.get("optional"))
    params = _insert_param(A, name + ("=None" if optional else ""), name, optional, first_place=True)
    label = (op.get("label") or _humanize(name)).strip().rstrip(".")
    text = f"{name}: {label} [result{': ' + result.strip() if result else ''}]"
    doc = _Doc(A)
    doc.add("Inputs", text)
    return [_signature_patch(A, params), doc.patch()]


def _op_add_result(A: _Analysis, op: dict) -> list:
    wire = _need(op, "wire", "the wire to return")
    if wire not in A.made:
        raise LabError(f"There is no wire `{wire}` in the recipe. Wires: {', '.join(sorted(A.made)) or 'none'}.")
    name = op.get("name") or wire
    if not isinstance(name, str) or not name.strip():
        raise LabError("A result needs a name.")
    if any(o["id"] == name for o in A.outputs):
        raise LabError(f"There is already a result called `{name}`: pick another name.")
    label = op.get("label") or _humanize(name)
    item = f"{json.dumps(name, ensure_ascii=False)}: {wire}"
    patches = []
    if A.ret is None:
        last = A.stmts[-1].end_lineno if A.stmts else (A.doc.end_lineno if A.doc else A.node.body[0].lineno - 1)
        patches.append((last + 1, last, _wrap(A.indent + "return {", [item], "}")))
    else:
        v = A.ret.value
        if not (isinstance(v, ast.Dict) and None not in v.keys):
            raise LabError("The recipe's return isn't a plain dict of results: change it in the code.")
        if v.keys:
            _, _, l1, c1 = A.src.span(v.values[-1])
            spans = [(l1, c1, l1, c1, ", " + item)]
        else:
            l0, c0, _, _ = A.src.span(v)
            spans = [(l0, c0 + 1, l0, c0 + 1, item)]
        patches.append(_statement_patch(A, A.ret, spans))
    doc = _Doc(A)
    doc.add("Returns", f"{name}: {label}")
    patches.append(doc.patch())
    return patches


def _op_remove_result(A: _Analysis, op: dict) -> list:
    name = _need(op, "name", "the result to remove")
    v = A.ret.value if A.ret is not None else None
    if not isinstance(v, ast.Dict) or None in v.keys:
        raise LabError("The recipe's return isn't a plain dict of results: change it in the code.")
    i = next((k for k, key in enumerate(v.keys) if isinstance(key, ast.Constant) and key.value == name), None)
    if i is None:
        raise LabError(f"There is no result `{name}`. Results: {', '.join(o['id'] for o in A.outputs) or 'none'}.")
    if len(v.keys) == 1:
        l0, c0, l1, c1 = A.src.span(v)
        spans = [(l0, c0, l1, c1, "{}")]
    elif i < len(v.keys) - 1:
        l0, c0, _, _ = A.src.span(v.keys[i])
        nl0, nc0, _, _ = A.src.span(v.keys[i + 1])
        spans = [(l0, c0, nl0, nc0, "")]
    else:
        _, _, pl1, pc1 = A.src.span(v.values[i - 1])
        _, _, l1, c1 = A.src.span(v.values[i])
        spans = [(pl1, pc1, l1, c1, "")]
    patches = [_statement_patch(A, A.ret, spans)]
    doc = _Doc(A)
    doc.remove("Returns", name)
    if doc.changed:
        patches.append(doc.patch())
    return patches


def _op_new_function(A: _Analysis, op: dict) -> list:
    name = _check_name(A, _need(op, "name", "the new function's name, e.g. relative_areas"), "function name")
    params = op.get("params") or []
    if isinstance(params, str):
        params = [p.strip() for p in params.split(",") if p.strip()]
    for p in params:
        if not isinstance(p, str) or not p.isidentifier() or keyword.iskeyword(p):
            raise LabError(f"`{p}` can't be an input's name: use letters, digits and _.")
    if len(set(params)) != len(params):
        raise LabError("Each input of the new function needs its own name.")
    sentence = (op.get("sentence") or f"{_humanize(name)}.").strip()
    if not sentence.endswith((".", "?", "!")):
        sentence += "."
    body = op.get("body")
    if isinstance(body, list):
        body = "\n".join(body)
    if body:
        body_lines = textwrap.dedent(body.strip("\n")).splitlines()
    else:
        body_lines = [f"return {params[0]}" if params else "return None"]
    lines = _wrap(f"def {name}(", params, "):")
    doc = _wrap_text(f'    """{sentence}"""', "    ")
    lines += doc + ["    " + ln if ln.strip() else "" for ln in body_lines]
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError as e:
        raise LabError(f"Python can't read the new function (line {(e.lineno or 1) - 1} of its body): {e.msg}.") from e
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise LabError("The body must be the inside of one function only.")
    prev_end = 0
    for n in A.method.tree.body:
        if (n.end_lineno or n.lineno) < A.fn.line:
            prev_end = max(prev_end, n.end_lineno or n.lineno)
    at = A.fn.line
    for ln in range(prev_end + 1, A.fn.line):
        if A.src.line(ln).lstrip().startswith("#"):
            at = ln
            break
    return [(at, at - 1, lines + ["", ""])]


def _op_group(A: _Analysis, op: dict) -> list:
    ids = _need(op, "nodes", "the steps to put in the group")
    title = str(_need(op, "title", "the group's name")).strip()
    if "\n" in title or title.startswith("-"):
        raise LabError("A group's name is one line of words.")
    if isinstance(ids, str):
        ids = [ids]
    nodes = sorted({A.find(i).index for i in ids})
    if nodes != list(range(nodes[0], nodes[-1] + 1)):
        between = [A.nodes[i] for i in range(nodes[0], nodes[-1] + 1) if i not in nodes]
        raise LabError(f"A group holds steps that follow each other; {_titles(A, between)} "
                       f"{'is' if len(between) == 1 else 'are'} in between: add {'it' if len(between) == 1 else 'them'}"
                       " too, or move them.")
    for i in nodes:
        g = A.group_of(A.nodes[i])
        if g:
            raise LabError(f"{A.title(A.nodes[i])} is already in the group {g['title']}: ungroup it first.")
    first, last = A.nodes[nodes[0]], A.nodes[nodes[-1]]
    patches = [(first.line, first.line - 1, [f"{A.indent}# --- {title}"])]
    if last.index + 1 < len(A.nodes):
        patches.append((last.end_line + 1, last.end_line, [f"{A.indent}# ---"]))
    return patches


def _op_ungroup(A: _Analysis, op: dict) -> list:
    gid = _need(op, "group", "the group to dissolve")
    g = next((x for x in A.groups if x["id"] == gid), None)
    if g is None:
        raise LabError(f"There is no group `{gid}`. Groups: {', '.join(x['id'] for x in A.groups) or 'none'}.")
    patches = [(g["line"], g["line"], [])]
    if g["closed_by"]:
        patches.append((g["closed_by"], g["closed_by"], []))
    return patches


def _op_add_code(A: _Analysis, op: dict) -> list:
    code = op.get("code")
    if code is None and op.get("lines") is not None:
        code = "\n".join(op["lines"])
    if not code or not str(code).strip():
        raise LabError("`add_code` needs `code` (or `lines`): the Python lines of the code box.")
    code = textwrap.dedent(str(code).strip("\n"))
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise LabError(f"Python can't read the code (line {e.lineno}): {e.msg}.") from e
    if any(isinstance(st, ast.Return) for st in tree.body):
        raise LabError("A code box can't return: add a result instead.")
    after = A.find(op["after"]).index if op.get("after") else None
    reads: set[str] = set()
    for st in tree.body:
        reads.update(_loads(st))
    made = {n for st in tree.body for n in _assigned(st)}
    for name in sorted(made):
        if name in A.made or name in A.params:
            raise LabError(f"The name `{name}` is taken (a wire or setting in the recipe): pick another.")
    needs = []
    for name in sorted(reads - made):
        p = A.producer(name, after + 1 if after is not None else None)
        if p is None and A.producer(name) is not None:
            later = A.nodes[A.producer(name)]
            raise LabError(f"The code reads `{name}`, made by {A.title(later)} after the place asked for: the order "
                           "would loop.")
        if p is not None:
            needs.append(p)
    last = max(needs) if needs else None
    if after is not None:
        last = after
    at = _insert_after(A, last)
    return [(at, at - 1, [(A.indent + ln) if ln.strip() else "" for ln in code.splitlines()])]


_HANDLERS = {"add_step": _op_add_step, "remove_step": _op_remove_step, "connect": _op_connect,
             "rename_wire": _op_rename_wire, "add_setting": _op_add_setting, "remove_setting": _op_remove_setting,
             "set_default": _op_set_default, "add_input": _op_add_input, "add_result": _op_add_result,
             "remove_result": _op_remove_result, "new_function": _op_new_function, "group": _op_group,
             "ungroup": _op_ungroup, "add_code": _op_add_code, "disconnect": _op_disconnect,
             "remove_input": _op_remove_input}


def edit_all(source: str, ops: list[dict[str, Any]]) -> str:
    """Several changes, one after the other (each sees the source the one before left)."""
    for i, op in enumerate(ops, start=1):
        try:
            source = edit(source, op)
        except LabError as e:
            if len(ops) == 1:
                raise
            raise LabError(f"Change {i} ({op.get('op') if isinstance(op, dict) else '?'}): {e}") from e
    return source


# ---------------------------------------------------------------------------------------------------- a new method


def new_method(name: str, description: str = "") -> str:
    """A starting file: the name, the description and an empty recipe (no inputs, no steps yet). It passes the style
    checker; the flow then adds raw files, settings and steps to it."""
    name = " ".join(str(name or "").split()) or "New method"
    description = " ".join(str(description or "").split()) or "What this method measures, and how."
    head = _wrap_text(description, "")
    doc = _wrap_text(f'    """{name.rstrip(".")}.', "    ")
    return "\n".join(['"""' + name, "", *head, '"""', "", "", "def analysis():", *doc, "",
                      "    Add raw files, settings and steps in the flow.", '    """', "    return {}", ""])


__all__ = ["graph", "edit", "edit_all", "new_method", "type_of_value", "OPS"]

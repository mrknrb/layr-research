"""The style checker: reads a method file and says, in plain words with line numbers, what to fix.

Errors (they block saving): code Python can't read, an import that can't work, no recipe. Warnings (they don't block
the user; an AI agent's change comes back with them so it fixes them):
- a function without a docstring;
- a formula that does more than arithmetic;
- a step much longer than about 25 lines;
- a function that changes a table (or list) it was given instead of a copy;
- a recipe that does more than call steps and keep their results;
- imports other than the toolbox, pandas, numpy, scipy, statsmodels, scikit-learn, the standard maths modules and
  the packages the method lists in REQUIREMENTS;
- PAGES, CORRECTIONS or REQUIREMENTS that can't be used as written (lab/pages.py);
- a `rows` setting whose columns can't be read, or whose default doesn't fit its columns;
- an `Inputs:` line that can't be used as written: a bracket that is neither file types (`[.csv, .xlsx]`) nor
  `[result]` / `[result: name]`, `[result]` on a raw file or on `corrections`, a result's name that isn't one, an
  input the recipe doesn't take, an entry that is really a setting, `[result]` written under `Settings:`;
- reading or writing files, or the network, outside the toolbox;
- a name used both for a function and for a result in the same function (Python then can't call the function).
"""

from __future__ import annotations

import ast
import importlib.util
import re
from typing import Any

from . import methodfile
from .latex import body_statements, call_name

ALLOWED_IMPORTS = {"toolbox", "pandas", "numpy", "scipy", "statsmodels", "sklearn", "pyreadstat", "matplotlib",
                   "math", "statistics", "cmath", "fractions", "decimal", "re", "datetime", "itertools", "collections",
                   "functools", "string", "textwrap", "dataclasses", "typing", "random", "__future__"}
STEP_LINES = 30  # "much longer than about 25"
FILE_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}
NUMPY_FILE = {"load", "save", "savez", "savez_compressed", "loadtxt", "savetxt", "genfromtxt", "fromfile", "tofile",
              "memmap"}
IO_FORMATS = {"csv", "excel", "json", "parquet", "pickle", "sql", "sql_table", "sql_query", "html", "feather", "hdf",
              "table", "clipboard", "xml", "stata", "sas", "spss", "orc", "fwf", "latex", "markdown", "string_io"}
CHANGING_METHODS = {"append", "extend", "insert", "pop", "remove", "clear", "update", "drop_duplicates", "setdefault",
                    "popitem", "sort", "reverse"}


def _msg(level: str, line: int | None, message: str, function: str | None = None) -> dict[str, Any]:
    return {"level": level, "line": line, "function": function, "message": message}


def check(source: str) -> dict[str, Any]:
    """{ok (no errors), messages: [{level, line, function, message}], method summary when it could be read}."""
    try:
        method = methodfile.parse(source)
    except SyntaxError as e:
        where = f"line {e.lineno}" if e.lineno else "the file"
        text = (e.text or "").strip()
        detail = f" — the line is: {text}" if text else ""
        return {"ok": False, "messages": [_msg("error", e.lineno, f"Python can't read {where}: {_plain_syntax(e)}"
                                                                 f"{detail}")], "summary": None}
    messages: list[dict[str, Any]] = []
    messages += _imports(method)
    messages += _recipe(method)
    messages += _declarations(method)
    messages += _rows_settings(method)
    messages += _inputs_section(method)
    names = {f.name for f in method.functions}
    formulas = {f.name for f in method.functions if f.kind == "formula"}
    for fn in method.functions:
        messages += _docstring(fn)
        messages += _formula(fn, formulas)
        messages += _length(fn)
        messages += _changes_inputs(fn)
        messages += _file_access(fn)
        messages += _shadowing(fn, names)
    messages.sort(key=lambda m: (m["level"] != "error", m["line"] or 0))
    return {"ok": not any(m["level"] == "error" for m in messages), "messages": messages,
            "summary": methodfile.summary(method)}


def _plain_syntax(e: SyntaxError) -> str:
    msg = e.msg or "invalid syntax"
    plain = {
        "invalid syntax": "something is missing or extra here (a bracket, a comma, a colon at the end of a def, "
                          "if or for line?)",
        "unexpected indent": "this line is indented more than the one before it",
        "expected an indented block": "the line after a def, if or for line must be indented",
        "unindent does not match any outer indentation level": "this line's indentation doesn't line up with the "
                                                               "lines above",
        "EOL while scanning string literal": "a text in quotes isn't closed",
        "unterminated string literal": "a text in quotes isn't closed",
    }
    for k, v in plain.items():
        if msg.startswith(k):
            return v
    return msg


# ---------------------------------------------------------------------------------------------------- imports


def _declarations(method: methodfile.Method) -> list[dict[str, Any]]:
    """PAGES, CORRECTIONS and REQUIREMENTS that can't be used as written (lab/pages.py)."""
    from . import pages

    fn = method.recipe_function
    outputs = set(fn.doc.returns) if fn and fn.doc.returns else None
    inputs = {p for p in fn.params if p.endswith("_file")} if fn else None
    line = {n.targets[0].id: n.lineno for n in method.tree.body
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
    out = [_msg("warning", line.get(name), p) for name, p in pages.problems(method.constants, outputs, inputs)]
    for n in pages.NAMES:
        if n not in method.constants and any(isinstance(x, ast.Assign) and len(x.targets) == 1 and
                                             isinstance(x.targets[0], ast.Name) and x.targets[0].id == n
                                             for x in method.tree.body):
            out.append(_msg("warning", line.get(n), f"{n} must be written as a plain value (text, numbers, lists and "
                                                     "dicts), so the panel can read it without running the method."))
    return out


def _rows_settings(method: methodfile.Method) -> list[dict[str, Any]]:
    """`rows` settings whose columns can't be read, or whose default doesn't fit them."""
    fn = method.recipe_function
    out = []
    for s in methodfile.settings(method):
        if s["type"] != "rows":
            continue
        if s.get("problem"):
            out.append(_msg("warning", fn.line, f"The setting `{s['id']}`: {s['problem']}", fn.name))
            continue
        try:
            methodfile.check_value(s, s.get("default"))
        except ValueError as e:
            out.append(_msg("warning", fn.line, f"The default of `{s['id']}` doesn't fit its columns: {e}", fn.name))
    return out


def _inputs_section(method: methodfile.Method) -> list[dict[str, Any]]:
    """The recipe docstring's `Inputs:` lines that can't be used as written (see the module's docstring)."""
    fn = method.recipe_function
    if fn is None:
        return []
    out = []
    lines = fn.source.splitlines()

    def warn(text: str, name: str | None = None) -> None:
        """A warning at the docstring line of `name` (else the recipe's def line)."""
        at = next((fn.line + i for i, ln in enumerate(lines) if name and re.match(
            rf"\s*{re.escape(name)}\s*(\([^)]*\))?\s*:", ln) and i > 0), fn.line)
        out.append(_msg("warning", at, text, fn.name))

    for name, text in fn.doc.inputs.items():
        _, marker = methodfile.input_marker(text)
        is_result, wanted = methodfile.result_marker(marker)
        if marker is not None and not is_result and not re.fullmatch(r"(\s*\.[A-Za-z0-9]+\s*,?)+", marker):
            warn(f"The input `{name}` ends with `[{marker}]`, which is neither file types (like `[.csv, .xlsx]`) nor "
                 "`[result]` (a result of other experiments, e.g. `[result]` or `[result: scores]`).", name)
            continue
        if name not in fn.params:
            what = "a result of other experiments" if is_result else "an input"
            warn(f"`Inputs:` lists `{name}` as {what}, but the recipe `{fn.name}` has no parameter `{name}`: add it "
                 f"to the recipe's parameters, or remove the line.", name)
            continue
        if is_result and name.endswith("_file"):
            warn(f"`{name}` is marked `[result]`, but a name ending in _file always means a raw file: rename it "
                 f"(e.g. `{name.removesuffix('_file')}`) to take a result of other experiments.", name)
            continue
        if is_result and name == "corrections":
            warn("`corrections` is marked `[result]`, but that name always gets the experiment's corrections: give "
                 "the result input another name, e.g. `results`.", name)
            continue
        if is_result and wanted is not None and not methodfile.RESULT_NAME.match(wanted):
            warn(f"In `[result: {wanted}]` of `{name}`, the name after `result:` is the name the other experiments' "
                 "recipe returns the table under: one word like `scores` (or `step:<function>` for a step's table).",
                 name)
            continue
        if not is_result and not name.endswith("_file") and name != "corrections":
            warn(f"`Inputs:` lists `{name}`, but it is neither a raw file (its name would end in _file) nor a result "
                 f"of other experiments (its line would end in `[result]`), so it is a setting: move it under "
                 "`Settings:`, or end its line with `[result]`.", name)
    for s in fn.doc.settings:
        if methodfile.result_marker(s.get("unit"))[0]:
            warn(f"`{s['id']}` is marked `[result]` under `Settings:`: a result of other experiments is an input; "
                 "move its line under `Inputs:`.", s["id"])
    return out


def _imports(method: methodfile.Method) -> list[dict[str, Any]]:
    from . import packages, pages

    out = []
    reqs = pages.requirements(method.constants)
    for imp in method.imports:
        module = imp["module"]
        top = module.split(".")[0]
        line = imp["line"]
        if module.startswith("."):
            out.append(_msg("error", line, f"`{module}` is a relative import: a method imports by full names "
                                           "(e.g. `from toolbox import fit_line`)."))
            continue
        extra = packages.provides(reqs, top) if reqs else None
        if top not in ALLOWED_IMPORTS and not extra and not (reqs and extra is None):
            out.append(_msg("warning", line, f"`{module}` is imported. Methods use the toolbox, pandas, numpy, "
                                             "scipy, statsmodels, scikit-learn and the maths modules, so anyone can "
                                             "rerun them; a package from the internet goes into the method's "
                                             "packages (REQUIREMENTS)."))
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found and extra:
            continue
        if not found and reqs and extra is None:
            out.append(_msg("warning", line, f"`{module}` isn't installed yet: the method's packages "
                                             f"({', '.join(reqs)}) are installed before its first run."))
            continue
        if not found:
            hint = " If it is a package from the internet, add it to the method's packages (REQUIREMENTS)." \
                if top not in ALLOWED_IMPORTS else ""
            out.append(_msg("error", line, f"There is no module called `{module}`: check its spelling.{hint}"))
            continue
        if top == "toolbox" and imp["names"]:
            try:
                mod = importlib.import_module(module)
            except Exception as e:  # the toolbox is the plugin's own code; report rather than crash
                out.append(_msg("error", line, f"`{module}` can't be imported: {e}"))
                continue
            for name in imp["names"]:
                if name != "*" and not hasattr(mod, name):
                    known = ", ".join(getattr(mod, "__all__", [])) or "see the toolbox"
                    which = "The toolbox" if module == "toolbox" else f"`{module}`"
                    out.append(_msg("error", line, f"{which} has no `{name}`. It has: {known}."))
    return out


# ---------------------------------------------------------------------------------------------------- recipe


def _recipe(method: methodfile.Method) -> list[dict[str, Any]]:
    if not method.functions:
        return [_msg("error", 1, "The file has no functions: a method needs at least a recipe, the function that "
                                 "calls the steps.")]
    fn = method.recipe_function
    if fn is None:
        return [_msg("error", 1, f"RECIPE names `{method.recipe}`, but there is no function of that name.")]
    out = []
    for st in body_statements(fn.node):
        if isinstance(st, ast.Assign) and _just_calls(st.value):
            continue
        if isinstance(st, ast.Return) and st.value is not None and _plain_result(st.value):
            continue
        what = ast.unparse(st).splitlines()[0][:80]
        out.append(_msg("warning", st.lineno, f"The recipe `{fn.name}` should only call steps and keep their "
                                              f"results (`name = step(...)`), then return them; `{what}` does "
                                              "more. Move it into a step.", fn.name))
    if not any(isinstance(st, ast.Return) for st in body_statements(fn.node)):
        out.append(_msg("warning", fn.line, f"The recipe `{fn.name}` returns nothing: return a dict of its results, "
                                            'e.g. `return {"activities": activities}`.', fn.name))
    return out


def _simple_arg(node: ast.AST) -> bool:
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Call):
        return _just_calls(node)
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_simple_arg(e) for e in node.elts)
    return False


def _just_calls(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
        all(_simple_arg(a) for a in node.args) and all(_simple_arg(k.value) for k in node.keywords)


def _plain_result(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Dict):
        return all(isinstance(k, ast.Constant) for k in node.keys) and all(isinstance(v, ast.Name) for v in node.values)
    return False


# ---------------------------------------------------------------------------------------------------- functions


def _docstring(fn: methodfile.Function) -> list[dict[str, Any]]:
    if fn.has_doc:
        return []
    return [_msg("warning", fn.line, f"`{fn.name}` has no docstring: start it with one plain sentence saying what it "
                                     'does, in triple quotes (""" ... """).', fn.name)]


def _formula(fn: methodfile.Function, formulas: set[str]) -> list[dict[str, Any]]:
    if not fn.formula_shaped:
        return []
    for st in body_statements(fn.node):
        value = st.value
        if not methodfile.is_arithmetic(value, formulas):
            bad = _first_non_arithmetic(value, formulas)
            return [_msg("warning", st.lineno, f"`{fn.name}` is written as a formula (one calculation), but "
                                               f"`{bad}` is not arithmetic. Keep formulas to numbers, names, "
                                               "+ - * / ** and maths functions such as sqrt or log; pick rows and "
                                               "columns in a step and pass the numbers in.", fn.name)]
    return []


def _first_non_arithmetic(node: ast.AST, formulas: set[str]) -> str:
    for child in ast.walk(node):
        if isinstance(child, (ast.Attribute, ast.Subscript, ast.Call, ast.Compare, ast.BoolOp, ast.IfExp,
                              ast.ListComp, ast.DictComp, ast.GeneratorExp, ast.Lambda, ast.List, ast.Dict)) \
                and not methodfile.is_arithmetic(child, formulas):
            return ast.unparse(child)[:60]
    return ast.unparse(node)[:60]


def _length(fn: methodfile.Function) -> list[dict[str, Any]]:
    if fn.kind == "recipe":
        return []
    body = body_statements(fn.node)
    if not body:
        return []
    lines = fn.source.splitlines()
    start = body[0].lineno - fn.line
    count = sum(1 for ln in lines[start:] if ln.strip() and not ln.strip().startswith("#"))
    if count <= STEP_LINES:
        return []
    return [_msg("warning", fn.line, f"`{fn.name}` is {count} lines long; a step reads best in about 25. Move a part "
                                     "of it into a function of its own (one job each) and call that.", fn.name)]


def _names_in(target: ast.AST) -> ast.AST:
    """The variable at the root of a target: `scores` for scores["x"], scores.loc[...], scores.x."""
    while isinstance(target, (ast.Subscript, ast.Attribute)):
        target = target.value
    return target


def _changes_inputs(fn: methodfile.Function) -> list[dict[str, Any]]:
    params = set(fn.params)
    aliases = {p: p for p in params}  # a name -> the parameter it is (not a copy of)
    out = []
    reported: set[str] = set()

    def report(name: str, line: int, how: str) -> None:
        param = aliases.get(name)
        if param is None or param in reported:
            return
        reported.add(param)
        out.append(_msg("warning", line, f"`{fn.name}` changes `{param}`, which it was given ({how}). The caller's "
                                         f"table changes too, which is hard to follow: work on a copy, "
                                         f"`{param} = {param}.copy()`, or build a new table.", fn.name))

    for node in ast.walk(fn.node):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if isinstance(node.value, ast.Name) and node.value.id in aliases:
                        aliases[t.id] = aliases[node.value.id]
                    else:
                        aliases.pop(t.id, None)
                elif isinstance(t, (ast.Subscript, ast.Attribute)):
                    root = _names_in(t)
                    if isinstance(root, ast.Name):
                        report(root.id, node.lineno, f"`{ast.unparse(t)[:50]} = ...`")
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, (ast.Subscript, ast.Attribute)):
            root = _names_in(node.target)
            if isinstance(root, ast.Name):
                report(root.id, node.lineno, f"`{ast.unparse(node.target)[:50]}` is changed in place")
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                root = _names_in(t)
                if isinstance(root, ast.Name) and not isinstance(t, ast.Name):
                    report(root.id, node.lineno, "`del`")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            root = _names_in(node.func.value)
            if not isinstance(root, ast.Name):
                continue
            inplace = any(k.arg == "inplace" and isinstance(k.value, ast.Constant) and k.value.value is True
                          for k in node.keywords)
            if inplace:
                report(root.id, node.lineno, f"`{node.func.attr}(..., inplace=True)`")
            elif node.func.attr in CHANGING_METHODS and isinstance(node.func.value, ast.Name):
                report(root.id, node.lineno, f"`.{node.func.attr}(...)`")
    return out


def _file_access(fn: methodfile.Function) -> list[dict[str, Any]]:
    out = []
    for node in ast.walk(fn.node):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        what = None
        if isinstance(node.func, ast.Name) and node.func.id in FILE_CALLS:
            what = node.func.id
        elif isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            base = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            if (attr.startswith("read_") and attr[5:] in IO_FORMATS) or (attr.startswith("to_") and attr[3:] in
                                                                            IO_FORMATS and attr != "to_string"):
                what = ast.unparse(node.func)
            elif base in ("np", "numpy") and attr in NUMPY_FILE:
                what = ast.unparse(node.func)
            elif attr in ("savefig", "urlopen", "get", "post") and base in ("plt", "fig", "requests", "urllib"):
                what = ast.unparse(node.func)
        if what:
            out.append(_msg("warning", node.lineno, f"`{what}` (in `{fn.name}`) reads or writes files or the network. "
                                                    "Raw data comes in through the recipe's inputs (parameters "
                                                    "ending in _file) and the toolbox's readers (such as "
                                                    "read_table); results go out by returning them from the recipe.",
                            fn.name))
        del name
    return out


def _shadowing(fn: methodfile.Function, names: set[str]) -> list[dict[str, Any]]:
    out = []
    assigned: dict[str, int] = {}
    for node in ast.walk(fn.node):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.For, ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in names and n.id != fn.name:
                    assigned.setdefault(n.id, node.lineno)
    called = {n.func.id for n in ast.walk(fn.node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for name, line in assigned.items():
        if name in called:
            out.append(_msg("warning", line, f"In `{fn.name}`, `{name}` is the name of a function and also of a "
                                             "result you keep. Python then can't call the function here (it fails "
                                             f"with 'cannot access local variable'). Give the result another name, "
                                             f"e.g. `{name}_table`.", fn.name))
    return out

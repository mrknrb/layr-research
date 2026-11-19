"""Reading a method file without running it: its functions, what kind each is, and the recipe's settings.

A method is one Python file:
- The module docstring's first line is the method's name; the rest describes it.
- The **recipe** is the function named by a module-level `RECIPE = "name"`, or else the last function in the file.
  Its parameters are the experiment's inputs and settings:
  - a parameter whose name ends in `_file` is a raw file the experiment supplies (`gaml_file`, `plate_file`);
  - a parameter named `corrections` gets the experiment's approved corrections (a list of dicts);
  - every other parameter is a setting; its default (a literal, or a module-level constant holding one) is what an
    experiment uses unless it sets its own. A setting without a default must be set by the experiment.
- **Steps** are the functions the recipe calls. **Formulas** are functions made only of arithmetic lines
  (`name = ...` and `return ...`). Other functions are **helpers** (they decide or loop for a step).
- `DATABASE = ["table", ...]`: the returned tables kept in the results database (default: every returned table).

Docstrings (the explanation comes from them and from the code itself):

    def initial_rates(concentrations, rate_window_min, biomass_gcdw_per_l):
        \"\"\"One plain sentence: what it does.

        More text, if needed.

        Formula:
            slope = sum((t - t_mean) * (c - c_mean)) / sum((t - t_mean) ** 2)
        \"\"\"

The recipe's docstring documents its parameters (sections Inputs, Settings, Returns; groups inside Settings):

        Inputs:
            gaml_file: GC sequence, the GAML export from Chromeleon [.gaml]
        Settings:
            Integration:
                rt_tolerance_min: Retention time tolerance. The apex must lie this close to the reference time.
                biomass_gcdw_per_l (biomass): Biomass [gCDW/L]. One number, or one per fed substrate.
        Returns:
            activities: Specific activities

A setting line is `name: Label. Help text`; `(type)` after the name picks the form's editor (text, number,
integer, boolean, list, object, patterns, compounds, biomass), otherwise the default's type decides (a setting
without a default whose name ends in a unit is a number); `[unit]` in the label overrides the unit read from the
name (`rt_tolerance_min` → min).
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from .latex import UNITS, body_statements, call_name, split_unit

UNKNOWN = object()  # a default that isn't a plain value (it is worked out when the method runs)
SECTIONS = {"formula": "formula", "formulas": "formula", "inputs": "inputs", "settings": "settings",
            "returns": "returns", "outputs": "returns"}
SPECIAL_EDITORS = {"sample_patterns": "patterns", "compounds": "compounds", "biomass_gcdw_per_l": "biomass"}
EDITORS = {"text", "number", "integer", "boolean", "list", "object", "patterns", "compounds", "biomass"}
ARITHMETIC_CALLS = {"sqrt", "abs", "fabs", "exp", "log", "ln", "log10", "log2", "min", "max", "round", "float", "int",
                    "floor", "ceil", "minimum", "maximum"}


@dataclass
class Doc:
    sentence: str = ""
    text: str = ""
    formula: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    settings: list[dict[str, Any]] = field(default_factory=list)  # {id, group, type?, label, help, unit?}
    returns: dict[str, str] = field(default_factory=dict)


@dataclass
class Function:
    name: str
    line: int
    end_line: int
    kind: str  # recipe, step, formula, helper
    params: list[str]
    defaults: dict[str, Any]
    doc: Doc
    has_doc: bool
    node: ast.FunctionDef
    source: str
    calls: list[str]  # names of functions it calls directly (in order, once each)
    formula_shaped: bool = False


@dataclass
class Method:
    name: str
    description: str
    functions: list[Function]
    recipe: str | None
    constants: dict[str, Any]
    database: list[str] | None
    imports: list[dict[str, Any]]
    tree: ast.Module

    def function(self, name: str) -> Function | None:
        return next((f for f in self.functions if f.name == name), None)

    @property
    def recipe_function(self) -> Function | None:
        return self.function(self.recipe) if self.recipe else None


# ---------------------------------------------------------------------------------------------------- docstrings


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_doc(doc: str | None) -> Doc:
    """The docstring convention above, read into its parts."""
    out = Doc()
    if not doc:
        return out
    lines = textwrap.dedent("\n".join(doc.expandtabs().splitlines()[1:])).splitlines()
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    out.sentence = first
    section = None
    blocks: dict[str, list[str]] = {}
    text_lines: list[str] = []
    for line in lines:
        head = line.strip().rstrip(":").lower()
        if _indent(line) == 0 and line.strip().endswith(":") and head in SECTIONS:
            section = SECTIONS[head]
            blocks.setdefault(section, [])
            continue
        if section and (line.strip() == "" or _indent(line) > 0):
            blocks[section].append(line)
            continue
        if line.strip():
            section = None
        text_lines.append(line)
    paragraphs = "\n".join(text_lines).strip().split("\n\n")
    out.text = "\n\n".join(" ".join(ln.strip() for ln in p.splitlines()) for p in paragraphs if p.strip())
    out.formula = [ln.strip() for ln in textwrap.dedent("\n".join(blocks.get("formula", []))).splitlines()
                   if ln.strip()]
    out.inputs = _entries(blocks.get("inputs", []))
    out.returns = _entries(blocks.get("returns", []))
    out.settings = _settings(blocks.get("settings", []))
    return out


_ENTRY = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(\((?P<type>[^)]*)\))?\s*:\s*(?P<text>.*)$")


def _entries(lines: list[str]) -> dict[str, str]:
    """`name: text` lines (continuation lines indented deeper are joined)."""
    out: dict[str, str] = {}
    lines = textwrap.dedent("\n".join(lines)).splitlines()
    last = None
    for line in lines:
        if not line.strip():
            continue
        m = _ENTRY.match(line.strip())
        if _indent(line) == 0 and m:
            last = m.group("name")
            out[last] = m.group("text").strip()
        elif last:
            out[last] = (out[last] + " " + line.strip()).strip()
    return out


def _is_group(line: str) -> bool:
    s = line.strip()
    m = _ENTRY.match(s)
    return _indent(line) == 0 and s.endswith(":") and not (m and m.group("text"))


def _settings(lines: list[str]) -> list[dict[str, Any]]:
    """Setting lines, optionally under group lines (`Integration:` with the settings indented below it)."""
    lines = [ln for ln in textwrap.dedent("\n".join(lines)).splitlines() if ln.strip()]
    grouped = any(_is_group(ln) for ln in lines)
    entry_indent = min((_indent(ln) for ln in lines if not _is_group(ln)), default=0) if grouped else 0
    out: list[dict[str, Any]] = []
    group = "Settings"
    for line in lines:
        s = line.strip()
        if grouped and _is_group(line):
            group = s[:-1].strip() or "Settings"
            continue
        m = _ENTRY.match(s)
        if m and _indent(line) == entry_indent:
            out.append({"id": m.group("name"), "group": group, "type": (m.group("type") or "").strip() or None,
                        "text": m.group("text").strip()})
        elif out:
            out[-1]["text"] = (out[-1]["text"] + " " + s).strip()
    for o in out:
        text = o.pop("text")
        unit = None
        um = re.search(r"\[([^\]]+)\]", text)
        if um:
            unit = um.group(1).strip()
            text = (text[:um.start()] + text[um.end():]).replace("  ", " ").replace(" .", ".").strip()
        label, sep, help_ = text.partition(". ")
        o["label"] = label.strip().rstrip(".")
        o["help"] = help_.strip() if sep else ""
        if unit:
            o["unit"] = unit
    return out


# ---------------------------------------------------------------------------------------------------- values


def static_value(node: ast.AST | None, constants: dict[str, Any]) -> Any:
    """A default's value when it is written as a plain value (or names a module constant holding one)."""
    if node is None:
        return UNKNOWN
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    try:
        v = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return UNKNOWN
    return _plain(v)


def _plain(v: Any) -> Any:
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------------------------------------------- kinds


def is_arithmetic(node: ast.AST, formulas: set[str] = frozenset()) -> bool:
    """Only numbers, names, + - * / ** and maths functions (or other formulas) in this expression."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id in ("np", "numpy", "math") and \
            node.attr in ("pi", "e", "nan", "inf")
    if isinstance(node, ast.BinOp):
        return is_arithmetic(node.left, formulas) and is_arithmetic(node.right, formulas)
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, (ast.USub, ast.UAdd)) and is_arithmetic(node.operand, formulas)
    if isinstance(node, ast.Call):
        name = call_name(node.func)
        ok = name in ARITHMETIC_CALLS or (isinstance(node.func, ast.Name) and node.func.id in formulas)
        return ok and not node.keywords and all(is_arithmetic(a, formulas) for a in node.args)
    return False


def _top_is_arithmetic(node: ast.AST) -> bool:
    """The outermost operation is arithmetic (so the line looks like a formula, whatever is inside)."""
    if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.Call):
        return call_name(node.func) in ARITHMETIC_CALLS
    return False


def formula_shape(fn: ast.FunctionDef) -> bool:
    """A formula's shape: only `name = <arithmetic>` lines and a final `return <arithmetic>`."""
    body = body_statements(fn)
    if not body or not isinstance(body[-1], ast.Return) or body[-1].value is None:
        return False
    for st in body[:-1]:
        if not (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                and _top_is_arithmetic(st.value)):
            return False
    return _top_is_arithmetic(body[-1].value)


def _calls(fn: ast.FunctionDef, names: set[str]) -> list[str]:
    out: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names \
                and node.func.id not in out:
            out.append(node.func.id)
    return out


def parse(source: str) -> Method:
    """Read a method file. Raises SyntaxError for code Python can't read."""
    tree = ast.parse(source)
    module_doc = ast.get_docstring(tree) or ""
    first, _, rest = module_doc.strip().partition("\n")
    constants: dict[str, Any] = {}
    recipe = None
    database = None
    imports: list[dict[str, Any]] = []
    defs: list[ast.FunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = static_value(node.value, constants)
            if v is not UNKNOWN:
                constants[node.targets[0].id] = v
            if node.targets[0].id == "RECIPE" and isinstance(v, str):
                recipe = v
            if node.targets[0].id == "DATABASE" and isinstance(v, list):
                database = [str(x) for x in v]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imports.append({"module": a.name, "names": [], "line": node.lineno, "as": a.asname})
            else:
                imports.append({"module": ("." * node.level) + (node.module or ""),
                                "names": [a.name for a in node.names], "line": node.lineno})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(node)
    if recipe is None and defs:
        recipe = defs[-1].name
    names = {d.name for d in defs}
    lines = source.splitlines()
    recipe_node = next((d for d in defs if d.name == recipe), None)
    steps = set(_calls(recipe_node, names)) - {recipe} if recipe_node else set()
    functions = []
    for d in defs:
        shaped = formula_shape(d)
        kind = "recipe" if d.name == recipe else "step" if d.name in steps else "formula" if shaped else "helper"
        args = d.args.args + d.args.kwonlyargs
        params = [a.arg for a in args]
        defaults: dict[str, Any] = {}
        pos_defaults = d.args.defaults
        for a, dv in zip(d.args.args[len(d.args.args) - len(pos_defaults):], pos_defaults):
            defaults[a.arg] = static_value(dv, constants)
        for a, dv in zip(d.args.kwonlyargs, d.args.kw_defaults):
            if dv is not None:
                defaults[a.arg] = static_value(dv, constants)
        doc = ast.get_docstring(d)
        start = (d.decorator_list[0].lineno if d.decorator_list else d.lineno)
        end = d.end_lineno or d.lineno
        functions.append(Function(name=d.name, line=start, end_line=end, kind=kind, params=params,
                                  defaults=defaults, doc=parse_doc(doc), has_doc=bool(doc and doc.strip()), node=d,
                                  source="\n".join(lines[start - 1:end]), calls=_calls(d, names),
                                  formula_shaped=shaped))
    return Method(name=first.strip(), description=" ".join(rest.split()), functions=functions, recipe=recipe,
                  constants=constants, database=database, imports=imports, tree=tree)


# ---------------------------------------------------------------------------------------------------- the form


def is_input(param: str) -> bool:
    return param.endswith("_file") or param == "corrections"


def _editor(param: str, default: Any, explicit: str | None, unit: str | None) -> str:
    if explicit and explicit in EDITORS:
        return explicit
    if param in SPECIAL_EDITORS:
        return SPECIAL_EDITORS[param]
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, int):
        return "integer"
    if isinstance(default, float):
        return "number"
    if isinstance(default, list):
        return "object" if any(isinstance(x, (dict, list)) for x in default) else "list"
    if isinstance(default, dict):
        return "object"
    if default is None or default is UNKNOWN:
        return "number" if unit else "text"
    return "text"


def settings(method: Method) -> list[dict[str, Any]]:
    """The recipe's settings for the experiment's form: id, type, label, default, group, help, unit, required."""
    fn = method.recipe_function
    if fn is None:
        return []
    documented = {s["id"]: s for s in fn.doc.settings}
    order = [s["id"] for s in fn.doc.settings if s["id"] in fn.params] + \
        [p for p in fn.params if p not in documented]
    out = []
    for pid in order:
        if is_input(pid):
            continue
        d = documented.get(pid, {})
        _, unit = split_unit(pid)
        unit = d.get("unit") or unit
        default = fn.defaults.get(pid, UNKNOWN)
        has_default = pid in fn.defaults
        item = {"id": pid, "type": _editor(pid, default, d.get("type"), unit),
                "label": d.get("label") or pid.replace("_", " "),
                "default": None if default is UNKNOWN else default, "group": d.get("group") or "Other settings",
                "required": not has_default}
        if d.get("help"):
            item["help"] = d["help"]
        if unit:
            item["unit"] = unit
        if has_default and default is UNKNOWN:
            item["computed_default"] = ast.unparse(_default_node(fn, pid))
        out.append(item)
    return out


def _default_node(fn: Function, pid: str) -> ast.AST:
    args = fn.node.args
    for a, dv in zip(args.args[len(args.args) - len(args.defaults):], args.defaults):
        if a.arg == pid:
            return dv
    for a, dv in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg == pid and dv is not None:
            return dv
    return ast.Constant(None)


def inputs(method: Method) -> list[dict[str, Any]]:
    """The raw files the recipe takes: [{id, kind: file, label, types}]."""
    fn = method.recipe_function
    if fn is None:
        return []
    out = []
    for p in fn.params:
        if p.endswith("_file"):
            text = fn.doc.inputs.get(p, "")
            types = re.findall(r"\.[A-Za-z0-9]+", (re.search(r"\[([^\]]*)\]\s*$", text) or [None, ""])[1] or "")
            label = re.sub(r"\s*\[[^\]]*\]\s*$", "", text).strip() or p.replace("_", " ")
            out.append({"id": p, "kind": "file", "label": label, "types": types,
                        "optional": p in fn.defaults})
    return out


def outline(method: Method) -> list[dict[str, Any]]:
    """Every function in file order: name, kind, lines, sentence, parameters."""
    return [{"name": f.name, "kind": f.kind, "line": f.line, "end_line": f.end_line, "sentence": f.doc.sentence,
             "params": f.params, "calls": f.calls} for f in method.functions]


def summary(method: Method) -> dict[str, Any]:
    return {"name": method.name, "description": method.description, "recipe": method.recipe,
            "outline": outline(method), "settings": settings(method), "inputs": inputs(method),
            "outputs": (method.recipe_function.doc.returns if method.recipe_function else {}),
            "database": method.database,
            "steps": sum(1 for f in method.functions if f.kind == "step")}


def unit_of(name: str) -> str | None:
    return split_unit(name)[1]


__all__ = ["parse", "settings", "inputs", "outline", "summary", "UNKNOWN", "is_input", "is_arithmetic", "UNITS"]

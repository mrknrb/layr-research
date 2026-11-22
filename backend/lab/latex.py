"""Python code drawn as typeset math: LaTeX that both KaTeX (the panel) and matplotlib's mathtext (reports) draw.

A formula function's body becomes equations: `return rate_mM_per_min * 1000 / cells_g_per_L` in a function
`specific_rate` is drawn as specific rate = rate·1000 / cells, with the units from the names
(`rate_mM_per_min` → rate [mM/min]). With a recorded call's numbers the same equation is drawn again with the
numbers put in. Only drawing happens here: the numbers come from the run, nothing is computed for the result.
"""

from __future__ import annotations

import ast
import math
from typing import Any

UNITS = {
    "mM": "mM", "uM": "µM", "nM": "nM", "M": "M", "mol": "mol", "mmol": "mmol", "umol": "µmol", "nmol": "nmol",
    "min": "min", "s": "s", "h": "h", "ms": "ms", "d": "d",
    "L": "L", "l": "L", "mL": "mL", "ml": "mL", "uL": "µL", "ul": "µL",
    "g": "g", "mg": "mg", "ug": "µg", "kg": "kg", "gCDW": "gCDW", "gcdw": "gCDW", "gDW": "gDW",
    "U": "U", "pA": "pA", "mAU": "mAU", "pct": "%", "percent": "%", "nm": "nm", "um": "µm", "mm": "mm",
    "degC": "°C", "K": "K", "rpm": "rpm", "OD": "OD", "Da": "Da", "kDa": "kDa", "bar": "bar",
}
GREEK = {"alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "kappa", "lambda", "mu", "nu", "xi",
         "pi", "rho", "sigma", "tau", "phi", "chi", "psi", "omega", "Delta", "Sigma", "Omega", "Gamma", "Lambda"}
MATH_MODULES = {"np", "numpy", "math", "statistics", "scipy", "pd", "pandas"}
TRANSPARENT = {"float", "int"}


class DrawError(ValueError):
    """Code that can't be drawn as a formula."""


# ---------------------------------------------------------------------------------------------------- numbers, names


def num(v: Any, digits: int = 4) -> str:
    """A number for a formula: 4 significant digits, a power of ten for very large or small ones."""
    if v is None:
        return r"\mathrm{n/a}"
    if isinstance(v, bool):
        return r"\mathrm{true}" if v else r"\mathrm{false}"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return text(str(v))
    if f != f:
        return r"\mathrm{NaN}"
    if math.isinf(f):
        return r"\infty" if f > 0 else r"-\infty"
    if f == 0:
        return "0"
    s = f"{f:.{digits}g}"
    if "e" in s:
        m, e = s.split("e")
        return rf"{m} \times 10^{{{int(e)}}}"
    return s


def constant(v: Any) -> str:
    """A number written in the code (kept as written: 3.3, 1000, 1.05)."""
    if isinstance(v, bool):
        return num(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = repr(v)
        if "e" in s or len(s) > 12:
            return num(v, 6)
        return s
    return text(str(v))


def text(s: str) -> str:
    """Words as upright text inside a formula."""
    safe = str(s).replace("\\", "").replace("{", "").replace("}", "").replace("$", "")
    for a, b in (("_", r"\_"), ("%", r"\%"), ("#", r"\#"), ("&", r"\&"), ("^", ""), ("~", "")):
        safe = safe.replace(a, b)
    safe = safe.replace(" ", r"\ ")
    return rf"\mathrm{{{safe}}}" if safe else ""


def _is_unit(token: str) -> bool:
    return token in UNITS


def split_unit(name: str) -> tuple[list[str], str | None]:
    """A name's words and its unit, read from the end of the name: rate_mM_per_min → (["rate"], "mM/min")."""
    tokens = [t for t in name.split("_") if t]
    for i in range(1, len(tokens)):
        tail = tokens[i:]
        if _is_unit(tail[0]) and tail[-1] != "per" and all(_is_unit(t) or t == "per" for t in tail):
            unit = "".join("/" if t == "per" else UNITS[t] for t in tail)
            return tokens[:i], unit
    return tokens, None


def unit_tex(unit: str) -> str:
    parts = []
    for i, piece in enumerate(unit.split("µ")):
        if i:
            parts.append(r"\mu ")
        piece = piece.replace("%", r"\%").replace("°C", r"{}^{\circ}C")
        if piece:
            parts.append(rf"\mathrm{{{piece}}}")
    return "".join(parts)


def var(name: str) -> str:
    """A variable's name as math: single letters in italics, words upright, the unit as a small bracketed index."""
    name = name.lstrip("_") or name
    if name.endswith("_mean") and len(name) > 5:
        return rf"\overline{{{var(name[:-5])}}}"
    words, unit = split_unit(name)
    if not words:
        words = [name]
    if len(words) == 1 and words[0] in GREEK:
        base = "\\" + words[0]
    elif len(words) == 1 and len(words[0]) == 1:
        base = words[0]
    elif len(words) == 2 and len(words[0]) == 1 and len(words[1]) <= 3:
        base = rf"{words[0]}_{{{text(words[1])}}}"
    else:
        base = text(" ".join(words))
    if unit:
        return rf"{base}_{{[{unit_tex(unit)}]}}"
    return base


# ---------------------------------------------------------------------------------------------------- expressions

_PREC = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2, ast.FloorDiv: 2, ast.Mod: 2, ast.MatMult: 2, ast.Pow: 4}


def call_name(func: ast.AST) -> str | None:
    """The name of a called function: sqrt for sqrt(x), np.sqrt(x) and math.sqrt(x); None for other calls."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in MATH_MODULES:
        return func.attr
    return None


def _wrap(s: str) -> str:
    return rf"\left({s}\right)"


class Drawer:
    """Draws one expression; `values` puts numbers in place of names (a recorded call's inputs)."""

    def __init__(self, values: dict[str, Any] | None = None):
        self.values = values

    def draw(self, node: ast.AST) -> str:
        return self._tex(node)[0]

    def _value(self, key: str) -> tuple[str, int] | None:
        if self.values is None or key not in self.values:
            return None
        v = self.values[key]
        s = num(v)
        try:
            negative = float(v) < 0
        except (TypeError, ValueError):
            negative = False
        return (s, 3) if negative else (s, 9)

    def _tex(self, node: ast.AST) -> tuple[str, int]:
        """(latex, precedence): 9 = atomic, 4 = power, 3 = unary minus, 2 = product, 1 = sum, 0 = comparison."""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return constant(node.value), 9 if node.value >= 0 else 3
            if isinstance(node.value, str):
                return text(f"“{node.value}”"), 9
            return num(node.value), 9
        if isinstance(node, ast.Name):
            got = self._value(node.id)
            if got:
                return got
            if node.id in ("pi",):
                return r"\pi", 9
            return var(node.id), 9
        if isinstance(node, ast.Attribute):
            key = ast.unparse(node)
            got = self._value(key)
            if got:
                return got
            if isinstance(node.value, ast.Name) and node.value.id in MATH_MODULES:
                if node.attr == "pi":
                    return r"\pi", 9
                if node.attr == "e":
                    return "e", 9
                if node.attr in ("nan", "NaN"):
                    return r"\mathrm{NaN}", 9
                if node.attr == "inf":
                    return r"\infty", 9
            return rf"{var(node.attr)}_{{{text(ast.unparse(node.value))}}}", 9
        if isinstance(node, ast.BinOp):
            return self._binop(node)
        if isinstance(node, ast.UnaryOp):
            v, pv = self._tex(node.operand)
            if isinstance(node.op, ast.Not):
                return rf"\mathrm{{not}}\ {v}", 3
            if pv < 4:
                v = _wrap(v)
            return ("-" if isinstance(node.op, ast.USub) else "+") + v, 3
        if isinstance(node, ast.BoolOp):
            word = r"\ \mathrm{and}\ " if isinstance(node.op, ast.And) else r"\ \mathrm{or}\ "
            return word.join(self._tex(v)[0] for v in node.values), 0
        if isinstance(node, ast.Compare):
            syms = {ast.Eq: "=", ast.NotEq: r"\neq", ast.Lt: "<", ast.LtE: r"\leq", ast.Gt: ">", ast.GtE: r"\geq",
                    ast.In: r"\in", ast.NotIn: r"\notin", ast.Is: "=", ast.IsNot: r"\neq"}
            parts = [self._tex(node.left)[0]]
            for op, comp in zip(node.ops, node.comparators):
                parts += [syms[type(op)], self._tex(comp)[0]]
            return " ".join(parts), 0
        if isinstance(node, ast.IfExp):
            return (rf"{self.draw(node.body)}\ \mathrm{{if}}\ {self.draw(node.test)},\ \mathrm{{else}}\ "
                    rf"{self.draw(node.orelse)}"), 0
        if isinstance(node, ast.Subscript):
            v, _ = self._tex(node.value)
            return rf"{v}_{{{self.draw(node.slice)}}}", 9
        if isinstance(node, ast.Tuple):
            return _wrap(", ".join(self.draw(e) for e in node.elts)), 9
        if isinstance(node, ast.Call):
            return self._call(node)
        raise DrawError(f"'{ast.unparse(node)}' can't be drawn as a formula.")

    def _binop(self, node: ast.BinOp) -> tuple[str, int]:
        p = _PREC[type(node.op)]
        if isinstance(node.op, ast.Div):
            return rf"\frac{{{self.draw(node.left)}}}{{{self.draw(node.right)}}}", 9
        a, pa = self._tex(node.left)
        b, pb = self._tex(node.right)
        if isinstance(node.op, ast.Pow):
            if pa < 9:
                a = _wrap(a)
            return rf"{{{a}}}^{{{b}}}", 4
        if pa < p:
            a = _wrap(a)
        if pb < p or (pb == p and isinstance(node.op, (ast.Sub, ast.Mod, ast.FloorDiv))) or (pb == 3 and p >= 1):
            b = _wrap(b)
        sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: r"\cdot", ast.MatMult: r"\cdot", ast.Mod: r"\bmod",
               ast.FloorDiv: r"\mathrm{div}"}[type(node.op)]
        return f"{a} {sym} {b}", p

    def _call(self, node: ast.Call) -> tuple[str, int]:
        name = call_name(node.func)
        args = node.args
        if name in TRANSPARENT and len(args) == 1:
            return self._tex(args[0])
        if name and len(args) == 1:
            a, pa = self._tex(args[0])
            simple = {
                "sqrt": rf"\sqrt{{{a}}}", "abs": rf"\left|{a}\right|", "fabs": rf"\left|{a}\right|",
                "exp": rf"e^{{{a}}}", "log": rf"\ln{_wrap(a)}", "ln": rf"\ln{_wrap(a)}",
                "log10": rf"\log_{{10}}{_wrap(a)}", "log2": rf"\log_{{2}}{_wrap(a)}",
                "mean": rf"\overline{{{a}}}", "nanmean": rf"\overline{{{a}}}",
                "median": rf"\mathrm{{median}}{_wrap(a)}", "len": rf"n{_wrap(a)}",
                "floor": rf"\lfloor {a} \rfloor", "ceil": rf"\lceil {a} \rceil",
            }
            if name in simple:
                return simple[name], 9
            if name in ("sum", "nansum"):
                return rf"\sum {a if pa >= 2 else _wrap(a)}", 2
        if name in ("min", "max", "minimum", "maximum") and args:
            fn = r"\min" if name.startswith("min") else r"\max"
            return fn + _wrap(", ".join(self.draw(a) for a in args)), 9
        if name == "round" and args:
            return r"\mathrm{round}" + _wrap(", ".join(self.draw(a) for a in args)), 9
        label = var(name) if name else text(ast.unparse(node.func))
        inner = [self.draw(a) for a in args] + [f"{text(k.arg)}={self.draw(k.value)}" for k in node.keywords if k.arg]
        return label + _wrap(", ".join(inner)), 9


def draw(node: ast.AST, values: dict[str, Any] | None = None) -> str:
    return Drawer(values).draw(node)


def draw_code(code: str, values: dict[str, Any] | None = None) -> str:
    """One line of Python (an expression or `name = expression`) as LaTeX."""
    tree = ast.parse(code.strip(), mode="exec")
    if len(tree.body) != 1:
        raise DrawError("One line, one formula.")
    st = tree.body[0]
    if isinstance(st, ast.Assign) and len(st.targets) == 1:
        return f"{draw(st.targets[0], None)} = {draw(st.value, values)}"
    if isinstance(st, ast.Expr):
        return draw(st.value, values)
    raise DrawError(f"'{code}' is not a formula.")


# ---------------------------------------------------------------------------------------------------- evaluating


_FUNCS = {"sqrt": math.sqrt, "abs": abs, "fabs": abs, "exp": math.exp, "log": math.log, "ln": math.log,
          "log10": math.log10, "log2": math.log2, "min": min, "max": max, "round": round, "float": float, "int": int}


def evaluate(node: ast.AST, env: dict[str, Any]) -> float:
    """The value of an arithmetic expression for numbers in `env` (for intermediate lines of a formula only)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        key = node.id if isinstance(node, ast.Name) else ast.unparse(node)
        if key in env and isinstance(env[key], (int, float)):
            return env[key]
        if isinstance(node, ast.Attribute) and node.attr == "pi":
            return math.pi
        raise DrawError(f"no number for {key}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = evaluate(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = evaluate(node.left, env), evaluate(node.right, env)
        ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.Div: lambda: a / b,
               ast.Pow: lambda: a**b, ast.FloorDiv: lambda: a // b, ast.Mod: lambda: a % b}
        if type(node.op) in ops:
            return ops[type(node.op)]()
    if isinstance(node, ast.Call):
        name = call_name(node.func)
        if name in _FUNCS and not node.keywords:
            return _FUNCS[name](*[evaluate(a, env) for a in node.args])
    raise DrawError(f"can't evaluate {ast.unparse(node)}")


# ---------------------------------------------------------------------------------------------------- functions


def body_statements(fn: ast.FunctionDef) -> list[ast.stmt]:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def draw_function(fn: ast.FunctionDef, values: dict[str, Any] | None = None, result: Any = None,
                  with_result: bool = False) -> list[str]:
    """A formula function's body as equations, one per line: assignments, then the function's name = its return.

    With `values` (the call's inputs) the numbers are put in, and the intermediate lines are worked out from them;
    `result` (the recorded result) closes the last line.
    """
    lines = []
    env = dict(values) if values is not None else None
    for st in body_statements(fn):
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name = st.targets[0].id
            if env is None:
                lines.append(f"{var(name)} = {draw(st.value)}")
                continue
            line = f"{var(name)} = {draw(st.value, env)}"
            try:
                env[name] = evaluate(st.value, env)
                line += f" = {num(env[name])}"
            except (DrawError, ArithmeticError, ValueError, TypeError):
                pass
            lines.append(line)
        elif isinstance(st, ast.Return) and st.value is not None:
            line = f"{var(fn.name)} = {draw(st.value, env)}"
            if with_result:
                line += f" = {num(result)}"
            lines.append(line)
        else:
            raise DrawError(f"Line {st.lineno} is not a formula line.")
    return lines


def draw_formula_lines(lines: list[str]) -> list[dict[str, str]]:
    """A docstring's Formula: block, one line each: Python (drawn) or $LaTeX$ (as written)."""
    out = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("$") and s.endswith("$") and len(s) > 1:
            out.append({"code": s, "latex": s.strip("$")})
            continue
        try:
            out.append({"code": s, "latex": draw_code(s)})
        except (SyntaxError, DrawError, KeyError):
            out.append({"code": s, "latex": text(s)})
    return out

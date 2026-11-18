"""A small, safe formula language over table columns, shown as typeset math.

    rel = od600 / mean(od600[treatment == "control"])
    log_ratio = log2(`dry mass` / 0.5)

- Columns by name; names with spaces or symbols in backticks. Numbers, "text", ^ or ** for powers.
- + - * / ^, comparisons (== != < <= > >=), and / or / not.
- Functions: ln, log10, log2, exp, sqrt, abs, round(x, digits), ifelse(condition, a, b);
  aggregates over the column (or over each group when the step groups): mean, median, sd, sem, var, sum, min, max,
  n. `x[condition]` keeps the rows where the condition holds, e.g. mean(x[group == "control"]).

Parsed with Python's `ast` and checked against this whitelist; nothing is ever passed to eval. The same tree is
evaluated with pandas and rendered as LaTeX, so what is shown is exactly what is computed.
"""

from __future__ import annotations

import ast
import re
from typing import Any

import numpy as np
import pandas as pd

from .core import StepError, num, tex_name

AGGREGATES = {"mean", "median", "sd", "sem", "var", "sum", "min", "max", "n"}
SCALAR_FUNCS = {"ln", "log10", "log2", "exp", "sqrt", "abs", "round", "ifelse"}
FUNCS = AGGREGATES | SCALAR_FUNCS

_BACKTICK = re.compile(r"`([^`]+)`")


class Formula:
    def __init__(self, text: str):
        self.text = (text or "").strip()
        if not self.text:
            raise StepError("The formula is empty.")
        self.aliases: dict[str, str] = {}

        def repl(m: re.Match) -> str:
            key = f"__c{len(self.aliases)}"
            self.aliases[key] = m.group(1)
            return key

        src = _BACKTICK.sub(repl, self.text).replace("^", "**")
        try:
            tree = ast.parse(src, mode="eval")
        except SyntaxError as e:
            raise StepError(f"Can't read the formula '{self.text}': {e.msg} near position {e.offset}.") from e
        self.tree = tree.body
        self._check(self.tree)

    # ------------------------------------------------------------------ checking

    def _check(self, node: ast.AST, in_agg: bool = False) -> None:
        ok = (ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.Call, ast.Name, ast.Constant, ast.Subscript,
              ast.Load, ast.operator, ast.unaryop, ast.cmpop, ast.boolop)
        if not isinstance(node, ok):
            raise StepError(f"'{ast.unparse(node)}' is not allowed in a formula.")
        if isinstance(node, ast.BinOp) and not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            raise StepError("Only + - * / and ^ are allowed between values.")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str)):
            raise StepError(f"'{node.value!r}' is not a number or text.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS or node.keywords:
                name = ast.unparse(node.func)
                raise StepError(f"Unknown function '{name}'. Functions: {', '.join(sorted(FUNCS))}.")
            if node.func.id in AGGREGATES and len(node.args) != 1:
                raise StepError(f"{node.func.id}(...) takes one column.")
            if node.func.id == "ifelse" and len(node.args) != 3:
                raise StepError("ifelse(condition, value if true, value if false) takes three values.")
            for a in node.args:
                self._check(a, in_agg or node.func.id in AGGREGATES)
            return
        if isinstance(node, ast.Subscript):
            self._check(node.value, in_agg)
            self._check(node.slice, in_agg)
            return
        for child in ast.iter_child_nodes(node):
            self._check(child, in_agg)

    def columns(self) -> list[str]:
        funcs = {id(n.func) for n in ast.walk(self.tree) if isinstance(n, ast.Call)}
        out: list[str] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and id(node) not in funcs:
                name = self.aliases.get(node.id, node.id)
                if name not in out:
                    out.append(name)
        return out

    # ------------------------------------------------------------------ evaluating

    def evaluate(self, df: pd.DataFrame, by: list[str] | None = None) -> pd.Series | Any:
        missing = [c for c in self.columns() if c not in df.columns]
        if missing:
            have = ", ".join(map(str, df.columns))
            raise StepError(f"No column {', '.join(repr(m) for m in missing)}. Columns: {have}.")
        if by:
            parts = [self._as_series(self._ev(self.tree, g), g) for _, g in df.groupby(by, sort=False, dropna=False)]
            return pd.concat(parts).reindex(df.index) if parts else pd.Series(dtype=float)
        return self._ev(self.tree, df)

    @staticmethod
    def _as_series(v: Any, df: pd.DataFrame) -> pd.Series:
        return v if isinstance(v, pd.Series) else pd.Series(v, index=df.index)

    def _ev(self, node: ast.AST, df: pd.DataFrame) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return df[self.aliases.get(node.id, node.id)]
        if isinstance(node, ast.BinOp):
            a, b = self._ev(node.left, df), self._ev(node.right, df)
            op = node.op
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.Div):
                return a / b
            return a**b
        if isinstance(node, ast.UnaryOp):
            v = self._ev(node.operand, df)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
            return ~v if isinstance(v, pd.Series) else (not v)
        if isinstance(node, ast.BoolOp):
            vals = [self._ev(v, df) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.Compare):
            left = self._ev(node.left, df)
            result = None
            for op, comp in zip(node.ops, node.comparators):
                right = self._ev(comp, df)
                r = {ast.Eq: lambda x, y: x == y, ast.NotEq: lambda x, y: x != y, ast.Lt: lambda x, y: x < y,
                     ast.LtE: lambda x, y: x <= y, ast.Gt: lambda x, y: x > y, ast.GtE: lambda x, y: x >= y}
                f = r.get(type(op))
                if f is None:
                    raise StepError("Only == != < <= > >= compare values.")
                cur = f(left, right)
                result = cur if result is None else (result & cur)
                left = right
            return result
        if isinstance(node, ast.Subscript):
            v = self._ev(node.value, df)
            cond = self._ev(node.slice, df)
            if not isinstance(v, pd.Series):
                raise StepError("Only a column can be filtered with [condition].")
            return v[cond] if isinstance(cond, pd.Series) else (v if cond else v.iloc[0:0])
        if isinstance(node, ast.Call):
            name = node.func.id
            args = [self._ev(a, df) for a in node.args]
            if name in AGGREGATES:
                s = args[0]
                s = s if isinstance(s, pd.Series) else pd.Series([s])
                s = pd.to_numeric(s, errors="coerce").dropna()
                return {"mean": s.mean, "median": s.median, "sd": lambda: s.std(ddof=1), "var": lambda: s.var(ddof=1),
                        "sem": lambda: s.std(ddof=1) / np.sqrt(len(s)) if len(s) else np.nan, "sum": s.sum,
                        "min": s.min, "max": s.max, "n": lambda: float(len(s))}[name]()
            if name == "ifelse":
                c, a, b = args
                if isinstance(c, pd.Series):
                    return pd.Series(np.where(c, a, b), index=c.index)
                return a if c else b
            if name == "round":
                return np.round(args[0], int(args[1]) if len(args) > 1 else 0)
            f = {"ln": np.log, "log10": np.log10, "log2": np.log2, "exp": np.exp, "sqrt": np.sqrt, "abs": np.abs}[name]
            return f(args[0])
        raise StepError(f"'{ast.unparse(node)}' is not allowed in a formula.")

    # ------------------------------------------------------------------ LaTeX

    def latex(self, row: dict | None = None, df: pd.DataFrame | None = None) -> str:
        """The formula as LaTeX; with `row` (and `df` for aggregates) the same formula with the values put in."""
        return self._tex(self.tree, row, df)[0]

    _PREC = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2, ast.Pow: 4}

    def _name(self, node: ast.Name) -> str:
        return tex_name(self.aliases.get(node.id, node.id))

    def _tex(self, node: ast.AST, row: dict | None, df: pd.DataFrame | None) -> tuple[str, int]:
        """(latex, precedence): 9 = atomic."""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return tex_name(node.value), 9
            return num(node.value, 6), 9
        if isinstance(node, ast.Name):
            if row is not None:
                return num(row.get(self.aliases.get(node.id, node.id))), 9
            return self._name(node), 9
        if isinstance(node, ast.BinOp):
            p = self._PREC[type(node.op)]
            if isinstance(node.op, ast.Div):
                a, _ = self._tex(node.left, row, df)
                b, _ = self._tex(node.right, row, df)
                return rf"\frac{{{a}}}{{{b}}}", 9
            a, pa = self._tex(node.left, row, df)
            b, pb = self._tex(node.right, row, df)
            if isinstance(node.op, ast.Pow):
                if pa < 9:
                    a = rf"\left({a}\right)"
                return rf"{{{a}}}^{{{b}}}", 4
            if pa < p:
                a = rf"\left({a}\right)"
            if pb < p or (pb == p and isinstance(node.op, ast.Sub)):
                b = rf"\left({b}\right)"
            sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: r"\cdot"}[type(node.op)]
            return f"{a} {sym} {b}", p
        if isinstance(node, ast.UnaryOp):
            v, pv = self._tex(node.operand, row, df)
            if isinstance(node.op, ast.Not):
                return rf"\neg {v}", 3
            if pv < 3:
                v = rf"\left({v}\right)"
            return ("-" if isinstance(node.op, ast.USub) else "+") + v, 3
        if isinstance(node, ast.BoolOp):
            word = r"\ \mathrm{and}\ " if isinstance(node.op, ast.And) else r"\ \mathrm{or}\ "
            return word.join(self._tex(v, row, df)[0] for v in node.values), 0
        if isinstance(node, ast.Compare):
            syms = {ast.Eq: "=", ast.NotEq: r"\neq", ast.Lt: "<", ast.LtE: r"\leq", ast.Gt: ">", ast.GtE: r"\geq"}
            parts = [self._tex(node.left, row, df)[0]]
            for op, comp in zip(node.ops, node.comparators):
                parts += [syms[type(op)], self._tex(comp, row, df)[0]]
            return " ".join(parts), 0
        if isinstance(node, ast.Subscript):
            v, _ = self._tex(node.value, None, None)
            cond, _ = self._tex(node.slice, None, None)
            return rf"{v}_{{{cond}}}", 9
        if isinstance(node, ast.Call):
            name = node.func.id
            if name in AGGREGATES and row is not None and df is not None:
                return num(self._ev(node, df)), 9
            if name == "mean" and isinstance(node.args[0], ast.Subscript):  # the bar over the column only
                v, _ = self._tex(node.args[0].value, None, None)
                cond, _ = self._tex(node.args[0].slice, None, None)
                return rf"\overline{{{v}}}_{{{cond}}}", 9
            if name in AGGREGATES:
                a, _ = self._tex(node.args[0], None, None)
                return {
                    "mean": rf"\overline{{{a}}}", "median": rf"\mathrm{{median}}\left({a}\right)",
                    "sd": rf"s\left({a}\right)", "var": rf"s^2\left({a}\right)",
                    "sem": rf"\mathrm{{SEM}}\left({a}\right)",
                    "sum": rf"\sum {a}", "min": rf"\min\left({a}\right)", "max": rf"\max\left({a}\right)",
                    "n": rf"n\left({a}\right)",
                }[name], 9
            args = [self._tex(a, row, df)[0] for a in node.args]
            if name == "sqrt":
                return rf"\sqrt{{{args[0]}}}", 9
            if name == "exp":
                return rf"e^{{{args[0]}}}", 9
            if name == "abs":
                return rf"\left|{args[0]}\right|", 9
            if name == "ln":
                return rf"\ln\left({args[0]}\right)", 9
            if name in ("log10", "log2"):
                return rf"\log_{{{name[3:]}}}\left({args[0]}\right)", 9
            if name == "round":
                return rf"\mathrm{{round}}\left({', '.join(args)}\right)", 9
            if name == "ifelse":
                return rf"{args[1]}\ \mathrm{{if}}\ {args[0]},\ \mathrm{{else}}\ {args[2]}", 0
        raise StepError(f"Can't draw '{ast.unparse(node)}'.")


def legend(f: Formula) -> list[dict[str, str]]:
    """Symbols of a formula: each column it uses, and the functions."""
    out = [{"symbol": tex_name(c), "meaning": f"column {c}", "unit": ""} for c in f.columns()]
    used = {n.func.id for n in ast.walk(f.tree) if isinstance(n, ast.Call)}
    meanings = {"mean": (r"\overline{x}", "mean of x"), "sd": ("s(x)", "sample standard deviation (n - 1)"),
                "sem": (r"\mathrm{SEM}(x)", "standard error of the mean, s/√n"), "n": ("n(x)", "number of values"),
                "var": (r"s^2(x)", "sample variance (n - 1)")}
    for k, (sym, meaning) in meanings.items():
        if k in used:
            out.append({"symbol": sym, "meaning": meaning, "unit": ""})
    if any(isinstance(n, ast.Subscript) for n in ast.walk(f.tree)):
        out.append({"symbol": r"x_{\mathrm{condition}}", "meaning": "only the rows where the condition holds",
                    "unit": ""})
    return out

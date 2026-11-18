"""Step types: the building blocks of methods, each with its own explanation.

A step type is a small, named computation with typed inputs, outputs and parameters. Every step type also says,
in words and formulas, what it computes (`explain`) and writes its part of the Methods paragraph (`methods`), so a
method built from steps can always be read like a methods section. A run of a step returns its outputs plus
`facts`: this run's numbers for the explanation (a formula with real values substituted, counts, fit results).

Port kinds: `file` (a raw file in the store), `injections` (chromatograms, kept in memory only), `table` (a pandas
DataFrame, saved as CSV with each run), `value` (JSON), `corrections` (the experiment's approved corrections),
`figure` (PNG bytes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

PORT_KINDS = ("file", "injections", "table", "value", "corrections", "figure")


class StepError(ValueError):
    """A problem with a step's inputs or parameters, said so a person (or an AI) can fix it."""


@dataclass
class Port:
    id: str
    kind: str
    label: str
    optional: bool = False

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "optional": self.optional}


@dataclass
class Param:
    id: str
    type: str  # number, integer, text, boolean, choice, list, object, formula, columns
    label: str
    default: Any = None
    help: str = ""
    unit: str = ""
    choices: list[str] | None = None

    def describe(self) -> dict[str, Any]:
        d = {"id": self.id, "type": self.type, "label": self.label, "default": self.default}
        for k in ("help", "unit", "choices"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


@dataclass
class StepOut:
    outputs: dict[str, Any]
    facts: dict[str, Any] = field(default_factory=dict)  # numbers for the run-mode explanation
    flags: list[dict[str, Any]] = field(default_factory=list)  # {level, category, injection, compound, message}


@dataclass
class Ctx:
    """What a running step may use besides its inputs."""

    cache: dict[Any, Any]
    store_path: Callable[[str], Any]  # a store-relative file path -> absolute Path
    progress: Callable[[str], None] = lambda _msg: None
    run_method: Callable[..., Any] | None = None  # for steps that run another method


@dataclass
class StepType:
    id: str
    title: str
    category: str
    sentence: str  # one plain sentence: what it does
    inputs: list[Port]
    outputs: list[Port]
    params: list[Param]
    run: Callable[[Ctx, dict[str, Any], dict[str, Any]], StepOut]
    explain: Callable[[dict[str, Any]], dict[str, Any]]  # general sheet parts for these parameters
    methods: Callable[[dict[str, Any], dict[str, Any]], str] | None = None  # Methods paragraph sentence
    example: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, str]]] | None = None  # run-mode formulas

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.id,
            "title": self.title,
            "category": self.category,
            "sentence": self.sentence,
            "inputs": [p.describe() for p in self.inputs],
            "outputs": [p.describe() for p in self.outputs],
            "params": [p.describe() for p in self.params],
        }

    def defaults(self) -> dict[str, Any]:
        return {p.id: p.default for p in self.params}


REGISTRY: dict[str, StepType] = {}


def register(step: StepType) -> StepType:
    if step.id in REGISTRY:
        raise RuntimeError(f"step type {step.id} registered twice")
    for p in step.inputs + step.outputs:
        if p.kind not in PORT_KINDS:
            raise RuntimeError(f"{step.id}: unknown port kind {p.kind}")
    REGISTRY[step.id] = step
    return step


def step_type(type_id: str) -> StepType:
    st = REGISTRY.get(type_id)
    if st is None:
        raise StepError(f"No step type '{type_id}'. Step types: {', '.join(sorted(REGISTRY))}.")
    return st


def sheet(
    *,
    formulas: list[dict[str, str]] | None = None,
    symbols: list[dict[str, str]] | None = None,
    rules: list[str] | None = None,
    assumptions: list[str] | None = None,
    code: str = "",
    reference: str = "",
) -> dict[str, Any]:
    """The general part of an explanation sheet. Formulas are LaTeX (the subset KaTeX and matplotlib both draw)."""
    return {
        "formulas": formulas or [],
        "symbols": symbols or [],
        "rules": rules or [],
        "assumptions": assumptions or [],
        "code": code.strip(),
        "reference": reference,
    }


def num(v: Any, digits: int = 4) -> str:
    """A number for a formula: 4 significant digits, no trailing noise, LaTeX minus sign as is."""
    if v is None:
        return r"\mathrm{n/a}"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return tex_name(v)
    if f != f:
        return r"\mathrm{n/a}"
    if f == 0:
        return "0"
    s = f"{f:.{digits}g}"
    if "e" in s:
        m, e = s.split("e")
        return rf"{m} \times 10^{{{int(e)}}}"
    return s


def tex_name(name: str) -> str:
    """A column or compound name as an upright LaTeX symbol (underscores and spaces escaped)."""
    safe = str(name).replace("\\", "").replace("{", "").replace("}", "")
    safe = safe.replace("_", r"\_").replace(" ", r"\ ").replace("%", r"\%").replace("#", r"\#")
    return rf"\mathrm{{{safe}}}"

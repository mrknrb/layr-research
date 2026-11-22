"""What a method declares besides its code: the experiment's own pages (made of cards), the kinds of correction an
experiment can record, and the extra Python packages the method needs. Each is one plain value at the top of the
method file, so the file stays the one source of the method:

    PAGES = [
        {"title": "Overview", "cards": [
            {"card": "number", "value": "alpha", "title": "Cronbach's alpha"},
            {"card": "table", "table": "scores"},
        ]},
    ]
    CORRECTIONS = {
        "drop": {"label": "Leave out a respondent", "fields": {"respondent": "integer"}},
    }
    REQUIREMENTS = ["pingouin>=0.5"]

The panel edits them (the page builder, the kinds of correction, the packages) by rewriting only that assignment
(`write`); every other line of the file stays as it is. Nothing here runs the method.

Cards (every card may have a "title" and "width": "half" to sit beside the next half card):
- text: {"text": "words"} or {"value": "<a returned value or text>"}
- number: {"value": "<a returned value>"} or {"table": t, "column": c, "summary": mean|median|sum|count|min|max|
  first|last, "where": {column: value}}, optional "unit"
- table: {"table": t, "columns": [...], "filters": [...]}; t is a returned table or "step:<function>"
- figure: {"figure": "<a returned figure>"}
- chart: {"table": t, "x": c, "y": c or [c, ...], "group": c, "kind": line|scatter|bar} (drawn by the panel)
- review: a curve per item with regions on it whose boundaries can be dragged and saved as a correction:
  {"regions": t, "item": c, "label": c, "start": c, "end": c, "curve": ..., optional "name", "group", "status",
  "ok", "info", "note", "expected", "tolerance", "baseline", "window", "x_label", "y_label", "correct",
  "start_field", "end_field", "exclude"}. The curves come from
  - a table of the run with one row per point: "curve": {"table": t, "item": c, "x": c, "y": c} ("item": the
    table's column naming the curve; default the card's "item"), or
  - a raw file of the experiment: "curve": {"file": "<input id>"}, read as a table with the same "item", "x" and
    "y" columns when it is one (CSV, Excel...), or else by the curve reader the toolbox (or one of its add-ons)
    registers for its kind of file (toolbox/registry.py).

A correction kind: {"label", "help"?, "fields": {name: "text" | "number" | "integer" | "boolean", with "optional"
written in it when it may be left empty}, "key"?: [fields]}. Two approved corrections of one kind with the same key
fields are one decision: the newer replaces the older. Without "key", every field that isn't a number is the key.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

NAMES = ("PAGES", "CORRECTIONS", "REQUIREMENTS")
COMMENTS = {
    "PAGES": "# The experiment's own pages, made of cards (the panel's page builder writes this; it can be edited here).",
    "CORRECTIONS": "# The kinds of correction an experiment can record, each with a reason; approved ones reach the "
                   "recipe's `corrections`.",
    "REQUIREMENTS": "# Extra Python packages this method needs (installed for it before its first run).",
}
CARDS = {"text", "number", "table", "figure", "chart", "review"}
SUMMARIES = {"mean", "median", "sum", "count", "min", "max", "first", "last"}
CHART_KINDS = {"line", "scatter", "bar"}
FIELD_TYPES = {"text", "number", "integer", "boolean"}
WIDTH = 120

_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,\s-]+\])?"
                          r"(\s*(==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*(==|>=|<=|~=|!=|>|<)\s*"
                          r"[A-Za-z0-9.*+!_-]+)*)?$")
_ID = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


# ---------------------------------------------------------------------------------------------------- reading


def declared(constants: dict[str, Any]) -> dict[str, Any]:
    """The three declarations as written (None when absent), from a parsed method's module constants."""
    return {n.lower(): constants.get(n) for n in NAMES}


def pages(constants: dict[str, Any]) -> list[dict[str, Any]]:
    """The method's pages, cleaned: each {title, cards: [card dicts with "card" known]}."""
    out = []
    for p in constants.get("PAGES") or []:
        if not isinstance(p, dict) or not str(p.get("title") or "").strip():
            continue
        cards = [dict(c) for c in p.get("cards") or [] if isinstance(c, dict) and c.get("card") in CARDS]
        out.append({**p, "title": str(p["title"]).strip(), "cards": cards})
    return out


def field_spec(spec: Any) -> dict[str, Any]:
    """'optional integer' / 'text' / {"type", "optional"} → {"type", "optional"}."""
    if isinstance(spec, dict):
        t = str(spec.get("type") or "text").strip().lower()
        return {"type": t if t in FIELD_TYPES else "text", "optional": bool(spec.get("optional"))}
    words = str(spec or "text").replace("?", " optional").lower().split()
    t = next((w for w in words if w in FIELD_TYPES), "text")
    return {"type": t, "optional": "optional" in words}


def normalize_kinds(value: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(value, dict):
        return out
    for kid, k in value.items():
        if not isinstance(k, dict):
            continue
        fields = {str(n): field_spec(s) for n, s in (k.get("fields") or {}).items()}
        key = [str(x) for x in k.get("key") or [] if str(x) in fields] or \
            [n for n, f in fields.items() if f["type"] != "number"]
        out[str(kid)] = {"label": str(k.get("label") or kid), "help": str(k.get("help") or ""), "fields": fields,
                         "key": key}
    return out


def correction_kinds(constants: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The kinds of correction an experiment of this method can record (normalized); none when it declares none."""
    return normalize_kinds(constants.get("CORRECTIONS"))


def requirements(constants: dict[str, Any]) -> list[str]:
    reqs = constants.get("REQUIREMENTS") or []
    if isinstance(reqs, str):
        reqs = [reqs]
    return [str(r).strip() for r in reqs if isinstance(r, str) and str(r).strip()]


def problems(constants: dict[str, Any], outputs: set[str] | None = None,
             inputs: set[str] | None = None) -> list[tuple[str, str]]:
    """What is wrong with the declarations, in plain words: [(which declaration, message)] (the checker shows these
    as warnings). `outputs` and `inputs`: the recipe's results and raw file inputs, when known."""
    out: list[tuple[str, str]] = []
    if "PAGES" in constants:
        ps = constants["PAGES"]
        if not isinstance(ps, list):
            out.append(("PAGES", "PAGES must be a list of pages, each {\"title\": ..., \"cards\": [...]}."))
        else:
            for i, p in enumerate(ps, 1):
                if not isinstance(p, dict) or not str(p.get("title") or "").strip():
                    out.append(("PAGES", f"Page {i} in PAGES needs a \"title\"."))
                    continue
                for c in p.get("cards") or []:
                    kind = c.get("card") if isinstance(c, dict) else None
                    if kind not in CARDS:
                        out.append(("PAGES", f"The page \"{p['title']}\" has a card of an unknown kind ({kind!r}); "
                                             f"kinds: {', '.join(sorted(CARDS))}."))
                        continue
                    for ref in ("table", "regions", "figure", "value"):
                        name = c.get(ref)
                        if outputs is not None and isinstance(name, str) and name and not name.startswith("step:") \
                                and name not in outputs:
                            out.append(("PAGES", f"A {kind} card on the page \"{p['title']}\" shows `{name}`, which "
                                                 "the recipe doesn't return."))
                    if kind == "review":
                        out += [("PAGES", f"The review card on the page \"{p['title']}\": {m}")
                                for m in curve_problems(c.get("curve"), outputs, inputs)]
    if "CORRECTIONS" in constants:
        ks = constants["CORRECTIONS"]
        if not isinstance(ks, dict):
            out.append(("CORRECTIONS", "CORRECTIONS must be a dict: {kind id: {\"label\": ..., \"fields\": {...}}}."))
        else:
            for kid, k in ks.items():
                if not _ID.match(str(kid)):
                    out.append(("CORRECTIONS", f"The correction kind {kid!r} needs an id of lowercase letters, "
                                               "digits and _."))
                if not isinstance(k, dict) or not isinstance(k.get("fields") or {}, dict):
                    out.append(("CORRECTIONS", f"The correction kind {kid!r} needs \"fields\": {{name: type}}."))
    if "REQUIREMENTS" in constants:
        rs = constants["REQUIREMENTS"]
        if not isinstance(rs, list) or not all(isinstance(r, str) for r in rs):
            out.append(("REQUIREMENTS", "REQUIREMENTS must be a list of package names, e.g. [\"pingouin>=0.5\"]."))
        else:
            for r in rs:
                if not _REQUIREMENT.match(r.strip()):
                    out.append(("REQUIREMENTS", f"{r!r} in REQUIREMENTS isn't a package name with an optional "
                                                "version (like pingouin or pingouin>=0.5)."))
    return out


def curve_problems(curve: Any, outputs: set[str] | None = None, inputs: set[str] | None = None) -> list[str]:
    """What is wrong with a review card's "curve", in plain words."""
    if not isinstance(curve, dict) or not (curve.get("table") or curve.get("file")):
        return ["say where its curves come from: \"curve\": {\"table\": ..., \"x\": ..., \"y\": ...} (a table of the "
                "run) or {\"file\": \"<input>\"} (a raw file of the experiment)."]
    if curve.get("table") and curve.get("file"):
        return ["its curves come from a table or from a raw file, not both."]
    out = []
    if curve.get("table"):
        if not (curve.get("x") and curve.get("y")):
            out.append("a curve from a table needs its x and y columns.")
        name = curve["table"]
        if outputs is not None and isinstance(name, str) and not name.startswith("step:") and name not in outputs:
            out.append(f"its curves come from `{name}`, which the recipe doesn't return.")
    elif inputs is not None and curve["file"] not in inputs:
        out.append(f"its curves come from the raw file `{curve['file']}`, which isn't an input of the recipe "
                   f"(inputs: {', '.join(sorted(inputs)) or 'none'}).")
    return out


def check_requirements(reqs: list[str]) -> list[str]:
    bad = [r for r in reqs if not _REQUIREMENT.match(r.strip())]
    if bad:
        raise ValueError(f"Not a package name with an optional version: {', '.join(bad)} (like pingouin or "
                         "pingouin>=0.5; no addresses or options).")
    return [r.strip() for r in reqs]


# ---------------------------------------------------------------------------------------------------- corrections


def check_correction(kinds: dict[str, dict[str, Any]], kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The correction's fields, checked against its kind and turned into their types. Raises ValueError in plain
    words."""
    if not kinds:
        raise ValueError(f"This method declares no kinds of correction, so there is no '{kind}': add the kind to the "
                         "method first (Corrections › Kinds of correction, or CORRECTIONS in its file), and a "
                         "`corrections` input to its recipe.")
    k = kinds.get(kind)
    if k is None:
        raise ValueError(f"The method has no kind of correction '{kind}'. Kinds: {', '.join(kinds)}.")
    out: dict[str, Any] = {}
    for name, f in k["fields"].items():
        v = fields.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            if not f["optional"]:
                raise ValueError(f"\"{k['label']}\" needs {name}.")
            continue
        try:
            if f["type"] == "integer":
                v = int(float(v)) if not isinstance(v, bool) else int(v)
            elif f["type"] == "number":
                v = float(v)
            elif f["type"] == "boolean":
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y", "on")
            else:
                v = str(v).strip()
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be {'a whole number' if f['type'] == 'integer' else 'a number'}, "
                             f"not {v!r}.") from None
        out[name] = v
    for name in k["fields"]:  # start… and end… numbers: the end comes after the start
        if name.startswith("start") and k["fields"][name]["type"] == "number":
            end = "end" + name[len("start"):]
            if end in out and name in out and out[end] <= out[name]:
                raise ValueError(f"{end} must come after {name}.")
    unknown = [n for n in fields if n not in k["fields"] and fields[n] not in (None, "")]
    if unknown:
        raise ValueError(f"\"{k['label']}\" has no field {', '.join(unknown)}. Fields: {', '.join(k['fields'])}.")
    return out


def same_target(kinds: dict[str, dict[str, Any]], a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two corrections that decide the same thing (the newer replaces the older)."""
    if a.get("kind") != b.get("kind"):
        return False
    k = kinds.get(a.get("kind"))
    key = k["key"] if k else [n for n in a if n not in ("id", "kind", "reason", "status", "by", "at")]
    return all(a.get(n) == b.get(n) for n in key)


def describe(kinds: dict[str, dict[str, Any]], c: dict[str, Any]) -> str:
    """'Leave out a respondent: respondent 17' for lists and reports."""
    k = kinds.get(c.get("kind"))
    names = list(k["fields"]) if k else [n for n in c if n not in ("id", "kind", "reason", "status", "by", "at",
                                                                     "decided_by", "decided_at", "replaced_by")]
    parts = [f"{n.replace('_', ' ')} {c[n]}" for n in names if c.get(n) not in (None, "")]
    return f"{k['label'] if k else str(c.get('kind', '')).replace('_', ' ')}: {', '.join(parts)}".rstrip(": ")


def _without_declarations(source: str) -> list[str]:
    tree = ast.parse(source)
    lines = source.splitlines()
    for n in reversed(tree.body):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id in ("PAGES", "CORRECTIONS"):
            del lines[n.lineno - 1:n.end_lineno]
    comments = set(COMMENTS.values())
    return [ln.rstrip() for ln in lines if ln.strip() and ln.strip() not in comments]


def same_calculation(a: str, b: str) -> bool:
    """Two sources that differ only in their pages or kinds of correction: they calculate the same numbers."""
    try:
        return _without_declarations(a) == _without_declarations(b)
    except SyntaxError:
        return False


# ---------------------------------------------------------------------------------------------------- writing


def literal(v: Any) -> str:
    """A plain value as Python, on one line."""
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "True" if v else "False"
    if v is None:
        return "None"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{literal(str(k))}: {literal(x)}" for k, x in v.items()) + "}"
    raise ValueError(f"{type(v).__name__} can't be written in a method file (only text, numbers, lists and dicts).")


def layout(v: Any, indent: int = 0, room: int = WIDTH) -> str:
    """A plain value as Python, broken over lines (one item each) where it doesn't fit in `room`."""
    one = literal(v)
    if len(one) <= room or not isinstance(v, (list, tuple, dict)) or not v:
        return one
    pad = " " * (indent + 4)
    lines = []
    if isinstance(v, dict):
        for k, x in v.items():
            key = literal(str(k)) + ": "
            lines.append(pad + key + layout(x, indent + 4, WIDTH - len(pad) - len(key) - 1) + ",")
        return "{\n" + "\n".join(lines) + "\n" + " " * indent + "}"
    for x in v:
        lines.append(pad + layout(x, indent + 4, WIDTH - len(pad) - 1) + ",")
    return "[\n" + "\n".join(lines) + "\n" + " " * indent + "]"


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, (list, dict, tuple, str)) and not v)


def write(source: str, name: str, value: Any) -> str:
    """The source with `NAME = value` written in place of the old assignment (or added above the first function;
    an empty value removes it). Only those lines change."""
    if name not in NAMES:
        raise ValueError(f"Only {', '.join(NAMES)} are written here.")
    tree = ast.parse(source)
    lines = source.splitlines()
    text = f"{name} = {layout(value, 0, WIDTH - len(name) - 3)}" if not _empty(value) else None
    if text is not None:
        ast.parse(text)  # what is written must read back
    old = next((n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name), None)
    if old is not None:
        start, end = old.lineno - 1, old.end_lineno
        if text is not None:
            lines[start:end] = text.splitlines()
        else:
            if start > 0 and lines[start - 1].strip() == COMMENTS[name]:
                start -= 1
            if start > 0 and not lines[start - 1].strip() and end < len(lines) and not lines[end].strip():
                start -= 1
            del lines[start:end]
    elif text is not None:
        first_def = next((i for i, n in enumerate(tree.body)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))), len(tree.body))
        before = tree.body[:first_def]
        at = max((n.end_lineno for n in before), default=0)
        block = ["", COMMENTS[name], *text.splitlines()]
        if at == 0:
            block = block[1:] + [""]
        lines[at:at] = block
    out = "\n".join(lines) + "\n"
    ast.parse(out)
    return out

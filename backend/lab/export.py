"""Reports of a run: a self-contained HTML page, an Excel workbook of every table, and a Word document.

All three say the same: what went in (files with checksums, settings, corrections with reasons), how every number
was calculated (each step in the recipe's order: its sentence from the docstring, the formulas drawn from the code,
this run's numbers put in), the results and the quality flags, and the method's source as an appendix. Formulas are
drawn with matplotlib's mathtext, so the files need no JavaScript or fonts.
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from . import explain
from .store import Store, jsonable

MAX_ROWS = 80


def tex_image(latex: str, fmt: str = "svg", size: float = 12.0) -> bytes | None:
    """A formula drawn by matplotlib (None when mathtext can't draw it)."""
    if not latex:
        return None
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import mathtext
    from matplotlib.font_manager import FontProperties

    buf = io.BytesIO()
    try:
        mathtext.math_to_image(f"${latex}$", buf, prop=FontProperties(size=size), dpi=200, format=fmt)
    except (ValueError, RuntimeError):
        return None
    return buf.getvalue()


def methods_paragraph(exp: dict, run: dict) -> tuple[str, bool]:
    """(text, drafted by Claude): the experiment's kept Methods paragraph, or a factual sentence about the software."""
    if (exp.get("methods_text") or "").strip():
        return exp["methods_text"].strip(), True
    v = run.get("versions") or {}
    m = run.get("method") or {}
    return (f"Calculations were done in Python {v.get('python')} (pandas {v.get('pandas')}, NumPy {v.get('numpy')}, "
            f"SciPy {v.get('scipy')}) with the layr Research plugin, method '{m.get('name')}' version "
            f"{m.get('version')}; its source code is in the appendix."), False


def model(store: Store, exp: dict, run: dict) -> dict[str, Any]:
    explained = explain.explain_run(store, run)
    tables = []
    rdir = store.run_dir(run["id"])
    for name, o in (run.get("outputs") or {}).items():
        if o.get("kind") == "table":
            try:
                df = pd.read_csv(rdir / o["file"])
            except pd.errors.EmptyDataError:
                df = pd.DataFrame()
            tables.append({"name": name, "label": o.get("label") or name, "df": df})
        elif o.get("kind") == "figure":
            tables.append({"name": name, "label": o.get("label") or name, "png": (rdir / o["file"]).read_bytes()})
        elif o.get("kind") == "text":
            tables.append({"name": name, "label": o.get("label") or name,
                           "text": (rdir / o["file"]).read_text("utf-8"), "pre": True})
        elif o.get("kind") == "value":
            tables.append({"name": name, "label": o.get("label") or name, "text": _fmt(o.get("value"))})
    text, drafted = methods_paragraph(exp, run)
    exp = {**exp, "corrections": described(exp, explained["source"])}
    return {"exp": exp, "run": run, "explained": explained, "outputs": tables, "methods_text": text,
            "methods_drafted": drafted, "source": explained["source"]}


def described(exp: dict, source: str | None) -> list[dict]:
    """The experiment's corrections, each with `what` in words, from the kinds its method declares."""
    from . import methodfile
    from .pages import correction_kinds, describe

    try:
        parsed = methodfile.parse(source or "")
        kinds = correction_kinds(parsed.constants, methodfile.takes_corrections(parsed))
    except SyntaxError:
        kinds = {}
    return [{**c, "what": describe(kinds, c)} for c in exp.get("corrections") or []]


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60] or "report"


def write(store: Store, exp: dict, run: dict, fmt: str) -> Path:
    m = model(store, exp, run)
    name = f"{_slug(exp.get('title') or exp['id'])}_{run['id']}.{fmt}"
    path = store.root / "exports" / name
    if fmt == "html":
        path.write_text(to_html(m), "utf-8")
    elif fmt == "xlsx":
        to_xlsx(m, path)
    elif fmt == "docx":
        to_docx(m, path)
    else:
        raise ValueError(f"Unknown format {fmt}")
    return path


def _fmt(v: Any) -> str:
    v = jsonable(v)
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def human(name: str) -> str:
    return name.replace("_", " ").capitalize()


def table_text(summary: dict | None) -> str:
    if not isinstance(summary, dict):
        return _fmt(summary)
    if summary.get("kind") == "table":
        return f"table, {summary.get('rows')} rows: {', '.join(summary.get('columns') or [])}"
    return _fmt(explain.show(summary))


def step_parts(step: dict) -> dict[str, Any]:
    """What a report shows of one step: formulas (general) and examples (this run's numbers)."""
    formulas = [{"label": f"{human(step['function'])}", "latex": f["latex"]} for f in step.get("formulas") or []]
    examples = []
    for u in step.get("used") or []:
        for f in u.get("formulas") or []:
            formulas.append({"label": f"{human(u['function'])}: {u.get('sentence') or ''}", "latex": f["latex"]})
        for line in (u.get("example") or {}).get("latex") or []:
            examples.append({"label": f"{human(u['function'])} (one of {u.get('count')} calls)", "latex": line})
    return {"formulas": formulas, "examples": examples}


# ---------------------------------------------------------------------------------------------------- HTML


CSS = """
body{font:14px/1.5 system-ui,Segoe UI,sans-serif;color:#1d2129;max-width:1000px;margin:24px auto;padding:0 20px}
h1{font-size:24px;margin-bottom:4px}
h2{font-size:19px;margin-top:32px;border-bottom:1px solid #e6e8ec;padding-bottom:4px}
h3{font-size:16px;margin:22px 0 4px}.muted{color:#646b78}.sentence{margin:2px 0 8px}
table{border-collapse:collapse;font-size:12.5px;margin:8px 0}
td,th{border:1px solid #e6e8ec;padding:3px 7px;text-align:left}
th{background:#f4f5f7}.formula{margin:6px 0 2px}.formula img{vertical-align:middle;max-width:100%}
.label{font-size:12.5px;color:#646b78;margin-top:8px}
.ex{background:#f2f7fd;border-radius:6px;padding:6px 10px;margin:6px 0}
code,pre{font:12px Consolas,monospace;background:#f4f5f7;border-radius:3px}code{padding:1px 4px}
pre{padding:10px;overflow:auto;white-space:pre}
.draft{border-left:3px solid #eda100;padding-left:10px}
.warning{color:#a15c00}.error{color:#c62828}.step{page-break-inside:avoid}
@media print{body{margin:0}h2{page-break-after:avoid}pre{white-space:pre-wrap}}
"""


def _img(latex: str) -> str:
    svg = tex_image(latex, "svg")
    if svg is None:
        return f"<code>{html.escape(latex)}</code>"
    return f'<img alt="{html.escape(latex)}" src="data:image/svg+xml;base64,{base64.b64encode(svg).decode()}">'


def _table_html(df: pd.DataFrame, limit: int = MAX_ROWS) -> str:
    if df.empty:
        return '<p class="muted">(empty)</p>'
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(_fmt(v))}</td>" for v in r) + "</tr>"
                   for r in df.head(limit).itertuples(index=False))
    more = f'<p class="muted">{len(df) - limit} more rows in the Excel export.</p>' if len(df) > limit else ""
    return f"<table><tr>{head}</tr>{rows}</table>{more}"


def to_html(m: dict) -> str:
    exp, run = m["exp"], m["run"]
    meth = run.get("method") or {}
    e = html.escape
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(exp.get('title') or '')}</title>"
           f"<style>{CSS}</style></head><body>",
           f"<h1>{e(exp.get('title') or exp['id'])}</h1>",
           f"<p class='muted'>{e(meth.get('name') or '')} (method version {meth.get('version')}) · run {run['id']} · "
           f"{e(run.get('finished') or '')} · status {e(run['status'])}</p>"]
    if exp.get("notes"):
        out.append(f"<p>{e(exp['notes'])}</p>")
    out.append("<h2>Methods</h2>")
    if m["methods_drafted"]:
        out.append(f"<p class='draft'>{e(m['methods_text'])}</p><p class='muted'>Drafted by Claude from the method's "
                   "code and settings, kept with the experiment.</p>")
    else:
        out.append(f"<p>{e(m['methods_text'])}</p>")
    out.append("<h2>What went in</h2><h3>Raw files</h3><table><tr><th>Input</th><th>File</th><th>SHA-256</th></tr>")
    for k, f in (run.get("inputs") or {}).items():
        out.append(f"<tr><td>{e(k)}</td><td>{e(str(f.get('name')))}</td><td><code>{e(str(f.get('sha256')))}</code>"
                   "</td></tr>")
    out.append("</table><h3>Settings</h3><table><tr><th>Setting</th><th>Value</th></tr>")
    for k, v in (run.get("params") or {}).items():
        out.append(f"<tr><td>{e(k)}</td><td>{e(_fmt(v))}</td></tr>")
    out.append("</table><h3>Corrections</h3>")
    corr = exp.get("corrections") or []
    if corr:
        out.append("<table><tr><th>What</th><th>Reason</th><th>Status</th><th>By</th></tr>")
        for c in corr:
            out.append(f"<tr><td>{e(c.get('what') or c.get('kind', ''))}</td>"
                       f"<td>{e(c.get('reason', ''))}</td><td>{e(c.get('status', ''))}</td>"
                       f"<td>{e(c.get('by', ''))}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='muted'>None: every number comes from the automatic method.</p>")
    out.append("<h2>How every number was calculated</h2><p class='muted'>Each step of the method, in the order the "
               "recipe calls it: what it does (from its docstring), the formulas it uses (drawn from the code), and "
               "one of this run's calculations with its numbers. The full code is in the appendix.</p>")
    for i, s in enumerate(m["explained"]["steps"], 1):
        parts = step_parts(s)
        out.append(f"<div class='step'><h3>{i}. {e(human(s['function']))} <span class='muted'>"
                   f"<code>{e(s['function'])}</code></span></h3><p class='sentence'>{e(s.get('sentence') or '')}</p>")
        if s.get("text"):
            out.append(f"<p class='muted'>{e(s['text'])}</p>")
        ins = ", ".join(f"{k}: {table_text(v)}" for k, v in (s.get("inputs") or {}).items()
                        if isinstance(v, dict) and v.get("kind") == "table")
        if ins:
            out.append(f"<p class='muted'>Takes {e(ins)}. Gives {e(table_text(s.get('output')))}.</p>")
        for f in parts["formulas"]:
            out.append(f"<div class='label'>{e(f['label'])}</div><div class='formula'>{_img(f['latex'])}</div>")
        for x in parts["examples"]:
            out.append(f"<div class='ex'><div class='label'>In this run: {e(x['label'])}</div>"
                       f"<div class='formula'>{_img(x['latex'])}</div></div>")
        out.append("</div>")
    out.append("<h2>Results</h2>")
    for t in m["outputs"]:
        if "text" in t:
            body = f"<pre>{e(t['text'])}</pre>" if t.get("pre") else f"<p><b>{e(t['text'])}</b></p>"
            out.append(f"<h3>{e(t['label'])}</h3>{body}")
    for t in m["outputs"]:
        if "png" in t:
            out.append(f"<h3>{e(t['label'])}</h3><img style='max-width:100%' "
                       f"src='data:image/png;base64,{base64.b64encode(t['png']).decode()}'>")
    for t in m["outputs"]:
        if "df" in t and t["name"] != "flags":
            out.append(f"<h3>{e(t['label'])}</h3>{_table_html(t['df'])}")
    flags = next((t["df"] for t in m["outputs"] if t["name"] == "flags" and "df" in t), None)
    if flags is not None:
        out.append(f"<h2>Quality checks ({len(flags)})</h2>{_table_html(flags, 400)}")
    v = run.get("versions") or {}
    out.append(f"<h2>Appendix: the method's code</h2><p class='muted'>{e(meth.get('name') or '')}, version "
               f"{meth.get('version')}, exactly as this run ran it. It imports the plugin's toolbox (reading GAML, "
               "tables, peaks, straight lines, quality flags).</p>")
    out.append(f"<pre>{e(m['source'])}</pre>")
    out.append(f"<p class='muted'>Python {e(str(v.get('python')))}, pandas {e(str(v.get('pandas')))}, NumPy "
               f"{e(str(v.get('numpy')))}, SciPy {e(str(v.get('scipy')))}, matplotlib {e(str(v.get('matplotlib')))}. "
               f"Made with the layr Research plugin.</p></body></html>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------------- Excel


def _sheet_name(s: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", s)[:31] or "sheet"
    name, n = base, 2
    while name.lower() in used:
        name = f"{base[:28]}_{n}"
        n += 1
    used.add(name.lower())
    return name


def to_xlsx(m: dict, path: Path) -> None:
    used: set[str] = set()
    exp, run = m["exp"], m["run"]
    meth = run.get("method") or {}
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        info = [("Experiment", exp.get("title")), ("Method", f"{meth.get('name')} (version {meth.get('version')})"),
                ("Run", run["id"]), ("Finished", run.get("finished")), ("Status", run["status"]),
                ("Methods", m["methods_text"])]
        info += [(f"File: {k}", f"{f.get('name')} (SHA-256 {f.get('sha256')})") for k, f in
                 (run.get("inputs") or {}).items()]
        pd.DataFrame(info, columns=["", "value"]).to_excel(xw, sheet_name=_sheet_name("about", used), index=False)
        for t in m["outputs"]:
            if "df" in t:
                t["df"].to_excel(xw, sheet_name=_sheet_name(t["name"], used), index=False)
        pd.DataFrame([{"setting": k, "value": _fmt(v)} for k, v in (run.get("params") or {}).items()]) \
            .to_excel(xw, sheet_name=_sheet_name("settings", used), index=False)
        corr = exp.get("corrections") or []
        pd.DataFrame(corr or [{"kind": "none"}]).to_excel(xw, sheet_name=_sheet_name("corrections", used), index=False)
        pd.DataFrame([{"step": i, "function": s["function"], "what it does": s.get("sentence") or "",
                       "uses": ", ".join(u["function"] for u in s.get("used") or [])}
                      for i, s in enumerate(m["explained"]["steps"], 1)]) \
            .to_excel(xw, sheet_name=_sheet_name("method", used), index=False)
        pd.DataFrame({"code": m["source"].splitlines()}).to_excel(xw, sheet_name=_sheet_name("code", used),
                                                                  index=False)


# ---------------------------------------------------------------------------------------------------- Word


def to_docx(m: dict, path: Path) -> None:
    from docx import Document
    from docx.shared import Cm, Pt

    exp, run = m["exp"], m["run"]
    meth = run.get("method") or {}
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.add_heading(exp.get("title") or exp["id"], 0)
    doc.add_paragraph(f"{meth.get('name')} (method version {meth.get('version')}) · run {run['id']} · "
                      f"{run.get('finished') or ''}")
    doc.add_heading("Methods", 1)
    doc.add_paragraph(m["methods_text"])
    if m["methods_drafted"]:
        doc.add_paragraph().add_run("Drafted by Claude from the method's code and settings.").italic = True

    def formula(latex: str, width_cm: float = 12) -> None:
        png = tex_image(latex, "png", 11)
        if png:
            doc.add_picture(io.BytesIO(png), width=None)
            pic = doc.inline_shapes[-1]
            if pic.width > Cm(width_cm):
                ratio = Cm(width_cm) / pic.width
                pic.width, pic.height = Cm(width_cm), int(pic.height * ratio)
            else:
                pic.width, pic.height = int(pic.width * 0.5), int(pic.height * 0.5)
        else:
            doc.add_paragraph(latex)

    def table(df: pd.DataFrame, limit: int = MAX_ROWS) -> None:
        if df.empty:
            doc.add_paragraph("(empty)")
            return
        df = df.head(limit)
        t = doc.add_table(rows=1, cols=len(df.columns))
        t.style = "Light Grid Accent 1"
        for i, c in enumerate(df.columns):
            t.rows[0].cells[i].text = str(c)
        for r in df.itertuples(index=False):
            cells = t.add_row().cells
            for i, v in enumerate(r):
                cells[i].text = _fmt(v)

    doc.add_heading("What went in", 1)
    for k, f in (run.get("inputs") or {}).items():
        doc.add_paragraph(f"{k}: {f.get('name')} (SHA-256 {f.get('sha256')})", style="List Bullet")
    corr = exp.get("corrections") or []
    doc.add_paragraph("Corrections: " + ("none." if not corr else ""))
    for c in corr:
        doc.add_paragraph(f"{c.get('what') or c.get('kind')}: {c.get('reason', '')} ({c.get('status')}, "
                          f"{c.get('by', '')})", style="List Bullet")
    doc.add_heading("How every number was calculated", 1)
    for i, s in enumerate(m["explained"]["steps"], 1):
        parts = step_parts(s)
        doc.add_heading(f"{i}. {human(s['function'])}", 2)
        doc.add_paragraph(s.get("sentence") or "")
        for f in parts["formulas"]:
            doc.add_paragraph().add_run(f["label"]).italic = True
            formula(f["latex"])
        for x in parts["examples"]:
            doc.add_paragraph("In this run: " + x["label"])
            formula(x["latex"])
    doc.add_heading("Results", 1)
    for t in m["outputs"]:
        if "text" in t:
            doc.add_heading(t["label"], 2)
            p = doc.add_paragraph()
            run_ = p.add_run(t["text"])
            if t.get("pre"):
                run_.font.name = "Consolas"
                run_.font.size = Pt(8)
    for t in m["outputs"]:
        if "png" in t:
            doc.add_heading(t["label"], 2)
            doc.add_picture(io.BytesIO(t["png"]), width=Cm(16))
    for t in m["outputs"]:
        if "df" in t and t["name"] != "flags" and len(t["df"]) <= MAX_ROWS and len(t["df"].columns) <= 14:
            doc.add_heading(t["label"], 2)
            table(t["df"])
    flags = next((t["df"] for t in m["outputs"] if t["name"] == "flags" and "df" in t), None)
    if flags is not None:
        doc.add_heading(f"Quality checks ({len(flags)})", 1)
        table(flags, 400)
    doc.add_heading("Appendix: the method's code", 1)
    for line in m["source"].splitlines():
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        run_ = p.add_run(line or " ")
        run_.font.name = "Consolas"
        run_.font.size = Pt(8)
    doc.save(path)

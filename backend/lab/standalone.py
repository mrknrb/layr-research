"""A method as a folder that runs without layr, and a notebook to explore a run in JupyterLab.

The folder holds everything needed to recompute the numbers on another computer:

    <method>.py          the method, exactly as it ran
    toolbox/             the toolbox it imports
    settings.json        the experiment's settings (the rest are the method's defaults)
    corrections.json     the approved corrections, with their reasons
    inputs.json          the raw files: name, SHA-256, size, and where they are on this PC
    run.py               the runner: python run.py [--data <folder with the raw files>] [--out <folder>]
    requirements.txt     the Python packages (the versions of the run)
    README.txt           how to run it
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from . import methodfile
from .errors import LabError
from .store import Store, jsonable

TOOLBOX = Path(__file__).resolve().parent.parent / "toolbox"

RUNNER = '''"""Runs the method in this folder without layr and writes its results.

    python run.py [--data FOLDER] [--out FOLDER]

The raw files (inputs.json) are looked for in --data (default: the folder data/ next to this file), then where they
were on the PC that exported this folder. Each is checked against its SHA-256 first, so the numbers come from exactly
the same data. Results go to --out (default: results/): tables as CSV, figures as PNG.
"""

import argparse
import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
METHOD = HERE / "{method_file}"


def recipe_name(source):
    """RECIPE = "name" in the method file, or else its last function."""
    recipe, last = None, None
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "RECIPE":
            recipe = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef):
            last = node.name
    return recipe or last


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_input(info, data):
    for p in [data / info["name"]] + ([Path(info["path"])] if info.get("path") else []):
        if p.exists():
            if sha256(p) != info["sha256"]:
                sys.exit(f"{{p}} is not the same file that was analysed (its SHA-256 differs).")
            return p
    sys.exit(f"The raw file {{info['name']}} is missing: put it into {{data}} (or give --data).")


def main():
    parser = argparse.ArgumentParser(description="Run the method without layr.")
    parser.add_argument("--data", default=str(HERE / "data"), help="folder with the raw files")
    parser.add_argument("--out", default=str(HERE / "results"), help="folder for the results")
    args = parser.parse_args()
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("method", METHOD)
    method = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(method)
    recipe = getattr(method, recipe_name(METHOD.read_text("utf-8")))
    kwargs = json.loads((HERE / "settings.json").read_text("utf-8"))
    if "corrections" in recipe.__code__.co_varnames[:recipe.__code__.co_argcount]:
        kwargs["corrections"] = json.loads((HERE / "corrections.json").read_text("utf-8"))
    for name, info in json.loads((HERE / "inputs.json").read_text("utf-8")).items():
        kwargs[name] = str(find_input(info, Path(args.data)))
    results = recipe(**kwargs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, value in results.items():
        if hasattr(value, "to_csv"):
            value.to_csv(out / f"{{name}}.csv", index=False)
            print(f"{{name}}: {{len(value)}} rows -> {{out / (name + '.csv')}}")
        elif hasattr(value, "savefig"):
            value.savefig(out / f"{{name}}.png", format="png")
            print(f"{{name}}: figure -> {{out / (name + '.png')}}")
        else:
            (out / f"{{name}}.json").write_text(json.dumps(value, default=str, indent=1), "utf-8")
            print(f"{{name}} -> {{out / (name + '.json')}}")


if __name__ == "__main__":
    main()
'''

README = """{name} (version {version}), exported from layr's Research plugin{experiment}.

This folder recomputes the numbers without layr. With Python 3.11 or newer:

    pip install -r requirements.txt
    python run.py --data <folder with the raw files>

The raw files are listed in inputs.json with their SHA-256; run.py checks them before it starts. The method is
{method_file} (plain Python: read it to see how every number is calculated); the toolbox/ folder holds the
functions it imports. settings.json holds the experiment's settings, corrections.json the corrections with their
reasons. The results go to results/.
"""


def module_name(method_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", method_id)
    return name if name[:1].isalpha() else f"m_{name}"


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")[:50] or "export"


def export_folder(store: Store, method: dict[str, Any], dest: Path, exp: dict[str, Any] | None = None,
                  run: dict[str, Any] | None = None, include_raw: bool = False) -> Path:
    """Write the folder (see the top of this file); `run` takes its exact source, settings and corrections."""
    source = method["source"]
    if run is not None:
        p = store.run_dir(run["id"]) / "method.py"
        if p.exists():
            source = p.read_text("utf-8")
    try:
        parsed = methodfile.parse(source)
    except SyntaxError as e:
        raise LabError(f"The method can't be read (line {e.lineno}): fix it before exporting.") from e
    fn = parsed.recipe_function
    params = fn.params if fn else []
    dest.mkdir(parents=True, exist_ok=True)
    method_file = f"{module_name(method['id'])}.py"
    (dest / method_file).write_text(source, "utf-8", newline="\n")
    if (dest / "toolbox").exists():
        shutil.rmtree(dest / "toolbox")
    shutil.copytree(TOOLBOX, dest / "toolbox", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    settings: dict[str, Any] = {}
    corrections: list[dict[str, Any]] = []
    inputs: dict[str, Any] = {}
    if exp is not None:
        given = dict((run or {}).get("settings") or exp.get("params") or {})
        settings = {k: v for k, v in given.items() if k in params and not methodfile.is_input(k)}
        corrections = (run or {}).get("corrections") if run else \
            [c for c in exp.get("corrections") or [] if c.get("status") == "approved"]
        from .runner import experiment_inputs

        files = experiment_inputs(exp, [p for p in params if p.endswith("_file")])
        for name, f in files.items():
            path = store.path(f["path"])
            inputs[name] = {"name": f.get("name") or path.name, "sha256": f.get("sha256"), "size": f.get("size"),
                            "path": str(path)}
            if include_raw:
                (dest / "data").mkdir(exist_ok=True)
                shutil.copyfile(path, dest / "data" / inputs[name]["name"])
    (dest / "settings.json").write_text(json.dumps(jsonable(settings), indent=1, ensure_ascii=False), "utf-8")
    (dest / "corrections.json").write_text(json.dumps(jsonable(corrections or []), indent=1, ensure_ascii=False),
                                           "utf-8")
    (dest / "inputs.json").write_text(json.dumps(jsonable(inputs), indent=1, ensure_ascii=False), "utf-8")
    (dest / "run.py").write_text(RUNNER.format(method_file=method_file), "utf-8", newline="\n")
    v = (run or {}).get("versions") or {}
    from .runner import versions_here

    v = {**versions_here(), **v}
    (dest / "requirements.txt").write_text("".join(f"{pkg}=={v[pkg]}\n" for pkg in ("pandas", "numpy", "scipy",
                                                                                  "matplotlib") if v.get(pkg)),
                                           "utf-8")
    (dest / "README.txt").write_text(README.format(
        name=method.get("name") or method["id"], version=(run or {}).get("method", {}).get("version")
        or method.get("version"), method_file=method_file,
        experiment=f" with the experiment '{exp.get('title')}'" if exp else ""), "utf-8")
    return dest


def zip_folder(folder: Path) -> Path:
    archive = shutil.make_archive(str(folder), "zip", root_dir=folder.parent, base_dir=folder.name)
    return Path(archive)


# ---------------------------------------------------------------------------------------------------- notebook


def _cell(kind: str, text: str) -> dict[str, Any]:
    lines = text.strip("\n").splitlines(keepends=True)
    if kind == "markdown":
        return {"cell_type": "markdown", "metadata": {}, "source": lines}
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": lines}


def notebook_root(store: Store) -> tuple[Path, bool]:
    """Where JupyterLab (the jupyter plugin) keeps its notebooks; else the exports folder."""
    import os

    home = os.environ.get("LAYR_HOME")
    data = os.environ.get("LAYR_DATA_DIR")
    for candidate in ([Path(home) / "plugin-data" / "jupyter"] if home else []) + \
            ([Path(data).parent / "jupyter"] if data and Path(data).parent.name == "plugin-data" else []):
        return candidate, True
    return store.root / "exports" / "notebooks", False


def make_notebook(store: Store, method: dict[str, Any], exp: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """A folder in JupyterLab's notebooks with the run's tables, the method, and explore.ipynb that loads them."""
    from .explain import calls_of

    root, jupyter = notebook_root(store)
    folder = root / "Research" / f"{slug(exp.get('title') or exp['id'])}-{run['id']}"
    export_folder(store, method, folder, exp, run)
    (folder / "tables").mkdir(exist_ok=True)
    tables = []
    for name, o in (run.get("outputs") or {}).items():
        if o.get("kind") == "table":
            shutil.copyfile(store.run_dir(run["id"]) / o["file"], folder / "tables" / f"{name}.csv")
            tables.append(name)
    module = module_name(method["id"])
    source = (folder / f"{module}.py").read_text("utf-8")
    parsed = methodfile.parse(source)
    recipe = parsed.recipe_function
    example = ""
    calls = calls_of(store, run["id"]) or {}
    for fn in parsed.functions:
        if fn.kind != "formula":
            continue
        call = next((c for c in calls.get("calls") or [] if c["function"] == fn.name
                     and all(isinstance(v, (int, float)) for v in (c.get("inputs") or {}).values())), None)
        if call:
            args = ", ".join(f"{k}={v!r}" for k, v in call["inputs"].items())
            example = f"help(method.{fn.name})\nmethod.{fn.name}({args})  # the run gave {call.get('result')!r}"
            break
    if not example and parsed.functions:
        example = f"help(method.{parsed.functions[0].name})"
    loads = "\n".join(f'{t} = pd.read_csv("tables/{t}.csv")' for t in tables)
    first = tables[0] if tables else None
    takes_corrections = recipe is not None and "corrections" in recipe.params
    rerun = (
        'settings = json.load(open("settings.json"))\n'
        'inputs = json.load(open("inputs.json"))\n'
        "files = {name: info[\"path\"] for name, info in inputs.items()}\n"
        + ('corrections = json.load(open("corrections.json"))\n' if takes_corrections else "")
        + f"results = method.{recipe.name if recipe else 'recipe'}(**files, "
        + ("corrections=corrections, " if takes_corrections else "") + "**settings)\n"
        + "list(results)"
    )
    cells = [
        _cell("markdown", f"# {exp.get('title') or exp['id']}: run {run['id']}\n\n"
                          f"Method **{method.get('name') or method['id']}** version "
                          f"{(run.get('method') or {}).get('version')}, run {run.get('finished') or ''}. The tables "
                          f"in `tables/` are this run's results; `{module}.py` is the exact code that made them. Change"
                          " and rerun anything here: nothing goes back to layr."),
        _cell("code", f"import json\n\nimport pandas as pd\n\nimport {module} as method  # the method's functions"),
        _cell("markdown", "## The run's tables"),
        _cell("code", (loads + (f"\n{first}" if first else "")) or "# this run returned no tables"),
        _cell("markdown", "## Try a function\n\nEvery function of the method can be called on its own; `help(...)` "
                          "shows its docstring."),
        _cell("code", example or "# no functions"),
        _cell("markdown", "## Run the whole method again\n\nWith the same settings, corrections and raw files (they "
                          "are read from where they are stored in layr)."),
        _cell("code", rerun),
    ]
    nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                                      "name": "python3"},
                                       "language_info": {"name": "python"}}, "nbformat": 4, "nbformat_minor": 4}
    path = folder / "explore.ipynb"
    path.write_text(json.dumps(nb, indent=1, ensure_ascii=False), "utf-8")
    rel = path.relative_to(root).as_posix() if jupyter else None
    return {"folder": str(folder), "notebook": str(path), "jupyter": jupyter, "in_jupyter": rel}

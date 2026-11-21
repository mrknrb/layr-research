# Research

Explainable data analysis for a researcher who is learning Python. Two panels:

- **Experiments** (`panels/lab.*`, `panels/flow.*`): an experiment's main screen is its method drawn as a **flow**
  (boxes for steps, wires for the tables between them, each with this run's values); from a box open its code with
  its values, its tables, the checks, peak review, corrections; methods are edited in a code editor (CodeMirror 6,
  `panels/editor.*`) or in the flow; the results database; a guide to the Python the methods use.
- **Quick analysis** (`panels/main.*`): load a CSV, describe it, chart it, t-test and ANOVA (the 0.1 plugin).

What 0.4 is and why: `docs/proposals/research-flow.md`; how its parts fit: `docs/proposals/research-flow/
build-contract.md` (its "Tools as built" has every new tool with a real result). The step design of 0.2 (JSON lists
of step types) is described, as history, in `docs/proposals/analysis-workflows.md`.

## Words

- **Method**: one plain Python file with three levels: **formulas** (tiny functions on numbers), **steps**
  (functions that take tables and return a table) and the **recipe** (the function that calls the steps in order and
  returns the results). Built-in methods ship in `backend/lab/methods/<id>.py`; every save is a new version in
  `LAYR_DATA_DIR/lab/methods/<id>/<n>.py`.
- **Toolbox** (`backend/toolbox/`): ready-made functions a method imports (`from toolbox import fit_line`): reading
  GAML and tables, finding and integrating peaks, straight lines, quality flags, figures. Readable, not editable in
  layr: to change one, copy it into the method (the panel does it).
- **Experiment**: a method's recipe applied to raw files, with settings and **corrections** (rename, exclude, manual
  peak boundaries), each with a written reason. Raw files are copied into the store by checksum and never changed.
- **Run**: one calculation of an experiment, in a separate process. It records the raw files' SHA-256, the settings,
  the approved corrections, the library versions, a copy of the exact source it ran, every step's tables, every
  returned table and figure, every call of the method's functions (`calls.json`) and the values inside them
  (`inside.json`, see below). A **trial run** (while she edits, or the comparison after a change) is kept apart: it
  isn't the experiment's latest run and doesn't go to the results database.
- **Flow**: the recipe drawn as boxes and wires, computed from the file every time (nothing of it is stored). Every
  change made in the flow is a small edit of the code.
- **Block**: a ready-made step from the toolbox (pick rows, add a column from a formula, summarize by group, join,
  fit a line per group, sort, pick columns, charts), offered in the flow with a small form.
- **Reference experiment**: one experiment per method that is rerun after every change of the code, to list which
  numbers moved.
- **Draft**: a change an AI agent wrote. It doesn't count until the user accepts it in the panel, where she sees the
  changed lines and the numbers that moved on the reference experiment. Her own saves count at once.
- **Results database**: `LAYR_DATA_DIR/lab/results.sqlite`; every experiment's latest results, one table per output,
  to compare strains and conditions across the PhD.

## Writing a method

```python
"""Growth rate from OD600

Specific growth rates from the exponential phase of each culture.
"""

import numpy as np
import pandas as pd

from toolbox import fit_line, read_table


def doubling_time_h(growth_rate_per_h):
    """The time a culture takes to double, from its specific growth rate."""
    return np.log(2) / growth_rate_per_h


def growth_rates(od, window_h):
    """Fit ln(OD600) against time in the exponential window, for each culture.

    Formula:
        mu = slope of ln(od600) against time_h
    """
    start, end = window_h
    rows = []
    for culture, group in od[od.time_h.between(start, end)].groupby("culture"):
        line = fit_line(group.time_h, np.log(group.od600))
        rows.append({"culture": culture, "growth_rate_per_h": line.slope, "r2": line.r2,
                     "doubling_time_h": doubling_time_h(line.slope)})
    return pd.DataFrame(rows)


def growth(plate_file, window_h=(2, 6)):
    """Growth rate from OD600.

    Inputs:
        plate_file: Plate reader export (CSV or Excel) [.csv, .xlsx]

    Settings:
        window_h: Exponential window. Start and end of the exponential phase.

    Returns:
        rates: Growth rates
    """
    od = read_table(plate_file)
    rates = growth_rates(od, window_h)
    return {"rates": rates}
```

The conventions (`backend/lab/methodfile.py` has them in full):

- The module docstring's first line is the method's name; the next paragraph describes it.
- **The recipe** is the last function in the file, or the one a module-level `RECIPE = "name"` names. It only calls
  steps, keeps their results and returns a dict of named results (tables, figures, values).
- **Its parameters** are the experiment's inputs and settings: a name ending in `_file` is a raw file; `corrections`
  gets the approved corrections (a list of dicts); every other parameter is a setting. A setting's default is a plain
  value or a module-level constant holding one (`sample_patterns=SAMPLE_PATTERNS`); a setting without a default must
  be set by the experiment. Keep the names of settings stored experiments use.
- **Its docstring** documents them: `Inputs:` (`name: label [.ext, ...]`), `Settings:` (`name: Label. Help text`,
  optionally under group lines like `Integration:`; `(type)` after the name picks the form's editor: text, number,
  integer, boolean, list, object, patterns, compounds, biomass; `[unit]` overrides the unit read from the name) and
  `Returns:` (`name: label`). Without `(type)` the default's type decides; a setting without a default whose name ends
  in a unit is a number.
- **Every function starts with a docstring** whose first line is one plain sentence: it is shown everywhere. A
  `Formula:` block holds formulas that aren't one expression, one per line, in Python (drawn as math) or as `$LaTeX$`.
- **Formulas** use only arithmetic (+ - * / ** and abs, sqrt, log, exp, min, max): they work on single numbers and
  on whole columns, and their body is drawn as math, once with names and once with a run's numbers.
- **Units go in names** (`conc_mM`, `time_min`, `rate_mM_per_min`): they are shown as units; there is no unit check.
- `DATABASE = ["table", ...]` names the returned tables kept in the results database (default: all of them). A
  returned table named `flags` (level, category, injection, compound, message) is the run's quality summary.
- Steps follow a few patterns a beginner can read: pick rows (`table[table.role == "sample"]`), "for each group:
  calculate, add a row dict to a list, `pd.DataFrame(rows)`", `merge` to join; never change a table you were given.
  The panel's **Python guide** shows each with an example.

**The style checker** (`backend/lab/checker.py`) reads the file and says, with line numbers: errors that block saving
(Python can't read it, an import can't work, no recipe) and warnings that don't (a function without a docstring, a
formula that does more than arithmetic, a step longer than about 25 lines, a function that changes a table it was
given, a recipe that does more than call steps, imports other than the toolbox, pandas, numpy, scipy and the maths
modules, file or network access outside the toolbox, a result named like a function).

## The flow

`backend/lab/flow.py` reads the recipe into a graph: one box per recipe line. `name = step(...)` is a step box whose
inputs are the step's parameters, each fed by a wire (a name an earlier box made), a setting, a raw file input or a
value written in place; any other line (an `if`, a loop, arithmetic) is a code box showing its lines. A comment line
`# --- Title` starts a group of boxes. The boxes whose results reach only the `flags` result are the quality lane.
For each table input the flow knows which columns the step reads (from its code) and marks a wire whose recorded
table lacks one. With a run, each box has its status (done, cached, failed, not run), its time and the value on every
wire. Layout is automatic, left to right; no positions are stored.

`flow.edit(source, op)` makes a flow change as an exact edit of the recipe: add or remove a step, connect or
disconnect an input, rename a wire, add or remove a setting, an input or a result, set a default, group boxes, add a
code box, start a new function. Only the lines it must touch change; comments stay byte for byte; lines are wrapped
at 120 columns as the file is. A change that would break the method is refused in plain words.

## How it runs, records and explains

- `methodfile.py` reads a method without running it (outline, settings form, inputs); the backend never executes a
  method itself.
- `runner.py` sends a run to the calculation process (`worker.py`), started ahead of time and kept warm; Stop, the
  time limit (15 min, or `time_limit_s`) or a crash end that process, and a fresh one is started.
- **Remembered steps**: a step whose code (and the code of the method's functions it uses), the toolbox, the
  libraries and the content of its inputs are the same as in an earlier run isn't calculated again: its result and
  its whole record come back from `LAYR_DATA_DIR/lab/cache/steps` and it is marked `cached`. After a changed setting
  only the steps that depend on it run. "Run everything again" (`run` with `fresh`) and the reference comparison
  calculate every step. Reading GAML and integrating peaks are also remembered by `toolbox/cache.py`.
- `recorder.py` wraps the method's top-level functions (and the toolbox functions it imports) while it runs and
  records every call: inputs, result, time, error. Calls made by the recipe are the steps (their tables are saved);
  deeper calls are the formulas and helpers. A function called many times keeps its first 1000 calls and a count;
  a formula used on a whole column keeps the column's index and values.
- **Inside the functions**: with Python 3.12's `sys.monitoring`, on the method file's own code only (pandas and the
  toolbox run at full speed), every statement that gives a name a value keeps that value: in a loop the first 3 and
  last 3 rounds and a failed one, with all rounds counted. Every table is stored once by its content
  (`values.py`: `runs/<id>/values/<fingerprint>.csv.gz`), with a size budget per run past which tables are kept as
  their first rows (and it says so). The values inside steps of old runs are cleared after a while to save space;
  their results stay.
- `explain.py` draws a formula's body as LaTeX (`latex.py`), shows a step's tables and the formulas used inside it
  with this run's numbers, and answers "where does this number come from": the recorded call that returned exactly
  that value (first among the calls inside the step that made the column; for a column-wise formula also by row),
  with its inputs, then the calls that gave those inputs.
- A run error names the method's own line and says what is wrong in plain words (a missing column: which columns
  the table does have).
- `compare.py` lists the numbers that moved between two runs (rows paired by their identifying columns).
- `standalone.py` exports a method as a folder that runs without layr (`python run.py`): the method, the toolbox,
  the settings, the corrections, the raw files' checksums, a runner and the requirements. It also writes "this run
  in a notebook": a folder in JupyterLab's notebooks (the `jupyter` plugin's data folder) with the run's tables, the
  method and `explore.ipynb`.
- Reports (`export.py`, HTML / Excel / Word): what went in, how every number was calculated (each step's sentence,
  formulas drawn from the code, this run's numbers), results, flags, corrections with reasons, and the source code
  as an appendix.

## Blocks and code help

- `toolbox/blocks.py`: the ready-made steps, plain functions a beginner can read ("a table in, a table out", with a
  docstring in the method style). `blocks()` builds the flow's palette from their signatures and docstrings: each
  one's title, sentence, what it takes and gives, and the fields of its form (column, columns, text, number, choice,
  formula, table...). A block added in the flow is an ordinary call in the recipe plus its name in the
  `from toolbox import` line.
- `lab/codeintel.py`: help in the editor from the plugin's own Python with Jedi: completion (columns of the tables
  the last run saw come first after `peaks.` and inside `peaks["`), hover, the signature being typed, go to
  definition, rename everywhere; Ruff lays a method out at 120 columns, only when asked. Jedi is warmed up in the
  background when the server starts.

## Tools

Methods: `methods`, `method_get`, `method_check`, `method_versions`, `method_diff`, `method_create`, `method_write`,
`method_revert`, `method_reference`, `method_delete`, `draft_get`, `draft_decide` (the user's; `source` accepts a
merged text), `toolbox_docs`, `toolbox_copy`. The flow: `flow_get`, `flow_edit` (saves nothing), `flow_new`, `blocks`.
Experiments and runs: `experiments`, `experiment_get`, `experiment_create`, `experiment_update`, `experiment_delete`,
`lab_sample`, `file_import` and the upload tools, `run` (with `fresh`, or a trial run of an unsaved `source` /
`settings`), `run_stop`, `run_get`, `runs`, `compare_runs`. Results and explanations: `result_table`, `figure`,
`provenance`, `explain`, `explain_function`, `function_inside` (a function's code with the values of every line),
`value_get` (the rows of any stored table), `chromatogram`, the corrections, `database_tables`, `database_query`,
`export`, `method_export`, `notebook`. The editor: `code_complete`, `code_hover`, `code_signature`,
`code_definition`, `code_rename`, `code_format`. For the panel's AI buttons: `ai_prompt` (explain, a Methods
paragraph, or write a step from her sentence).

## The GC-FID activity assay

`gc-activity-assay` (`backend/lab/methods/gc-activity-assay.py`) is Ragini's assay (gcassay, her Python package,
rewritten as one readable method): read the GAML export → sample names and design checks → reference retention
times from the standards → RT-locked peak integration (with drift correction and the corrections) → internal
standard ratio → calibration with LOD/LOQ → concentrations → time course → initial rates (U/gCDW) → interval rates →
mass balance → quality checks → figure. `tests/test_lab_real.py` proves its numbers equal gcassay's on a real run
(set `GCASSAY_PROJECT` to the gcassay project folder; the data never leaves it). A gcassay config and corrections
file can be taken over when making an experiment (New experiment › Settings from a gcassay config, or
`experiment_create` with `gcassay_config`).

## For AI agents

The server's instructions say the rules and the style; in short: numbers only from `result_table` / `provenance` /
`database_query` (with the run id); a method changes only through `method_write` (the whole file or one function),
which is a draft the user accepts and comes back with the checker's warnings to fix; `draft_get` shows the changed
lines and the numbers that moved; `draft_decide` is the user's. Corrections need a reason and stay proposed until the
user approves them. An agent reads the flow with `flow_get`, makes flow changes with `flow_edit` and saves the
result with `method_write` (a draft like any change), looks inside a run with `function_inside` / `value_get`, and
tries an unsaved change with `run` and `source` (a trial run that saves nothing). The panel's Explain button, the
Methods paragraph and "describe a step" ask Claude through the claude plugin's `ask` tool (an optional connection;
the buttons hide without it) and mark the answer as written by AI; a step Claude writes is a draft she reviews.

## Tests

From `plugins/research`: `uv run pytest tests -q` (add `GCASSAY_PROJECT=<folder>` for the real-data comparison).
`tests/test_flow_server.py` drives 0.4 end to end through the server as layr starts it (the sample run, its flow
with a value on every wire, the inside of a step, a changed setting with remembered steps, a block added and tried).
The panel: start `layr serve` with this plugin linked and its own home, then
`node plugins/dev/research.test.mjs --port <port> --home <that home>`.

# Research

Explainable data analysis for a researcher who is learning Python, for any study whose data fits in tables (a
survey, lab measurements, an instrument's export...). Nothing in the plugin's core knows one kind of study: what
belongs to one method lives in its method file, its example and, when it needs its own readers, a toolbox add-on.
Two panels:

- **Experiments** (`panels/lab.*`, `panels/flow.*`, `panels/pages.js`): an experiment's main screen is its method
  drawn as a **flow** (boxes for steps, wires for the tables between them, each with this run's values); beside it
  the experiment's **own pages**, made of cards the user puts together (numbers, tables, charts, texts, curves to
  review); from a box open its code with its values, its tables, the checks, corrections; methods are edited in a
  code editor (CodeMirror 6, `panels/editor.*`) or in the flow; a guide to the Python the methods use.
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
  tables, straight lines, quality flags, ready-made steps and charts. Its **add-ons** are its sub-packages
  (`backend/toolbox/<add-on>/`, imported as `from toolbox.<add-on> import ...`) with the functions for one kind of
  data or instrument; the GC add-on (`toolbox/gc/`) reads GAML files and finds and measures peaks. Readable, not
  editable in layr: to change one, copy it into the method (the panel does it).
- **Experiment**: a method's recipe applied to raw files, with settings and **corrections** of the kinds its method
  declares (leave out a respondent, recode an answer, a region's ends set by hand...), each with a written reason.
  Raw files are copied into the store by checksum and never changed.
- **Result input**: an input that reads what other experiments made instead of a raw file: the end result of one
  experiment is the start of another, and an experiment can compare others. The experiment picks one or several
  experiments and a table each of their latest runs made (`scores`, or a step's table `step:<function>`); the
  recipe gets them as one table, stacked, with the columns `experiment` (the source's title) and `experiment_id`
  in front. When a source gets a newer run with other numbers, the reader is **out of date** (it says why); it is
  never rerun by itself. No loops: an experiment can't read itself, not even through others.
- **Run**: one calculation of an experiment, in a separate process. It records the raw files' SHA-256, the settings,
  the approved corrections, the library versions, a copy of the exact source it ran, every step's tables, every
  returned table and figure, every call of the method's functions (`calls.json`) and the values inside them
  (`inside.json`, see below). A **trial run** (while she edits, or the comparison after a change) is kept apart: it
  isn't the experiment's latest run.
- **Flow**: the recipe drawn as boxes and wires, computed from the file every time (nothing of it is stored). Every
  change made in the flow is a small edit of the code.
- **Block**: a ready-made step from the toolbox (pick rows, add a column from a formula, summarize by group, join,
  fit a line per group, sort, pick columns, charts), offered in the flow with a small form.
- **Reference experiment**: one experiment per method that is rerun after every change of the code, to list which
  numbers moved.
- **Draft**: a change an AI agent wrote. It doesn't count until the user accepts it in the panel, where she sees the
  changed lines and the numbers that moved on the reference experiment. Her own saves count at once.
- **Example**: an experiment with made-up raw files to try a method (`backend/lab/examples/<id>.py`, see below).

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
- **Its parameters** are the experiment's inputs and settings: a name ending in `_file` is a raw file; a parameter
  whose `Inputs:` line ends in `[result]` is a result of other experiments (below); `corrections` gets the approved
  corrections (a list of dicts); every other parameter is a setting. A setting's default is a plain value or a
  module-level constant holding one (`strains=STRAINS`); a setting without a default must be set by the
  experiment. Keep the names of settings stored experiments use.
- **A result of other experiments** is declared on its `Inputs:` line: `scores: Scores to compare [result]`, or
  `[result: scores]` to name the table it expects (offered first when she picks; a step's table is
  `[result: step:scale_scores]`). The recipe gets one DataFrame: the picked tables stacked (every column any of
  them has; a cell a table doesn't have is empty), with `experiment` (the source's title; two equal titles become
  "Survey · scores" / "Survey · step:x" when the tables differ, else "Survey (1)", "Survey (2)") and
  `experiment_id` in front, also for a single source. A table that has those columns already keeps them as
  `from_experiment` and `from_experiment_id`. A name ending in `_file` stays a raw file and `corrections` stays the
  corrections whatever their line says; the checker explains a bracket it can't read, a misplaced `[result]` and
  an `Inputs:` line that is really a setting.
- **Its docstring** documents them: `Inputs:` (`name: label [.ext, ...]` or `name: label [result]`), `Settings:`
  (`name: Label. Help text`, optionally under group lines like `Fitting:`; `(type)` after the name picks the form's editor: text, number,
  integer, boolean, list, object, rows; `[unit]` overrides the unit read from the name) and `Returns:` (`name:
  label`). Without `(type)` the default's type decides; a setting without a default whose name ends in a unit is a
  number.
- **A small table as a setting**: `strains (rows: strain text, dilution number, optional note text): Strains.` Its
  value is a list of row dicts (`[{"strain": "WT", "dilution": 2.0}]`); each column has a type (text, number,
  integer, boolean), may be `optional`, and gets its label and unit from its name (`rt_min` → rt [min]). The form
  edits it as a table; values are checked row by row when an experiment's settings are saved (`methodfile.columns`,
  `methodfile.check_value`), and the checker warns about a default that doesn't fit the columns.
- **Every function starts with a docstring** whose first line is one plain sentence: it is shown everywhere. A
  `Formula:` block holds formulas that aren't one expression, one per line, in Python (drawn as math) or as `$LaTeX$`.
- **Formulas** use only arithmetic (+ - * / ** and abs, sqrt, log, exp, min, max): they work on single numbers and
  on whole columns, and their body is drawn as math, once with names and once with a run's numbers.
- **Units go in names** (`conc_mM`, `time_min`, `rate_mM_per_min`): they are shown as units; there is no unit check.
- A returned table named `flags` (level, category, what it is about, message; toolbox `flag(level, category,
  message, respondent=17)` names any item by keyword) is the run's quality summary.
- The recipe returns tables, figures, single values (a number, a dict) and texts; a long text or a report object
  (a statsmodels summary) is kept as text and shown whole.
- Three plain values at the top of the file declare what isn't code (`lab/pages.py`; the panel edits them, each
  edit is a new version that rewrites only that assignment):
  - `PAGES`: the experiment's own pages, each `{"title", "cards": [...]}`. Cards: text, number, table, figure,
    chart (drawn by the panel from a table: lines, points, bars) and review (one curve per item, with regions whose
    ends are dragged and saved as a correction). A review card's curves come from a table of the run (`{"table",
    "item", "x", "y"}`) or from a raw file of the experiment (`{"file": "<input>"}`): a table file (CSV, Excel...)
    with the same item, x and y columns, any other kind through the **curve reader** the toolbox or an add-on
    registers for its extension (`toolbox/registry.py`: `register_curve_reader(".ext", fn)`, `fn(path, settings)`
    gives `[{"item", "name"?, "x", "y"}]`; the GC add-on registers `.gaml`). The curves are thinned and kept in memory
    for the last few files, so dragging a region stays fast.
  - `CORRECTIONS`: the kinds of correction an experiment records, `{kind: {"label", "help", "fields": {name:
    "text" | "number" | "integer" | "boolean", "optional ..."}, "key"}}`; approved ones reach the recipe's
    `corrections` as `{"kind", field: value, ..., "reason"}`. A method that declares none has none (proposing one
    says so).
  - `REQUIREMENTS`: extra packages from PyPI (`["pingouin>=0.5"]`), installed once per set into
    `LAYR_DATA_DIR/lab/packages/<hash>/` with the plugin's own packages as constraints (`lab/packages.py`), and put
    after them on the calculation process's path. The plugin itself has pandas, numpy, SciPy, statsmodels,
    scikit-learn, matplotlib and pyreadstat; `read_table` reads CSV, Excel, SPSS, Stata, SAS and JSON.
- Steps follow a few patterns a beginner can read: pick rows (`table[table.role == "sample"]`), "for each group:
  calculate, add a row dict to a list, `pd.DataFrame(rows)`", `merge` to join; never change a table you were given.
  The panel's **Python guide** shows each with an example.

**The style checker** (`backend/lab/checker.py`) reads the file and says, with line numbers: errors that block saving
(Python can't read it, an import can't work, no recipe) and warnings that don't (a function without a docstring, a
formula that does more than arithmetic, a step longer than about 25 lines, a function that changes a table it was
given, a recipe that does more than call steps, imports other than the toolbox, pandas, numpy, scipy and the maths
modules, file or network access outside the toolbox, a result named like a function, a `rows` setting whose columns
can't be read or whose default doesn't fit them).

## The flow

`backend/lab/flow.py` reads the recipe into a graph: one box per recipe line. `name = step(...)` is a step box whose
inputs are the step's parameters, each fed by a wire (a name an earlier box made), a setting, a raw file input, a
result input (drawn like a raw file, `kind: result`, its value "2 experiments · scores", and with a run the table it
delivered on its wire) or a value written in place; any other line (an `if`, a loop, arithmetic) is a code box showing its lines. A comment line
`# --- Title` starts a group of boxes. The boxes whose results reach only the `flags` result are the quality lane.
For each table input the flow knows which columns the step reads (from its code) and marks a wire whose recorded
table lacks one. With a run, each box has its status (done, cached, failed, not run), its time and the value on every
wire. Layout is automatic, left to right; no positions are stored.

`flow.edit(source, op)` makes a flow change as an exact edit of the recipe: add or remove a step, connect or
disconnect an input, rename a wire, add or remove a setting, an input (a raw file, or with `kind: result` a result
of other experiments) or a result, set a default, group boxes, add a code box, start a new function. Only the lines it must touch change; comments stay byte for byte; lines are wrapped
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
  calculate every step. A toolbox function can also remember slow results itself (`toolbox/cache.py`; the GC
  add-on does for its files and peaks).
- **Experiments that read experiments** (`sources.py`): an experiment keeps its picks as `sources: {input:
  [{experiment, result}]}`, checked when saved in plain words (the experiment exists and isn't this one, the table
  is one it returns or its latest run has, nothing twice, no loop of any length). Before a run each source's latest
  run must be done and have the table, else the run doesn't start and says which experiment and what to do. The
  stacked table is the run's `sources/<input>.csv`; run.json's `sources` keeps, per input, its rows, columns, file
  and fingerprint and for each source its experiment, title, label, run, finish time, result, rows and the content
  fingerprint of the table it read. Remembered steps follow the content: the same numbers, the same steps. An
  experiment is out of date when a source has a newer finished run whose table has another fingerprint (an
  identical rerun doesn't count), its latest run failed, it lost the table or was deleted, or the picks changed
  since the last run; `experiments` and `experiment_get` say so (`outdated`), and a source's finished run sends a
  `research:lab` event (`what: sources`) for each reader. Deleting an experiment others read is refused, naming
  them, unless forced.
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
  with its inputs, then the calls that gave those inputs; a number that came in through a result input ends the
  chain with a `kind: source` step naming the experiment, its run, table, row (`where`) and column.
- A run error names the method's own line and says what is wrong in plain words (a missing column: which columns
  the table does have).
- `compare.py` lists the numbers that moved between two runs. Rows are paired by identifying columns found in the
  tables themselves: of the columns holding texts, whole numbers or yes/no (never decimals) the one that tells the
  most rows apart, then the next that tells more apart, until every row is told apart; when they can't, by
  position.
- `standalone.py` exports a method as a folder that runs without layr (`python run.py`): the method, the toolbox,
  the settings, the corrections, the raw files' checksums, a runner and the requirements; a result input's stacked
  table goes into `data/<input>.csv`, so the folder runs without the other experiments. It also writes "this run
  in a notebook": a folder in JupyterLab's notebooks (the `jupyter` plugin's data folder) with the run's tables, the
  method and `explore.ipynb`.
- Reports (`export.py`, HTML / Excel / Word): what went in (raw files, the results of other experiments with their
  runs and times, settings), how every number was calculated (each step's sentence,
  formulas drawn from the code, this run's numbers), results, flags, corrections with reasons, and the source code
  as an appendix.

## Blocks and code help

- `toolbox/blocks.py`: the ready-made steps, plain functions a beginner can read ("a table in, a table out", with a
  docstring in the method style). `blocks()` builds the flow's palette from their signatures and docstrings (and an
  add-on's own `FORMS`): each one's title, sentence, what it takes and gives, the fields of its form (column,
  columns, text, number, choice, formula, table...) and the module it is imported from. A block added in the flow
  is an ordinary call in the recipe plus its name in the `from toolbox import` line (an add-on's in its own
  `from toolbox.<add-on> import` line, added when missing).
- **Toolbox add-ons** (`toolbox/registry.py`): the plugin finds them by their folders (`registry.addons()`) and
  imports them only when it needs what they register; the general toolbox never imports one. An add-on registers its
  **readers** (`register_reader(fn, label, extensions, out, param)`: what the flow's "add a raw file" offers; the
  toolbox registers `read_table`) and its **curve readers** (above). Everything that treats the toolbox specially
  also knows its add-ons: the checker, `toolbox_docs` (the add-ons as their own groups) and `toolbox_copy`, the flow
  and the editor's help, explanations, the recorder (it wraps every imported `toolbox...` function), the remembered
  steps (their key hashes every file of the toolbox, add-ons included) and the exported folder (it copies them).
- `lab/codeintel.py`: help in the editor from the plugin's own Python with Jedi: completion (columns of the tables
  the last run saw come first after `peaks.` and inside `peaks["`), hover, the signature being typed, go to
  definition, rename everywhere; Ruff lays a method out at 120 columns, only when asked. Jedi is warmed up in the
  background when the server starts.

## Tools

Methods: `methods`, `method_get`, `method_check`, `method_versions`, `method_diff`, `method_create`, `method_write`,
`method_revert`, `method_reference`, `method_delete`, `draft_get`, `draft_decide` (the user's; `source` accepts a
merged text), `toolbox_docs`, `toolbox_copy`. The flow: `flow_get`, `flow_edit` (saves nothing), `flow_new`, `blocks`.
Experiments and runs: `experiments` (with each one's tables, what it reads, who reads it, why it is out of date),
`experiment_get`, `experiment_create` and `experiment_update` (`sources`: the results of other experiments it reads),
`experiment_delete` (`force` when others read it), `examples` and `lab_sample` (make one of them), `file_import` and the upload tools, `run` (with `fresh`, or a trial run of an unsaved `source` /
`settings`), `run_stop`, `run_get`, `runs`, `compare_runs`. Results and explanations: `result_table`, `figure`,
`provenance`, `explain`, `explain_function`, `function_inside` (a function's code with the values of every line),
`value_get` (the rows of any stored table), the corrections, `export`, `method_export`, `notebook`. What a method declares: `method_declare` (pages, kinds of correction,
packages), `correction_kinds`, and for the pages `curve` (one curve for a review card) and `result_value` (a
returned value or text). The editor: `code_complete`, `code_hover`, `code_signature`,
`code_definition`, `code_rename`, `code_format`. For the panel's AI buttons: `ai_prompt` (explain, a Methods
paragraph, or write a step from her sentence). References: `reference_make`, `reference_get` (below).

## References

Another plugin (a Wiki page) refers to something in Research with an ordinary layr link,
`layr://research/open/<ref>`: `ref` is a query string (keys `experiment`, `method`, `result` (or
`step:<function>`), `row` (compact JSON of the columns that pick one row, keys sorted), `column`, `function`, and
`run` / `was`, the run and the value when it was made) put through `encodeURIComponent`, with `( ) ! ' *` encoded
too so a Markdown link isn't cut. `layr.json`'s `links` opens it in the Experiments panel with the param `ref`. The
kind follows from the keys: an experiment, a result, one number (a table's row and column, or a single returned
number), a step of an experiment (its code with the latest run's values), a function of a method, a method.
`reference_make` checks the thing exists and gives the reference with its readable Markdown link ("Copy link":
`[mean score 3.42 · group B (Attitude survey)](layr://research/open/...)`); `reference_get` gives what it shows
now, always from the experiment's latest finished run: a title, a subtitle, a body (number, table of 8 × 8, image,
text, code, facts), whether the experiment changed since that run (`stale`), the number's `was` and `now` when it
moved, or a plain sentence when the thing is gone. The core (`lab/ref_tools.py`) is tested in `tests/test_refs.py`.

## The survey example

`survey-scores` (`backend/lab/methods/survey-scores.py`) shows the plugin on a study that has nothing to do with
chemistry: Likert answers (CSV, Excel, SPSS or Stata) → corrections (leave out a respondent, recode an answer) →
reversed items turned around → a score per respondent → Cronbach's alpha → group means → an OLS regression with
statsmodels (its coefficients as a table, its full report as text) → quality checks per respondent. Its pages:
Overview (alpha, respondents, a bar chart, the regression) and Respondents (a scatter chart, the scores). Make it
with Try an example › A survey (`lab_sample` with `which: survey`).

## Examples

`backend/lab/examples/<id>.py`, one module per example, listed by whatever is there (`examples` tool: id, title,
description, method). Each has `ID`, `TITLE`, `DESCRIPTION`, `METHOD`, `EXPERIMENT` (the new experiment's title),
`ORDER`, `SETTINGS`, `CORRECTIONS` and `files(folder)`, which writes its made-up raw files (`{input id: path}`).
`lab_sample(which=<id>)` stores the files, makes the experiment and, for the first one of a method, pins it as the
method's reference experiment. An example whose method reads other experiments has `SOURCES` (the experiments to
make and run first: `{key: {title, method, settings?, files}}`) and `PICKS` (`{input: [{source: key, result}]}`);
`lab_sample` then also gives the ids of those experiments in `made`.

## The comparison example

`compare-experiments` (`backend/lab/methods/compare-experiments.py`, "Compare a result across experiments") reads a
table from each experiment picked (`results: ... [result]`) and, for one numeric column (a setting) and optionally
per group of a grouping column: n, mean, sd and se per experiment, each mean's difference from the first
experiment's (also in %), a bar chart with standard errors, and quality flags (only one experiment picked, an
experiment without numbers in the column, a single value). Try an example › Compare two surveys (`lab_sample` with
`which: compare`, `lab/examples/compare.py`) makes two survey waves with survey-scores (made-up spring and autumn
answers), runs them and makes the comparison of their `scores` per `group`.

## The GC-FID activity assay

`gc-activity-assay` (`backend/lab/methods/gc-activity-assay.py`) is Ragini's assay (gcassay, her Python package,
rewritten as one readable method): read the GAML export → sample names and design checks → reference retention
times from the standards → RT-locked peak integration (with drift correction and the corrections) → internal
standard ratio → calibration with LOD/LOQ → concentrations → time course → initial rates (U/gCDW) → interval rates →
mass balance → quality checks → figure. Its GC-only functions are the toolbox's GC add-on (`toolbox/gc/`: GAML,
peaks, the time-course figure; it registers `.gaml` as a reader and a curve reader), its example is
`lab/examples/gc.py` (a made-up GAML sequence). Its compounds and sample name patterns are `rows` settings; the
biomass is a number with an optional row per fed substrate. `tests/test_lab_real.py` proves its numbers equal
gcassay's on a real run (set `GCASSAY_PROJECT` to the gcassay project folder; the data never leaves it; the test
reads gcassay's config with its own helper, `tests/gcassay_config.py`).

## For AI agents

The server's instructions say the rules and the style; in short: numbers only from `result_table` / `provenance`
(with the run id); a method changes only through `method_write` (the whole file or one function),
which is a draft the user accepts and comes back with the checker's warnings to fix; `draft_get` shows the changed
lines and the numbers that moved; `draft_decide` is the user's. Corrections need a reason and stay proposed until the
user approves them. An agent reads the flow with `flow_get`, makes flow changes with `flow_edit` and saves the
result with `method_write` (a draft like any change), looks inside a run with `function_inside` / `value_get`, and
tries an unsaved change with `run` and `source` (a trial run that saves nothing). To compare experiments it makes
an experiment of a method with a `[result]` input and picks them with `experiment_create` / `experiment_update`
(`sources`); an experiment that is `outdated` is rerun only when someone runs it. The panel's Explain button, the
Methods paragraph and "describe a step" ask Claude through the claude plugin's `ask` tool (an optional connection;
the buttons hide without it) and mark the answer as written by AI; a step Claude writes is a draft she reviews.

## Tests

From `plugins/research`: `uv run pytest tests -q` (add `GCASSAY_PROJECT=<folder>` for the real-data comparison).
`tests/test_flow_server.py` drives 0.4 end to end through the server as layr starts it (the sample run, its flow
with a value on every wire, the inside of a step, a changed setting with remembered steps, a block added and tried,
every example made and run). `tests/test_sources.py` covers experiments that read experiments (declaring, picks
and loops, stacking, the record, out of date, remembered steps, deleting, the flow, provenance, the exported
folder, the comparison example through the server). `tests/test_generality.py` fails when a word of one method (GC, GAML, peak, compound,
injection...) appears in the core (everything under `backend/` but the methods, the examples and the toolbox's
add-ons; and `panels/pages.js`, `panels/guide.js`, `layr.json`).
The panel: start `layr serve` with this plugin linked and its own home, then
`node plugins/dev/research.test.mjs --port <port> --home <that home>`.

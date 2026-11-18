# Research

Explainable data analysis for a researcher who is not a programmer. Two panels:

- **Experiments** (`panels/lab.*`): methods, experiments, GC peak review, results, checks, corrections, the
  results database.
- **Quick analysis** (`panels/main.*`): load a CSV, describe it, chart it, t-test and ANOVA (the 0.1 plugin).

Design and research: `docs/proposals/analysis-workflows.md`.

## Words

- **Method**: a named recipe, a list of steps read top to bottom, with inputs (raw files), parameters (with
  defaults) and named outputs. Versioned: every change is a new version with a readable diff, and going back is a
  new version too. Built-in methods live in `backend/lab/methods/*.json`; ones made in layr in
  `LAYR_DATA_DIR/lab/methods/<id>/<n>.json`.
- **Step type**: one small computation (`gc.integrate`, `table.compute`, `stats.compare_groups` ...) with typed
  inputs and outputs and an explanation: a plain sentence, formulas (LaTeX), symbols with units, the rules it
  follows, assumptions, the library call, the Methods sentence, and a worked example with the run's own numbers.
  A step of type `method.call` runs another method as one step.
- **Experiment**: a method applied to raw files with parameter values and **corrections** (rename, exclude,
  manual peak boundaries), each with a written reason. Raw files are copied into the store by checksum and never
  changed.
- **Run**: one calculation of an experiment. It records the raw files' SHA-256, the parameters, the approved
  corrections, the library versions and every step's tables and facts. Steps whose inputs didn't change come from a
  cache, so a rerun after a correction takes a fraction of a second.
- **Results database**: `LAYR_DATA_DIR/lab/results.sqlite`; every output marked `database: true` holds the latest run
  of every experiment, to compare strains and conditions across the PhD.

## The GC-FID activity assay

`gc-activity-assay` is Ragini's assay (gcassay, her Python package, split into explainable steps): read the GAML
export → sample names → reference retention times from the standards → RT-locked peak integration → internal
standard ratio → calibration with LOD/LOQ → concentrations → time course → initial rates (U/gCDW) → interval rates
→ mass balance → quality checks → figure. `tests/test_lab_real.py` proves the numbers equal gcassay's on a real run
(set `GCASSAY_PROJECT` to the gcassay project folder; the data never leaves it). A gcassay config and corrections
file can be taken over when making an experiment (New experiment › Settings from a gcassay config, or
`experiment_create` with `gcassay_config`).

## For AI agents

The server's instructions say the rules; in short: numbers only from `result_table` / `provenance` /
`database_query` (with the run id); methods change only through `method_patch` (one validated batch); corrections
need a reason and stay proposed until the user approves them in the panel; `correction_decide` asks the user.
A new kind of analysis is a new method from existing step types (`step_catalog`); `table.compute` takes formulas
such as `od600 / mean(od600[treatment == "control"])` (see `backend/lab/formula.py`: parsed against a whitelist,
never `eval`).

## Adding a step type

When no step type can do an analysis, a new one is added to the plugin's code (with Claude in layr: the plugin is a
git repository, so the change shows in My plugins with its diff, and Undo / History take it back). A step type is
registered in `backend/lab/steps/<area>.py`:

```python
register(StepType(
    id="growth.rate", title="Growth rate", category="Growth",
    sentence="Fits ln(OD) against time in the exponential phase; the slope is the specific growth rate.",
    inputs=[Port("table", "table", "OD table")],
    outputs=[Port("rates", "table", "Growth rates")],
    params=[Param("window_h", "list", "Exponential phase", [2, 6], unit="h")],
    run=_run_growth,                     # (ctx, inputs, params) -> StepOut(outputs, facts, flags)
    explain=lambda p: sheet(formulas=[{"label": "Specific growth rate", "latex": r"\mu = \frac{d \ln OD}{dt}"}],
                            rules=[...], code="scipy.stats.linregress(t, ln_od)"),
    methods=lambda p, f: "Specific growth rates were ...",     # one Methods sentence
    example=lambda p, f: [{"label": "...", "latex": "..."}],   # this run's numbers in the formula
))
```

Every new step type needs: formulas that both KaTeX and matplotlib's mathtext draw (`test_every_step_explains_and_renders`
checks), a test that its result matches an independent calculation, and plain-language rules a reviewer can follow.

## Tests

From `plugins/research`: `uv run pytest tests -q` (add `GCASSAY_PROJECT=<folder>` for the real-data comparison).
The panel: start `layr serve` with this plugin linked, then `node plugins/dev/research.test.mjs --port <port>`.

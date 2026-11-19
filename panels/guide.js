// "Python for methods": the handful of Python and pandas patterns the methods use, each with a short example.
// Shown as a page of the Experiments panel (Methods › Guide); the examples are the same shapes as the built-in
// GC-FID method's code.

export const GUIDE = [
  {
    title: "A method is one file with three levels",
    text: [
      "Formulas are tiny functions on numbers. Steps take tables and give a table. The recipe calls the steps in " +
        "order and returns the results. layr draws every formula as math, shows each step's tables, and finds the " +
        "formula behind any number you click.",
    ],
    code: `def specific_activity(rate_mM_per_min, biomass_gCDW_per_L):
    """Specific activity in U/gCDW."""                       # formula
    return rate_mM_per_min * 1000 / biomass_gCDW_per_L


def initial_rates(concentrations, rate_window_min, biomass_gcdw_per_l):
    """Fit a line through the early time points."""          # step
    ...


def activity_assay(gaml_file, corrections, compounds, rate_window_min=(0, 30)):
    """Whole-cell activity assay measured by GC-FID."""       # recipe (the last function)
    injections = read_gaml(gaml_file)
    ...
    return {"activities": activities}`,
  },
  {
    title: "Functions and docstrings",
    text: [
      "def starts a function; the indented lines below are its body; return gives the result back. The first line of " +
        "the docstring (in triple quotes) is the sentence layr shows everywhere: say plainly what the function does.",
      "Put units into names: conc_mM, time_min, rate_mM_per_min. layr shows them as units in the formulas.",
    ],
    code: `def area_ratio(area, is_area):
    """A compound's peak area divided by the internal standard's area."""
    return area / is_area`,
  },
  {
    title: "Formulas work on single numbers and on whole columns",
    text: [
      "Keep formulas to + - * / ** and maths functions (abs, sqrt, log). Then the same formula works for one number " +
        "and for a whole column at once: pandas does it row by row.",
    ],
    code: `area_ratio(12.5, 25.0)                       # 0.5
ratios["ratio"] = area_ratio(ratios.area, ratios.is_area)   # every row`,
  },
  {
    title: "Picking rows",
    text: [
      "table[condition] keeps the rows where the condition is true. Combine conditions with & (and) or | (or), each " +
        'in brackets. table.column (or table["column"]) is one column.',
    ],
    code: `samples = concentrations[concentrations.role == "sample"]
early = group[group.time_min.between(0, 30)]
found = peaks[(peaks.compound == "HAME") & (peaks.status == "ok")]`,
  },
  {
    title: "For each group: calculate, add a row, make a table",
    text: [
      "The pattern most steps use. groupby splits the table into groups; the loop gets each group's key and its " +
        "rows. Build one dict per result, collect them in a list, and turn the list into a table at the end.",
    ],
    code: `rows = []
for (strain, substrate), group in samples.groupby(["strain", "substrate"]):
    line = fit_line(group.time_min, group.conc_mM)
    rows.append({"strain": strain, "substrate": substrate, "rate_mM_per_min": line.slope})
rates = pd.DataFrame(rows)`,
  },
  {
    title: "Going through rows one by one",
    text: [
      'to_dict("records") gives each row as a dict, so row["column"] is that row\'s value. Good for checks that ' +
        "write one message per row.",
    ],
    code: `flags = []
for row in balance.to_dict("records"):
    if row["pct_of_t0"] < 80:
        flags.append(flag("warning", "mass balance", f"{row['pct_of_t0']:.0f}% of t0"))`,
  },
  {
    title: "Adding a column, and never changing a table you were given",
    text: [
      "A step gets tables from earlier steps. Changing them would change the earlier step's result too, so make a " +
        "copy first (or build a new table, as merge does). The checker warns when a step changes a table it was given.",
    ],
    code: `standards = ratios[ratios.role == "standard"].copy()
standards["used"] = standards.std_conc_mM.between(low, high)`,
  },
  {
    title: "Joining two tables",
    text: [
      'merge puts the matching rows of two tables side by side, matched by a column both have. how="left" keeps ' +
        "every row of the first table.",
    ],
    code: `lines = calibration[["compound", "slope", "intercept"]]
table = ratios.merge(lines, on="compound", how="left")`,
  },
  {
    title: "Straight lines and other toolbox functions",
    text: [
      "The toolbox has the ready-made parts: read_gaml, integrate_peak, fit_line (slope, intercept, se, r2, s_yx, " +
        "n), flags and figures. Read them in the method view's Toolbox list; to change one, copy it into your method.",
    ],
    code: `from toolbox import fit_line

line = fit_line(early.time_min, early.conc_mM)
line.slope, line.se, line.r2`,
  },
  {
    title: "Text with numbers in it",
    text: [
      'An f-string puts values into text: {value} inside f"...". After a colon comes the format: .3f is three ' +
        "decimals, .0f none, +.0f with a sign, g the shortest form.",
    ],
    code: `message = f"{row['conc_mM']:.3f} mM is above the calibrated range"`,
  },
  {
    title: "The recipe and its settings",
    text: [
      "The recipe only calls steps and keeps their results. Its parameters are the experiment's inputs and settings: " +
        "names ending in _file are raw files, corrections gets the approved corrections, everything else is a " +
        "setting with its default. The docstring's Settings: section gives each setting its label, help and group.",
    ],
    code: `def analysis(table_file, value_column="value", control="control"):
    """Values relative to the control.

    Settings:
        value_column: Value column. The column with the numbers.
        control: Control group.
    """
    table = read_table(table_file)
    relative = relative_to_control(table, value_column, control)
    return {"relative": relative}`,
  },
  {
    title: "When something goes wrong",
    text: [
      "A run that fails names your function and line and says what is wrong in plain words: for a missing column, " +
        "which columns the table does have. The checker's messages (left of the line numbers) point at lines that " +
        "make a method hard to read; they never stop you from saving. Every save is a version you can go back to.",
    ],
    code: `# The table \`table\` has no column 'height'. Its columns are: plant, treatment, height_cm, ...
value = table["height"]`,
  },
];

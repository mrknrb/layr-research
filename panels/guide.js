// "Python for methods": the handful of Python and pandas patterns the methods use, each with a short example.
// Shown as a page of the Experiments panel (Python guide, on its home and in a workspace's More menu); the examples
// come from one made-up study, growth curves (OD600 of cultures over time), in the shapes every method uses.

export const GUIDE = [
  {
    title: "A method is one file with three levels",
    text: [
      "Formulas are tiny functions on numbers. Steps take tables and give a table. The recipe calls the steps in " +
        "order and returns the results. layr draws every formula as math, shows each step's tables, and finds the " +
        "formula behind any number you click.",
    ],
    code: `def doubling_time_h(growth_rate_per_h):
    """The time a culture takes to double."""                # formula
    return log(2) / growth_rate_per_h


def growth_rates(od, window_h):
    """Fit a line through the exponential phase."""          # step
    ...


def growth(plate_file, window_h=(2, 6)):
    """Growth rates from OD600."""                           # recipe (the last function)
    od = read_table(plate_file)
    ...
    return {"rates": rates}`,
  },
  {
    title: "Functions and docstrings",
    text: [
      "def starts a function; the indented lines below are its body; return gives the result back. The first line of " +
        "the docstring (in triple quotes) is the sentence layr shows everywhere: say plainly what the function does.",
      "Put units into names: od600, time_h, growth_rate_per_h. layr shows them as units in the formulas.",
    ],
    code: `def blank_corrected(od600, blank_od600):
    """A culture's OD600 minus the medium's own OD600."""
    return od600 - blank_od600`,
  },
  {
    title: "Formulas work on single numbers and on whole columns",
    text: [
      "Keep formulas to + - * / ** and maths functions (abs, sqrt, log). Then the same formula works for one number " +
        "and for a whole column at once: pandas does it row by row.",
    ],
    code: `blank_corrected(0.45, 0.05)                         # 0.4
od["od_net"] = blank_corrected(od.od600, blank_od600)   # every row`,
  },
  {
    title: "Picking rows",
    text: [
      "table[condition] keeps the rows where the condition is true. Combine conditions with & (and) or | (or), each " +
        'in brackets. table.column (or table["column"]) is one column.',
    ],
    code: `samples = od[od.role == "sample"]
early = group[group.time_h.between(2, 6)]
wild_type = od[(od.strain == "WT") & (od.od600 > 0.05)]`,
  },
  {
    title: "For each group: calculate, add a row, make a table",
    text: [
      "The pattern most steps use. groupby splits the table into groups; the loop gets each group's key and its " +
        "rows. Build one dict per result, collect them in a list, and turn the list into a table at the end.",
    ],
    code: `rows = []
for (strain, culture), group in samples.groupby(["strain", "culture"]):
    line = fit_line(group.time_h, np.log(group.od600))
    rows.append({"strain": strain, "culture": culture, "growth_rate_per_h": line.slope})
rates = pd.DataFrame(rows)`,
  },
  {
    title: "Going through rows one by one",
    text: [
      'to_dict("records") gives each row as a dict, so row["column"] is that row\'s value. Good for checks that ' +
        "write one message per row.",
    ],
    code: `flags = []
for row in rates.to_dict("records"):
    if row["r2"] < 0.98:
        flags.append(flag("warning", "fit", f"R² {row['r2']:.3f}", culture=row["culture"]))`,
  },
  {
    title: "Adding a column, and never changing a table you were given",
    text: [
      "A step gets tables from earlier steps. Changing them would change the earlier step's result too, so make a " +
        "copy first (or build a new table, as merge does). The checker warns when a step changes a table it was given.",
    ],
    code: `blanks = od[od.role == "blank"].copy()
blanks["used"] = blanks.time_h.between(low, high)`,
  },
  {
    title: "Joining two tables",
    text: [
      'merge puts the matching rows of two tables side by side, matched by a column both have. how="left" keeps ' +
        "every row of the first table.",
    ],
    code: `strains = layout[["culture", "strain", "medium"]]
table = rates.merge(strains, on="culture", how="left")`,
  },
  {
    title: "Straight lines and other toolbox functions",
    text: [
      "The toolbox has the ready-made parts: read_table, fit_line (slope, intercept, se, r2, s_yx, n), flags, " +
        "ready-made steps and charts; its add-ons bring the parts for one kind of instrument. Read them in the " +
        "Toolbox list beside the code; to change one, copy it into your method.",
    ],
    code: `from toolbox import fit_line

line = fit_line(early.time_h, np.log(early.od600))
line.slope, line.se, line.r2`,
  },
  {
    title: "Text with numbers in it",
    text: [
      'An f-string puts values into text: {value} inside f"...". After a colon comes the format: .3f is three ' +
        "decimals, .0f none, +.0f with a sign, g the shortest form.",
    ],
    code: `message = f"{row['od600']:.3f} is above the reader's linear range"`,
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
    title: "Reading another experiment's result",
    text: [
      "An input whose line in the docstring ends in [result] reads a table other experiments made instead of a file: " +
        "the experiment picks which experiments (one or several) and which of their tables. The recipe gets them " +
        "stacked as one table, with the columns experiment (the source's title) and experiment_id in front. When a " +
        "source has newer numbers, the experiment says it is out of date; run it again to use them.",
    ],
    code: `def compare(results, value_column="rate"):
    """Means of the experiments picked, side by side.

    Inputs:
        results: Results to compare [result]
    """
    means = mean_per_experiment(results, value_column)   # results has the column "experiment"
    return {"means": means}`,
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

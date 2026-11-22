"""Survey scale scores

Scale scores from Likert answers: reversed items turned around, a score per respondent, the scale's reliability
(Cronbach's alpha), the mean score of each group and a regression of the score on group and age. Respondents can be
left out and single answers recoded, each with a reason.
"""

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

from toolbox import flag, flags_table, read_table

ITEMS = ["q1", "q2", "q3", "q4", "q5", "q6"]
REVERSED = ["q3", "q5"]

# The kinds of correction an experiment can record, each with a reason; approved ones reach the recipe's `corrections`.
CORRECTIONS = {
    "drop_respondent": {
        "label": "Leave out a respondent",
        "help": "For example a test entry or a duplicate.",
        "fields": {"respondent": "integer"},
    },
    "recode_answer": {
        "label": "Recode one answer",
        "help": "Set one answer to another value; leave the answer empty to treat it as missing.",
        "fields": {"respondent": "integer", "item": "text", "answer": "optional number"},
        "key": ["respondent", "item"],
    },
}

# The experiment's own pages, made of cards (the panel's page builder writes this; it can be edited here).
PAGES = [
    {
        "title": "Overview",
        "cards": [
            {"card": "number", "value": "alpha", "title": "Cronbach's alpha", "width": "half"},
            {
                "card": "number",
                "table": "scores",
                "column": "score",
                "summary": "count",
                "title": "Respondents with a score",
                "width": "half",
            },
            {
                "card": "chart",
                "table": "group_means",
                "x": "group",
                "y": "mean_score",
                "kind": "bar",
                "title": "Mean score per group",
            },
            {"card": "table", "table": "model", "title": "Regression of the score on group and age"},
            {"card": "text", "value": "model_report", "title": "The regression's full report"},
        ],
    },
    {
        "title": "Respondents",
        "cards": [
            {
                "card": "chart",
                "table": "scores",
                "x": "age",
                "y": "score",
                "group": "group",
                "kind": "scatter",
                "title": "Score by age",
            },
            {"card": "table", "table": "scores", "title": "Scores per respondent"},
        ],
    },
]


# ---------------------------------------------------------------- formulas: one calculation each, on numbers


def reversed_answer(answer, scale_max):
    """An answer on a reversed item, turned around (on a 1-5 scale, 5 becomes 1)."""
    return scale_max + 1 - answer


def cronbach_alpha(k, sum_item_variances, total_variance):
    """Cronbach's alpha: how consistently the items measure one thing (0 to 1).

    Formula:
        alpha = k / (k - 1) * (1 - sum_item_variances / total_variance)
    """
    return k / (k - 1) * (1 - sum_item_variances / total_variance)


def standard_error(sd, n):
    """The standard error of a mean."""
    return sd / sqrt(n)


def sqrt(x):
    """The square root."""
    return x ** 0.5


# ---------------------------------------------------------------- steps: tables in, a table out


def apply_corrections(answers, items, corrections):
    """Leave out the respondents and recode the answers the corrections name."""
    dropped = [c["respondent"] for c in corrections if c["kind"] == "drop_respondent"]
    kept = answers[~answers.respondent.isin(dropped)].copy()
    for c in corrections:
        if c["kind"] == "recode_answer" and c["item"] in items:
            kept.loc[kept.respondent == c["respondent"], c["item"]] = c.get("answer", np.nan)
    return kept


def coded_answers(answers, items, reversed_items, scale_max):
    """Every answer as a number, with the reversed items turned around."""
    coded = answers[["respondent"] + items].copy()
    for item in items:
        coded[item] = pd.to_numeric(coded[item], errors="coerce")
    for item in reversed_items:
        coded[item] = reversed_answer(coded[item], scale_max)
    return coded


def scale_scores(coded, answers, items, min_answers):
    """Each respondent's score: the mean of their answers, when they answered enough items."""
    rows = []
    for row in coded.to_dict("records"):
        values = [row[item] for item in items if pd.notna(row[item])]
        score = float(np.mean(values)) if len(values) >= min_answers else np.nan
        rows.append({"respondent": row["respondent"], "answered": len(values), "score": score})
    scores = pd.DataFrame(rows)
    return scores.merge(answers[["respondent", "group", "age"]], on="respondent")


def scale_reliability(coded, items):
    """Cronbach's alpha of the items, from the respondents who answered all of them."""
    complete = coded[items].dropna()
    item_variances = complete.var(ddof=1)
    total_variance = complete.sum(axis=1).var(ddof=1)
    alpha = cronbach_alpha(len(items), item_variances.sum(), total_variance)
    return pd.DataFrame([{"items": len(items), "respondents": len(complete), "alpha": alpha}])


def alpha_of(reliability):
    """The alpha alone, as one number."""
    first = reliability.to_dict("records")[0]
    return first["alpha"]


def group_means(scores):
    """The mean score of each group, with its spread."""
    rows = []
    for group, part in scores.dropna(subset=["score"]).groupby("group"):
        sd = part.score.std(ddof=1)
        rows.append({"group": group, "respondents": len(part), "mean_score": part.score.mean(), "sd": sd,
                     "se": standard_error(sd, len(part))})
    return pd.DataFrame(rows)


def score_model(scores):
    """A regression of the score on group and age (ordinary least squares)."""
    data = scores.dropna(subset=["score"])
    return smf.ols("score ~ C(group) + age", data=data).fit()


def model_table(fit):
    """The regression's coefficients with their 95 % confidence intervals and p-values."""
    ci = fit.conf_int()
    rows = []
    for term in fit.params.index:
        rows.append({"term": term, "estimate": fit.params[term], "std_error": fit.bse[term],
                     "ci_low": ci.loc[term, 0], "ci_high": ci.loc[term, 1], "p_value": fit.pvalues[term]})
    return pd.DataFrame(rows)


def model_summary(fit):
    """The regression's full report, as statsmodels prints it."""
    return fit.summary().as_text()


def answer_checks(answers, scores, items, min_answers):
    """Quality checks: respondents who answered too few items, or gave the same answer to every item."""
    flags = []
    for row in scores[scores.answered < min_answers].to_dict("records"):
        message = f"answered {row['answered']} of {len(items)} items: no score"
        flags.append(flag("warning", "missing answers", message, respondent=row["respondent"]))
    for row in answers.to_dict("records"):
        values = [row[item] for item in items if pd.notna(row[item])]
        if len(values) == len(items) and len(set(values)) == 1:
            message = f"gave {values[0]} to every item (reversed ones too): check the answers"
            flags.append(flag("warning", "same answer", message, respondent=row["respondent"]))
    return flags_table(flags)


# ---------------------------------------------------------------- the recipe: the steps in order


def survey_scores(answers_file, corrections, items=ITEMS, reversed_items=REVERSED, scale_max=5, min_answers=4):
    """Scale scores from Likert answers.

    Inputs:
        answers_file: Survey answers, one row per respondent (respondent, group, age, the items) [.csv, .xlsx, .sav,
            .dta]
        corrections: The experiment's approved corrections: respondents left out, answers recoded.

    Settings:
        Scale:
            items (list): Items of the scale. The columns holding the answers.
            reversed_items (list): Reversed items. Items worded the other way round.
            scale_max (integer): Highest answer. 5 on a 1-5 scale.
            min_answers (integer): Fewest answers for a score.

    Returns:
        scores: Scores per respondent
        reliability: Reliability
        alpha: Cronbach's alpha
        group_means: Mean score per group
        model: Regression on group and age
        model_report: Regression report
        flags: Quality flags
    """
    raw = read_table(answers_file)
    answers = apply_corrections(raw, items, corrections)
    coded = coded_answers(answers, items, reversed_items, scale_max)
    scores = scale_scores(coded, answers, items, min_answers)
    reliability = scale_reliability(coded, items)
    alpha = alpha_of(reliability)
    means = group_means(scores)
    fit = score_model(scores)
    model = model_table(fit)
    model_report = model_summary(fit)
    flags = answer_checks(answers, scores, items, min_answers)
    return {"scores": scores, "reliability": reliability, "alpha": alpha, "group_means": means, "model": model,
            "model_report": model_report, "flags": flags}

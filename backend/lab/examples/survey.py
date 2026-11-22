"""A made-up attitude survey (a CSV of Likert answers), for the method survey-scores."""

from __future__ import annotations

from pathlib import Path

import numpy as np

ID = "survey"
TITLE = "A survey"
DESCRIPTION = "Scale scores, reliability, group means and a regression from 160 made-up respondents"
METHOD = "survey-scores"
EXPERIMENT = "Example: made-up attitude survey"
ORDER = 1
SETTINGS: dict = {}
CORRECTIONS: list[dict] = []

SURVEY_ITEMS = ["q1", "q2", "q3", "q4", "q5", "q6"]
SURVEY_REVERSED = {"q3", "q5"}


def write_survey(path: Path, seed: int = 11, n: int = 160, shift: float = 0.0) -> None:
    """A made-up survey: 160 respondents (urban, suburban, rural), age, six 1-5 answers on one attitude scale (q3 and
    q5 worded the other way round). Urban respondents and younger ones score a little higher; a few answers are
    missing, two respondents answered too few items and one gave 3 to everything. `shift` moves everyone's attitude
    (another survey wave)."""
    rng = np.random.default_rng(seed)
    groups = rng.choice(["urban", "suburban", "rural"], size=n, p=[0.4, 0.35, 0.25])
    ages = rng.integers(18, 80, size=n)
    lines = ["respondent,group,age," + ",".join(SURVEY_ITEMS)]
    for i in range(n):
        trait = rng.normal(0, 1) + {"urban": 0.45, "suburban": 0.15, "rural": -0.3}[groups[i]] \
            - 0.012 * (ages[i] - 45) + shift
        answers = []
        for item in SURVEY_ITEMS:
            a = int(np.clip(np.rint(3 + 1.05 * trait + rng.normal(0, 0.75)), 1, 5))
            if item in SURVEY_REVERSED:
                a = 6 - a
            answers.append("" if rng.random() < 0.025 else str(a))
        if i in (23, 97):
            answers = [answers[0], "", "", answers[3], "", ""]
        if i == 58:
            answers = ["3"] * len(SURVEY_ITEMS)
        lines.append(f"{i + 1},{groups[i]},{ages[i]}," + ",".join(answers))
    path.write_text("\n".join(lines) + "\n", "utf-8")


def files(folder: Path) -> dict[str, Path]:
    """The example survey's answers (always the same file)."""
    path = folder / "Example survey answers.csv"
    write_survey(path)
    return {"answers_file": path}

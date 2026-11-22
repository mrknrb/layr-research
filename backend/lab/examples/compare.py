"""Two made-up survey waves (spring and autumn), each its own experiment with survey-scores, compared with the method
compare-experiments: the comparison reads both experiments' `scores` tables."""

from __future__ import annotations

from pathlib import Path

from .survey import write_survey

ID = "compare"
TITLE = "Compare two surveys"
DESCRIPTION = ("Two made-up survey waves, each analysed as its own experiment, then their scores compared: mean, "
               "spread and differences per experiment and group")
METHOD = "compare-experiments"
EXPERIMENT = "Example: spring and autumn surveys compared"
ORDER = 3
SETTINGS = {"column": "score", "group_column": "group"}
CORRECTIONS: list[dict] = []


def spring_files(folder: Path) -> dict[str, Path]:
    """The spring wave's answers."""
    path = folder / "Example survey spring.csv"
    write_survey(path, seed=21, n=140)
    return {"answers_file": path}


def autumn_files(folder: Path) -> dict[str, Path]:
    """The autumn wave's answers: the same questions, attitudes a little more positive."""
    path = folder / "Example survey autumn.csv"
    write_survey(path, seed=37, n=150, shift=0.8)
    return {"answers_file": path}


SOURCES = {
    "spring": {"title": "Example: survey in spring", "method": "survey-scores", "files": spring_files},
    "autumn": {"title": "Example: survey in autumn", "method": "survey-scores", "files": autumn_files},
}
PICKS = {"results": [{"source": "spring", "result": "scores"}, {"source": "autumn", "result": "scores"}]}

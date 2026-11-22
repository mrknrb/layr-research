"""Reading an experiment's design from the sample names."""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=256)
def _compiled(regex: str) -> re.Pattern:
    return re.compile(regex)


def match_name(name: str, patterns: list[dict]) -> tuple[str, dict[str, str]]:
    """Which pattern a sample name matches, and the parts it names: (role, {part: text}).

    `patterns` is a list of {"role": ..., "regex": ...}, tried in order; the first that matches wins. Named groups of
    the regular expression, like (?P<time_min>\\d+), become the parts. A name no pattern matches gives
    ("unknown", {}).

    Example: match_name("Growth_WT_5h_R2", [{"role": "sample", "regex":
    r"^Growth_(?P<strain>\\w+)_(?P<time_h>\\d+)h_R(?P<replicate>\\d+)$"}])
    gives ("sample", {"strain": "WT", "time_h": "5", "replicate": "2"}).
    """
    for pattern in patterns:
        try:
            role, regex = pattern["role"], _compiled(pattern["regex"])
        except (KeyError, TypeError, re.error) as e:
            raise ValueError(f"The sample pattern {pattern} can't be used: {e}") from e
        found = regex.match(name)
        if found:
            parts = {k: v for k, v in found.groupdict().items() if v is not None}
            return role, parts
    return "unknown", {}

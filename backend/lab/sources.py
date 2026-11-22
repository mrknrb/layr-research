"""Experiments that read other experiments: the end result of one experiment is the start of another.

A recipe parameter whose `Inputs:` line ends in `[result]` (or `[result: scores]`, the result it expects) takes a
result of other experiments (lab/methodfile.py). An experiment picks, for each such input, one or several sources:

    exp["sources"] = {"scores": [{"experiment": "e1a2b3c4d", "result": "scores"},
                                 {"experiment": "e5f6a7b8c", "result": "step:scale_scores"}]}

`result` is a table the source's recipe returns, or `step:<function>` for a step's table. When the experiment runs,
each source's latest run must be done and have that table; the picked tables are stacked into one (all their columns;
a cell a table doesn't have is empty) with two columns in front: `experiment` (the source's title, made distinct when
two are the same) and `experiment_id`. A table that has those columns already keeps them as `from_experiment` and
`from_experiment_id`. The run records what it read: per input the stacked table (rows, columns, its file
`sources/<input>.csv`, a fingerprint) and, for each source, its experiment, title, run, result, rows and fingerprint.

An experiment is **out of date** when a source has a newer finished run whose table differs (an identical rerun, the
same content fingerprint, doesn't count), its latest run failed, it lost the table, was deleted, or the picks changed
since its latest run. It is never rerun by itself.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

import pandas as pd

from . import methodfile
from .errors import LabError
from .store import Store

_FINGERPRINTS: dict[tuple[str, int, int], str] = {}


# ---------------------------------------------------------------------------------------------------- small helpers


def picks(exp: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The experiment's picked sources per result input."""
    return {k: list(v) for k, v in (exp.get("sources") or {}).items() if v}


def reads(exp: dict[str, Any]) -> set[str]:
    """The ids of the experiments it reads."""
    return {s["experiment"] for v in picks(exp).values() for s in v}


def title_of(exp: dict[str, Any] | None, exp_id: str) -> str:
    return (exp or {}).get("title") or exp_id


def fingerprint(path: os.PathLike | str) -> str:
    """A table file's content fingerprint (start of its SHA-256), remembered while the file is unchanged."""
    st = os.stat(path)
    key = (str(path), st.st_size, st.st_mtime_ns)
    got = _FINGERPRINTS.get(key)
    if got is None:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        got = _FINGERPRINTS[key] = h.hexdigest()[:16]
        if len(_FINGERPRINTS) > 2000:
            _FINGERPRINTS.pop(next(iter(_FINGERPRINTS)))
    return got


class Runs:
    """Run records read at most once per question (listing every experiment reads many)."""

    def __init__(self, store: Store):
        self.store = store
        self.kept: dict[str, dict[str, Any] | None] = {}

    def get(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        if run_id not in self.kept:
            try:
                self.kept[run_id] = self.store.get_run(run_id)
            except (LabError, OSError, ValueError):
                self.kept[run_id] = None
        return self.kept[run_id]

    def latest(self, exp: dict[str, Any] | None) -> dict[str, Any] | None:
        return self.get(((exp or {}).get("last_run") or {}).get("id"))


def tables(run: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """A run's returned tables and its steps' tables (`step:<function>`)."""
    if not run:
        return [], []
    returned = [n for n, o in (run.get("outputs") or {}).items() if o.get("kind") == "table"]
    steps = [n for n in Store.table_names(run) if n.startswith("step:")]
    return returned, list(dict.fromkeys(steps))


def declared_results(store: Store, exp: dict[str, Any]) -> list[str]:
    """The results the experiment's method documents under `Returns:` (what it will return once it runs)."""
    try:
        m = methodfile.parse(store.get_method(exp["method"], exp.get("method_version"))["source"])
    except (LabError, SyntaxError):
        return []
    return list(m.recipe_function.doc.returns) if m.recipe_function else []


def result_file(store: Store, run: dict[str, Any], result: str):
    """The file of a run's table (a returned one or a step's), or None."""
    out = Store.output_file(run, result)
    if not out or out.get("kind") != "table" or not out.get("file"):
        return None
    p = store.run_dir(run["id"]) / out["file"]
    return p if p.exists() else None


def when(stamp: str | None) -> str:
    """A run's time as people read it: 2026-11-22 14:03."""
    return (stamp or "")[:16].replace("T", " ")


# ---------------------------------------------------------------------------------------------------- picking


def check(store: Store, exp: dict[str, Any], inputs: list[dict[str, Any]], given: dict[str, Any] | None,
          everyone: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    """The experiment's sources with `given` put over them ({input: [{experiment, result}, ...]}; null or [] clears
    an input), each checked: the input takes results, the experiment exists and isn't this one, the result is a table
    it returns (its latest run has it, or its method documents it), nothing picked twice, no loop. Raises LabError in
    plain words."""
    out = picks(exp)
    if not given:
        return out
    by_input = {i["id"]: i for i in inputs if i.get("kind") == "result"}
    runs = Runs(store)
    for input_id, chosen in given.items():
        if input_id not in by_input:
            files = [i["id"] for i in inputs if i.get("kind") == "file"]
            hint = f" `{input_id}` is a raw file: give it in `files`." if input_id in files else ""
            raise LabError(f"The method has no input `{input_id}` that takes results of other experiments. Such "
                           f"inputs: {', '.join(by_input) or 'none'}.{hint}")
        if not chosen:
            out.pop(input_id, None)
            continue
        if isinstance(chosen, dict):
            chosen = [chosen]
        label = by_input[input_id]["label"]
        suggested = by_input[input_id].get("result")
        clean: list[dict[str, Any]] = []
        for c in chosen:
            if isinstance(c, str):
                c = {"experiment": c}
            if not isinstance(c, dict) or not c.get("experiment"):
                raise LabError(f"{label}: each pick is {{\"experiment\": id, \"result\": table name}}.")
            src_id = str(c["experiment"]).strip()
            result = str(c.get("result") or suggested or "").strip()
            if src_id == exp["id"]:
                raise LabError(f"{label}: an experiment can't read its own results. Pick another experiment.")
            try:
                src = store.get_experiment(src_id)
            except LabError:
                raise LabError(f"{label}: there is no experiment {src_id} (the experiments tool lists them).") from None
            name = title_of(src, src_id)
            if not result:
                raise LabError(f"{label}: say which of the tables of '{name}' to read (\"result\": its name).")
            run = runs.latest(src)
            returned, steps = tables(run)
            if run is not None and (run.get("outputs") or {}).get(result, {}).get("kind") not in (None, "table"):
                raise LabError(f"{label}: '{name}' returns `{result}` as a single value, not a table. Its tables: "
                               f"{', '.join(returned) or 'none'}.")
            if result not in returned and result not in steps and result not in declared_results(store, src):
                have = ", ".join(returned) or "none yet (it hasn't run)"
                raise LabError(f"{label}: '{name}' has no table `{result}`. Its tables: {have}.")
            if any(x["experiment"] == src_id and x["result"] == result for x in clean):
                raise LabError(f"{label}: '{name}' · {result} is picked twice.")
            clean.append({"experiment": src_id, "result": result})
        out[input_id] = clean
    loop = find_loop(store, exp["id"], {s["experiment"] for v in out.values() for s in v}, everyone)
    if loop:
        names = {e["id"]: title_of(e, e["id"]) for e in (everyone or store.list_experiments())}
        names[exp["id"]] = title_of(exp, exp["id"])
        said = ", ".join(f"'{names.get(a, a)}' reads '{names.get(b, b)}'" for a, b in zip(loop, loop[1:]))
        raise LabError(f"That would make a loop: {said}. An experiment can't read its own results, not even "
                       "through other experiments.")
    return out


def find_loop(store: Store, exp_id: str, new_reads: set[str], everyone: list[dict[str, Any]] | None = None
              ) -> list[str] | None:
    """A chain exp_id → … → exp_id the new picks would make ([exp_id, a, b, exp_id]), or None."""
    graph = {e["id"]: reads(e) for e in (everyone or store.list_experiments())}
    graph[exp_id] = set(new_reads)
    path: list[str] = []
    seen: set[str] = set()

    def walk(node: str) -> bool:
        path.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt == exp_id:
                path.append(nxt)
                return True
            if nxt not in seen:
                seen.add(nxt)
                if walk(nxt):
                    return True
        path.pop()
        return False

    return path if walk(exp_id) else None


def readers(exp_id: str, everyone: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who reads this experiment: [{experiment, title, input}]."""
    out = []
    for e in everyone:
        for input_id, chosen in picks(e).items():
            if any(s["experiment"] == exp_id for s in chosen):
                out.append({"experiment": e["id"], "title": e.get("title"), "input": input_id})
    return out


# ---------------------------------------------------------------------------------------------------- running


def _labels(items: list[dict[str, Any]]) -> list[str]:
    """Each source's name in the stacked table: its title; two the same are told apart by their result when that
    differs, else numbered: "Survey (2)"."""
    titles = [i["title"] for i in items]
    labels = list(titles)
    for t in set(titles):
        same = [k for k, x in enumerate(titles) if x == t]
        if len(same) < 2:
            continue
        results = [items[k]["result"] for k in same]
        for n, k in enumerate(same, 1):
            labels[k] = f"{t} · {items[k]['result']}" if len(set(results)) == len(results) else f"{t} ({n})"
    return labels


def stack(parts: list[tuple[str, str, pd.DataFrame]]) -> pd.DataFrame:
    """The picked tables as one: (label, experiment id, table) each, all columns, `experiment` and `experiment_id`
    first (a table's own columns of those names become `from_experiment`, `from_experiment_id`)."""
    frames = []
    for label, exp_id, df in parts:
        df = df.copy()
        for col in ("experiment_id", "experiment"):
            if col in df.columns:
                new = "from_" + col
                while new in df.columns:
                    new = "from_" + new
                df = df.rename(columns={col: new})
        df.insert(0, "experiment_id", exp_id)
        df.insert(0, "experiment", label)
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["experiment", "experiment_id"])
    return pd.concat(frames, ignore_index=True, sort=False)


def gather(store: Store, exp: dict[str, Any], results: dict[str, str | None], optional: set[str]
           ) -> dict[str, dict[str, Any]]:
    """For each result input of the recipe: {"table": the stacked DataFrame, "record": what was read}. Inputs
    without picks that are optional are left out. Raises LabError, naming the experiment and what to do, when a
    source can't be read (deleted, never ran, its latest run failed, it lacks the table)."""
    chosen = picks(exp)
    out: dict[str, dict[str, Any]] = {}
    for input_id in results:
        sources = chosen.get(input_id) or []
        if not sources:
            if input_id in optional:
                continue
            raise LabError(f"The input `{input_id}` has no results picked: pick the experiments (and their table) "
                           "it reads in the experiment's inputs.")
        items = []
        for s in sources:
            try:
                src = store.get_experiment(s["experiment"])
            except LabError:
                raise LabError(f"The experiment {s['experiment']} that `{input_id}` reads was deleted: pick another "
                               "one or remove it from the experiment's inputs.") from None
            name = title_of(src, src["id"])
            last = src.get("last_run")
            if not last:
                raise LabError(f"'{name}' hasn't been run yet, so `{input_id}` has nothing to read: run '{name}' "
                               "first.")
            if last.get("status") != "done":
                raise LabError(f"The latest run of '{name}' {last.get('status') or 'failed'}, so `{input_id}` has "
                               f"nothing to read: fix and run '{name}' first.")
            run = store.get_run(last["id"])
            path = result_file(store, run, s["result"])
            if path is None:
                have = ", ".join(tables(run)[0]) or "none"
                raise LabError(f"The latest run of '{name}' has no table `{s['result']}` for `{input_id}`. Its "
                               f"tables: {have}. Pick one of them.")
            try:
                df = pd.read_csv(path)
            except pd.errors.EmptyDataError:
                df = pd.DataFrame(columns=(Store.output_file(run, s["result"]) or {}).get("columns") or [])
            items.append({"experiment": src["id"], "title": name, "method": src.get("method"), "run": run["id"],
                          "finished": run.get("finished"), "result": s["result"], "rows": int(len(df)),
                          "fingerprint": fingerprint(path), "df": df})
        labels = _labels(items)
        table = stack([(lab, i["experiment"], i["df"]) for lab, i in zip(labels, items)])
        record = {"rows": int(len(table)), "columns": [str(c) for c in table.columns],
                  "from": [{**{k: v for k, v in i.items() if k != "df"}, "label": lab}
                           for lab, i in zip(labels, items)]}
        out[input_id] = {"table": table, "record": record}
    return out


# ---------------------------------------------------------------------------------------------------- out of date


def outdated(store: Store, exp: dict[str, Any], by_id: dict[str, dict[str, Any]], runs: Runs | None = None
             ) -> list[dict[str, Any]]:
    """Why the experiment's latest run no longer shows what its sources hold: [{input, experiment, title,
    reason}] (empty: up to date, or it doesn't read others, or it hasn't run)."""
    runs = runs or Runs(store)
    last = runs.latest(exp)
    chosen = picks(exp)
    if last is None:
        return []
    read = last.get("sources") or {}
    out: list[dict[str, Any]] = []
    for input_id in list(dict.fromkeys([*chosen, *read])):
        now = [(s["experiment"], s["result"]) for s in chosen.get(input_id) or []]
        then = {(f["experiment"], f["result"]): f for f in (read.get(input_id) or {}).get("from") or []}

        def say(exp_id: str, reason: str, title: str | None = None) -> None:
            out.append({"input": input_id, "experiment": exp_id,
                        "title": title or title_of(by_id.get(exp_id), exp_id), "reason": reason})

        for key in now:
            if key not in then:
                say(key[0], f"Picked ({key[1]}) after the last run: run this experiment to read it.")
        for key, f in then.items():
            if key not in now:
                say(key[0], f"No longer picked, but the last run read its {key[1]}.", f.get("title"))
        for exp_id, result in now:
            f = then.get((exp_id, result))
            if f is None:
                continue
            src = by_id.get(exp_id)
            if src is None:
                say(exp_id, "This experiment was deleted: pick another one, or remove it.", f.get("title"))
                continue
            lr = src.get("last_run") or {}
            if not lr or lr.get("id") == f.get("run"):
                continue
            if lr.get("status") != "done":
                say(exp_id, f"Its latest run {lr.get('status') or 'failed'} ({when(lr.get('finished'))}): run it "
                            "again before this experiment.")
                continue
            newer = runs.get(lr["id"])
            path = result_file(store, newer, result) if newer else None
            if path is None:
                say(exp_id, f"Its latest run ({when(lr.get('finished'))}) has no table {result} any more.")
            elif fingerprint(path) != f.get("fingerprint"):
                say(exp_id, f"It has a newer run ({when(lr.get('finished'))}) with other numbers in {result}.")
    return out


def view(store: Store, exp: dict[str, Any], inputs: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
         runs: Runs | None = None) -> dict[str, list[dict[str, Any]]]:
    """What is picked for each result input, as the experiment's screen shows it: {input: [{experiment, result,
    title, method, missing, last_run: {id, status, finished} | null, has_result}]}."""
    runs = runs or Runs(store)
    out: dict[str, list[dict[str, Any]]] = {}
    for i in inputs:
        if i.get("kind") != "result":
            continue
        items = []
        for s in picks(exp).get(i["id"]) or []:
            src = by_id.get(s["experiment"])
            item: dict[str, Any] = {"experiment": s["experiment"], "result": s["result"],
                                    "title": title_of(src, s["experiment"]), "method": (src or {}).get("method"),
                                    "missing": src is None, "last_run": None, "has_result": False}
            lr = (src or {}).get("last_run")
            if lr:
                item["last_run"] = {"id": lr.get("id"), "status": lr.get("status"), "finished": lr.get("finished")}
                run = runs.get(lr.get("id"))
                item["has_result"] = bool(run and lr.get("status") == "done" and result_file(store, run, s["result"]))
            items.append(item)
        out[i["id"]] = items
    return out

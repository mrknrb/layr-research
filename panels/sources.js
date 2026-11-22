// What an experiment reads of other experiments (an input of its method marked `[result]`): the picker and the
// chips that show what is picked. lab.js uses them in New experiment, the Settings tab and for the flow's input box.
//
//   const SRC = createSources({ h, put, openDialog, when, stateWord, onOpen: (experimentId) => … });
//   SRC.chips(items, { onRemove })   → the picked experiments as chips (each opens its experiment)
//   await SRC.pick({ input, picked, exps, methods, self, save })   → [{experiment, result}] or null (closed)

/** "scores", or a step's table "step:scale_scores" as "scale_scores (a step's table)". */
export const tableWord = (r) => (String(r || "").startsWith("step:") ? `${r.slice(5)} (a step's table)` : String(r || ""));

/** Why a picked source can't be read now (a sentence), or null. `item` as experiment_get's `sources` give it. */
export function sourceProblem(item) {
  if (item.missing) return "This experiment was deleted: pick another one, or remove it.";
  if (item.has_result !== false) return null;
  const st = item.last_run?.status;
  if (!st) return "It hasn't been calculated yet: run it first.";
  if (st !== "done") return `Its latest run ${st === "stopped" ? "was stopped" : "failed"}: run it again first.`;
  return `Its latest run has no table ${tableWord(item.result)}.`;
}

export function createSources({ h, put, openDialog, when, stateWord, onOpen }) {
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

  /** The picked experiments in reading order: "1. Spring survey · scores ×" (the title opens it). */
  function chips(items, { onRemove } = {}) {
    if (!items.length) return h("span", { class: "layr-muted src-none" }, "nothing picked yet");
    return h(
      "span",
      { class: "src-chips" },
      items.map((it, i) => {
        const problem = sourceProblem(it);
        const title = it.title || it.experiment;
        return h(
          "span",
          { class: `src-chip${problem ? " bad" : ""}`, title: problem || `Open ${title}` },
          h(
            "a",
            {
              href: "#",
              onclick: (e) => {
                e.preventDefault();
                if (!it.missing) onOpen(it.experiment);
              },
            },
            items.length > 1 ? h("span", { class: "src-n" }, `${i + 1}.`) : null,
            h("b", {}, title),
            h("span", { class: "src-table" }, ` · ${tableWord(it.result)}`),
          ),
          problem ? h("span", { class: "src-warn", "aria-label": problem }, "!") : null,
          onRemove
            ? h(
                "button",
                {
                  class: "src-x",
                  title: `Stop reading ${title}`,
                  "aria-label": `Stop reading ${title}`,
                  onclick: () => onRemove(i),
                },
                "×",
              )
            : null,
        );
      }),
    );
  }

  /** The tables an experiment offers, the input's usual one first, then its returned ones, then each step's. */
  function tableOptions(e, input, current) {
    const returned = e.results || [];
    const steps = e.step_results || [];
    const first = input.result && [...returned, ...steps].includes(input.result) ? input.result : null;
    const opt = (value, label) => h("option", { value, selected: value === current }, label);
    return [
      first ? opt(first, `${tableWord(first)} (usual)`) : null,
      returned.filter((r) => r !== first).map((r) => opt(r, r)),
      current && !returned.includes(current) && !steps.includes(current)
        ? opt(current, `${tableWord(current)} (not in its latest run)`)
        : null,
      steps.filter((s) => s !== first).length
        ? h(
            "optgroup",
            { label: "each step's table" },
            steps.filter((s) => s !== first).map((s) => opt(s, s.slice(5))),
          )
        : null,
    ];
  }

  /**
   * The picker: the experiments that have tables, grouped by method, each with its state and last run, a box to
   * tick and the table to read; what will be read is listed under them in reading order. `save(list)` → a sentence
   * when it was refused (shown, the picker stays open) or null. → the picks, or null when closed.
   */
  async function pick({ input, picked = [], exps, methods = [], self = null, save }) {
    const chosen = new Map(picked.map((p) => [p.experiment, p.result])); // its order is the reading order
    const others = exps.filter((e) => e.experiment !== self);
    const usable = others.filter((e) => (e.results || []).length || (e.step_results || []).length);
    const without = others.length - usable.length;
    const byId = Object.fromEntries(exps.map((e) => [e.experiment, e]));
    const titleOf = (id) => byId[id]?.title || picked.find((p) => p.experiment === id)?.title || id;
    const summary = h("div", { class: "src-summary", "aria-live": "polite" });
    const err = h("div", { class: "layr-error", hidden: true });
    const drawSummary = () =>
      put(
        summary,
        chosen.size
          ? [
              h("div", { class: "src-sum-head" }, `It will read ${plural(chosen.size, "table")}, in this order:`),
              h(
                "ol",
                {},
                [...chosen].map(([id, r]) => h("li", {}, h("b", {}, titleOf(id)), h("span", { class: "layr-muted" }, ` · ${tableWord(r)}`))),
              ),
            ]
          : h("span", { class: "layr-muted" }, "Nothing picked yet: tick the experiments to read."),
      );
    const rowOf = (e) => {
      const box = h("input", { type: "checkbox", checked: chosen.has(e.experiment) });
      const sel = h(
        "select",
        { class: "layr-select small src-table-pick", "aria-label": `The table read from ${e.title}` },
        tableOptions(e, input, chosen.get(e.experiment)),
      );
      if (!chosen.has(e.experiment)) {
        const usual = input.result && [...(e.results || []), ...(e.step_results || [])].includes(input.result);
        sel.value = usual ? input.result : (e.results || [])[0] || (e.step_results || [])[0];
      }
      const last = e.last_run;
      const state = [
        stateWord(e),
        last?.finished ? when(last.finished) : null,
        e.outdated?.length ? "out of date" : null,
      ].filter(Boolean);
      const row = h(
        "div",
        { class: `src-row${chosen.has(e.experiment) ? " on" : ""}`, "data-exp": e.experiment },
        h(
          "label",
          { class: "src-what" },
          box,
          h("span", { class: "src-name" }, h("b", {}, e.title), h("span", { class: "layr-muted small" }, state.join(" · "))),
        ),
        h("span", { class: "src-from layr-muted small" }, "table"),
        sel,
      );
      const changed = () => {
        if (box.checked) chosen.set(e.experiment, sel.value);
        else chosen.delete(e.experiment);
        row.classList.toggle("on", box.checked);
        drawSummary();
      };
      box.addEventListener("change", changed);
      sel.addEventListener("change", () => {
        box.checked = true;
        changed();
      });
      return row;
    };
    // grouped by method, in the order the methods are listed
    const order = (m) => {
      const i = methods.findIndex((x) => x.id === m);
      return i < 0 ? methods.length : i;
    };
    const groups = new Map();
    for (const e of [...usable].sort((a, b) => order(a.method) - order(b.method) || a.title.localeCompare(b.title)))
      (groups.get(e.method) || groups.set(e.method, []).get(e.method)).push(e);
    const list = h(
      "div",
      { class: "src-list" },
      usable.length
        ? [...groups].map(([m, es]) =>
            h(
              "div",
              { class: "src-group" },
              h("div", { class: "src-method" }, methods.find((x) => x.id === m)?.name || es[0].method_name || m),
              es.map(rowOf),
            ),
          )
        : h("div", { class: "layr-muted pad" }, "No other experiment has tables yet: run one first."),
    );
    const find = h("input", { class: "layr-input src-find", type: "search", placeholder: "Find an experiment or a method" });
    find.addEventListener("input", () => {
      const q = find.value.trim().toLowerCase();
      for (const g of list.querySelectorAll(".src-group")) {
        const inMethod = g.querySelector(".src-method").textContent.toLowerCase().includes(q);
        let any = false;
        for (const r of g.querySelectorAll(".src-row")) {
          const hit = !q || inMethod || r.querySelector(".src-name b").textContent.toLowerCase().includes(q);
          r.hidden = !hit;
          any ||= hit;
        }
        g.hidden = !any;
      }
    });
    drawSummary();
    setTimeout(() => (usable.length > 6 ? find : list.querySelector("input"))?.focus(), 30);
    return openDialog({
      title: `What it reads: ${input.label || input.id}`,
      size: "medium",
      body: [
        h(
          "p",
          { class: "layr-muted src-intro" },
          "Tick the experiments to read and pick the table each gives. Their latest runs' tables are put one under " +
            "the other, with a column saying which experiment each row came from. When one of them is calculated " +
            "again with other numbers, this experiment says it is out of date.",
        ),
        usable.length > 6 ? find : null,
        list,
        without ? h("div", { class: "layr-muted small" }, `Not shown: ${plural(without, "experiment")} without tables yet (not calculated, or the last run failed).`) : null,
        summary,
        err,
      ],
      buttons: [
        { label: "Cancel", id: null, kind: "ghost" },
        {
          label: "Read these",
          kind: "primary",
          value: async () => {
            const list = [...chosen].map(([experiment, result]) => ({ experiment, result }));
            const problem = save ? await save(list) : null;
            if (!problem) return list;
            put(err, problem);
            err.hidden = false;
            return undefined;
          },
        },
      ],
    });
  }

  return { chips, pick };
}

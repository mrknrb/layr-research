// Links in the Experiments panel: "Copy link" on every kind of thing (an experiment from More ▾ and from its home
// card, the method, a table, a figure, a text and a single number of the Results tab, one number from "how was it
// calculated" and from a cell's right-click, also in a step's table, a step from its box's menu and from the
// look-inside panel, a function from the outline) puts exactly reference_make's Markdown on the clipboard; then each
// link is opened as layr opens it (the param `ref`) and the panel must land there: one number scrolled into view and
// highlighted (also in a table longer than the rows shown, with a filter on before), a step looked inside, a function
// unfolded in the code. Broken links go as far as they can with one notice; a panel started by a link lands too.
// Screenshots (dark and light) in tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/links.e2e.mjs
import { bench } from "./lib.mjs";

const READER = `"""Doubled scores

Doubles the scores of other experiments.
"""


def double(value):
    """Twice the value."""
    return value * 2


def doubled(results):
    """Every score doubled."""
    out = results.copy()
    out["doubled"] = double(out["score"])
    return out


def read_results(results):
    """Doubled scores.

    Inputs:
        results: Scores of other experiments [result: scores]

    Returns:
        doubled: Doubled scores
    """
    doubled_scores = doubled(results)
    return {"doubled": doubled_scores}
`;
const PREFIX = "layr://research/open/";

await bench("links", async ({ h, open, shot, until, step }) => {
  step("the comparison example and a 290-row table (both surveys' scores doubled), made and run through the tools");
  const cmp = await h.tool("lab_sample", { which: "compare" });
  const [spring, autumn] = cmp.made;
  const must = async (exp) => {
    const r = await h.tool("run", { experiment: exp, wait_s: 55 });
    if (r.status !== "done") throw new Error(`${exp}: ${r.status} ${JSON.stringify(r.error)}`);
  };
  await must(cmp.experiment);
  await h.tool("method_create", { method: "doubled-scores", name: "Doubled scores", source: READER });
  const long = await h.tool("experiment_create", {
    title: "Both waves doubled", method: "doubled-scores",
    sources: { results: [{ experiment: spring, result: "scores" }, { experiment: autumn, result: "scores" }] },
  });
  await must(long.experiment);

  let { page, errors } = await open();
  async function stop(name) {
    await page.waitForTimeout(250);
    await shot(page, `${name}-dark`);
    await h.theme("light");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await page.waitForTimeout(150);
    await shot(page, `${name}-light`);
    await h.theme("dark");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  }
  const panel = () => page.evaluate(() => window.__lab.panel());
  const go = (target) => page.evaluate((t) => window.__lab.show(t), target);
  const lastSeq = async () => (await h.log(0)).reduce((m, e) => Math.max(m, e.seq), 0);
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });

  // ------------------------------------------------------------------ Copy link, every kind
  const links = {};
  /** Runs `act`, then checks the clipboard got exactly reference_make's Markdown for the link it holds. */
  async function copied(name, act, want) {
    const since = await lastSeq();
    await act();
    const clip = await until(`the clipboard (${name})`, async () => (await h.log(since, "clipboard")).at(-1), 20_000);
    const m = /\]\((layr:\/\/research\/open\/[^)\s]+)\)$/.exec(clip.text);
    if (!m) throw new Error(`${name}: not a Markdown link: ${clip.text}`);
    const ref = decodeURIComponent(m[1].slice(PREFIX.length));
    const q = Object.fromEntries(new URLSearchParams(ref));
    const args = {};
    for (const k of ["experiment", "method", "result", "column", "function"]) if (q[k] != null) args[k] = q[k];
    if (q.row) args.row = JSON.parse(q.row);
    for (const [k, v] of Object.entries(want))
      if (JSON.stringify(args[k]) !== JSON.stringify(v)) throw new Error(`${name}: ${k} is ${JSON.stringify(args[k])}, not ${JSON.stringify(v)}`);
    const made = await h.tool("reference_make", args);
    if (made.markdown !== clip.text) throw new Error(`${name}: the clipboard has\n  ${clip.text}\nreference_make gives\n  ${made.markdown}`);
    const notes = (await h.log(since, "notify")).map((n) => n.text);
    if (!notes.some((t) => /Link copied: paste it in a Wiki page/.test(t))) throw new Error(`${name}: no notice (${notes})`);
    links[name] = { ref, args };
  }

  step("an experiment: More ▾ and the home card's menu; the method: More ▾");
  await copied("experiment (card)", async () => {
    await h.answer("menu", "Copy link to this experiment");
    await page.locator('.exp-more[aria-label="More for Example: survey in autumn"]').click();
  }, { experiment: autumn });
  await go({ exp: spring, tab: "results" });
  await page.locator(".result-item.active").waitFor({ timeout: 30_000 });
  await copied("experiment", async () => {
    await h.answer("menu", "Copy link to this experiment");
    await page.locator(".actions button", { hasText: "More" }).click();
  }, { experiment: spring });
  await copied("method", async () => {
    await h.answer("menu", "Copy link to the method");
    await page.locator(".actions button", { hasText: "More" }).click();
  }, { method: "survey-scores" });

  step("the Results tab: a table, a text, a single number; the comparison's figure");
  const entry = async (exp, id) => {
    await go({ exp, result: id });
    await page.locator(`.result-item.active[data-id="${id}"]`).waitFor({ timeout: 30_000 });
    await page.locator(".result-shown .layr-spinner").waitFor({ state: "detached", timeout: 30_000 });
  };
  await entry(spring, "scores");
  await copied("table", () => page.locator(".result-head .result-link").click(), { experiment: spring, result: "scores" });
  await entry(spring, "model_report");
  await copied("text", () => page.locator(".result-head .result-link").click(), { experiment: spring, result: "model_report" });
  await entry(spring, ":numbers");
  await copied("value", () => page.locator('.value-card[data-name="alpha"] .result-link').click(), { experiment: spring, result: "alpha" });
  await entry(cmp.experiment, "chart");
  await copied("figure", () => page.locator(".result-head .result-link").click(), { experiment: cmp.experiment, result: "chart" });

  step("one number: from how it was calculated, and from a cell's right-click in a step's table");
  const cellOf = (rowText, column) => page.evaluate(([rowText, column]) => {
    const t = document.querySelector(".result-shown .layr-table");
    const ci = [...t.querySelectorAll("thead th")].findIndex((th) => th.textContent === column);
    const tr = [...t.querySelectorAll("tbody tr")].find((r) => [...r.children].some((td) => td.textContent === rowText));
    tr.children[ci].setAttribute("data-test", "cell");
    return tr.children[ci].textContent;
  }, [rowText, column]);
  await entry(spring, "group_means");
  await cellOf("urban", "mean_score");
  await page.locator('[data-test="cell"]').click();
  const dlg = page.locator("#dialog");
  await dlg.locator("h3", { hasText: "How mean_score" }).waitFor({ timeout: 20_000 });
  await copied("number", () => dlg.locator("button", { hasText: "Copy link to this number" }).click(),
    { experiment: spring, result: "group_means", column: "mean_score", row: { group: "urban" } });
  if (!(await dlg.evaluate((d) => d.open))) throw new Error("Copy link closed the dialog");
  await stop("84-copy-number-dialog");
  await dlg.locator("button", { hasText: "Close" }).click();
  await entry(spring, "step:group_means");
  await cellOf("rural", "mean_score");
  await copied("step table number", async () => {
    await h.answer("menu", "Copy link to this number");
    await page.locator('[data-test="cell"]').click({ button: "right" });
  }, { experiment: spring, result: "step:group_means", column: "mean_score", row: { group: "rural" } });

  step("a step: its box's menu and reading it under the flow; a function: the outline beside the code");
  await go({ exp: spring, tab: "flow", code: false });
  await until("the flow", async () => (await page.locator(".fl-node").count()) > 3, 30_000);
  const boxOf = (fn) => until(`the ${fn} box`, () =>
    page.evaluate((f) => window.__lab.flow()?.graph?.nodes.find((n) => n.fn === f)?.id, fn));
  const scaleBox = await boxOf("scale_scores");
  await copied("step (box menu)", async () => {
    await page.locator(`.fl-node[data-id="${scaleBox}"] .fl-nh .fl-more`).click();
    await page.locator(".fl-menu .fl-mi", { hasText: "Copy link to this step" }).click();
  }, { experiment: spring, function: "scale_scores" });
  const meansBox = await boxOf("group_means");
  await page.locator(`.fl-node[data-id="${meansBox}"] .fl-nh .fl-nt`).click();
  await page.locator(".mv-grid.reading .mv-read-info .fl-copylink").waitFor({ timeout: 20_000 });
  await copied("step", () => page.locator(".mv-grid.reading .mv-read-info .fl-copylink").click(), { experiment: spring, function: "group_means" });
  await stop("85-copy-step-inside");
  await page.keyboard.press("Escape");
  await go({ fn: "cronbach_alpha" });
  const ol = page.locator(".ol-item", { hasText: "cronbach_alpha" });
  await ol.waitFor({ timeout: 20_000 });
  await copied("function", async () => {
    await h.answer("menu", "Copy link to its code");
    await ol.locator(".ol-more").click();
  }, { method: "survey-scores", function: "cronbach_alpha" });

  // ------------------------------------------------------------------ opening them
  const away = async () => {
    await go({ view: "home" });
    await page.locator(".exp-card").first().waitFor();
  };
  const openRef = async (ref) => {
    await away();
    await h.params({ ref });
  };
  const inView = (sel) => page.locator(sel).first().evaluate((el) => {
    const r = el.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
  });
  async function landsOnNumber(name, { exp, result, rowText, text }) {
    await until(`${name}: the Results tab at ${result}`, async () => {
      const p = await panel();
      return p.view === "work" && p.exp === exp && p.tab === "results" && p.result[exp] === result;
    }, 30_000);
    const cell = page.locator(".result-shown td.cell-focus");
    await cell.waitFor({ timeout: 20_000 });
    const row = await cell.locator("xpath=..").innerText();
    if (!row.includes(rowText)) throw new Error(`${name}: the highlighted row is ${row}`);
    if (text != null && (await cell.innerText()) !== text) throw new Error(`${name}: the cell says ${await cell.innerText()}`);
    await until(`${name}: the cell in view`, () => inView(".result-shown td.cell-focus"), 5000);
  }

  step("each link opened as layr opens it (the param ref): where does the panel land?");
  await openRef(links.experiment.ref);
  await until("the experiment's workspace", async () => (await panel()).exp === spring && (await panel()).view === "work", 30_000);
  await openRef(links["experiment (card)"].ref);
  await until("autumn's workspace", async () => (await panel()).exp === autumn, 30_000);
  await go({ exp: cmp.experiment });
  await until("on the comparison", async () => (await panel()).method === "compare-experiments", 30_000);
  await h.params({ ref: links.method.ref });
  await until("the method's workspace", async () => (await panel()).method === "survey-scores", 30_000);
  for (const [name, id, sel] of [["table", "scores", ".result-shown .layr-table"], ["text", "model_report", "pre.text-result"],
    ["figure", "chart", "img.fig"]]) {
    await openRef(links[name].ref);
    const exp = links[name].args.experiment;
    await until(`${name}: Results at ${id}`, async () => (await panel()).result?.[exp] === id && (await panel()).tab === "results", 30_000);
    await page.locator(`.result-item.active[data-id="${id}"]`).waitFor({ timeout: 20_000 });
    await page.locator(sel).waitFor({ timeout: 20_000 });
  }
  await openRef(links.value.ref);
  await page.locator('.value-card.cell-focus[data-name="alpha"]').waitFor({ timeout: 30_000 });
  await stop("86-open-value");

  const means = await h.tool("result_table", { experiment: spring, table: "group_means", where: { group: "urban" } });
  const fmt = (v) => (Number.isInteger(v) ? String(v) : String(+v.toPrecision(4)));
  await openRef(links.number.ref);
  await landsOnNumber("number", { exp: spring, result: "group_means", rowText: "urban", text: fmt(means.rows[0].mean_score) });
  await stop("87-open-number");
  await openRef(links["step table number"].ref);
  await landsOnNumber("step table number", { exp: spring, result: "step:group_means", rowText: "rural" });

  await openRef(links.step.ref);
  await until("the step looked inside", async () => {
    const p = await panel();
    return p.exp === spring && p.tab === "flow" && p.code === true && p.codeMode === "read" && (await page.locator(".mv-grid.reading .mv-read-info .fl-copylink").count()) > 0;
  }, 30_000);
  await page.locator(`.fl-node.sel[data-id="${meansBox}"]`).waitFor({ timeout: 20_000 });
  const sideTitle = await page.locator(".mv-grid.reading .mv-read-info .fl-st").innerText();
  if (!/group means/i.test(sideTitle)) throw new Error(`reading shows ${sideTitle}`);
  await stop("88-open-step");
  await openRef(links["step (box menu)"].ref);
  await page.locator(`.fl-node.sel[data-id="${scaleBox}"]`).waitFor({ timeout: 30_000 });

  await openRef(links.function.ref);
  await until("the code unfolded at cronbach_alpha", async () => {
    const st = await page.evaluate(() => window.__lab.state());
    return st?.code === true && st.fn === "cronbach_alpha";
  }, 30_000);
  await page.locator(".ol-item.active", { hasText: "cronbach_alpha" }).waitFor();
  await stop("89-open-function");

  step("a number far down a 290-row table (past the rows shown), with a filter on before: still found");
  const rows = (await h.tool("result_table", { experiment: long.experiment, table: "doubled", limit: 2000 })).rows;
  const far = rows.slice(250).find((r) => typeof r.doubled === "number" && r.group !== rows[0].group);
  const farRef = await h.tool("reference_make", {
    experiment: long.experiment, result: "doubled", row: { experiment: far.experiment, respondent: far.respondent }, column: "doubled",
  });
  await go({ exp: long.experiment, result: "doubled" });
  const filter = page.locator(".result-shown .filters select").filter({ hasText: "All group" });
  await filter.waitFor({ timeout: 30_000 });
  await filter.selectOption(String(rows[0].group));
  await h.params({ ref: farRef.ref });
  await landsOnNumber("far row", { exp: long.experiment, result: "doubled", rowText: String(far.respondent), text: fmt(far.doubled) });
  const filters = await page.locator(".result-shown .filters select").evaluateAll((s) => s.map((x) => x.value));
  if (filters.some(Boolean)) throw new Error(`a filter is still on: ${filters}`);
  await stop("90-open-far-row");
  await page.setViewportSize({ width: 700, height: 800 });
  await h.params({ ref: links.number.ref });
  await landsOnNumber("number at 700 px", { exp: spring, result: "group_means", rowText: "urban" });
  await stop("91-open-number-700");
  await page.setViewportSize({ width: 1360, height: 860 });

  // ------------------------------------------------------------------ broken links
  step("broken links: as far as they go, with one notice each");
  const q = (o) => new URLSearchParams(o).toString();
  async function broken(name, ref, check, notice) {
    const since = await lastSeq();
    await openRef(ref);
    await until(`${name}: where it lands`, check, 30_000);
    const notes = await until(`${name}: its notice`, async () => {
      const n = (await h.log(since, "notify")).filter((x) => x.level === "warning");
      return n.length ? n : null;
    }, 20_000);
    await page.waitForTimeout(400);
    const all = (await h.log(since, "notify")).filter((x) => x.level === "warning");
    if (all.length !== 1 || !notice.test(all[0].text)) throw new Error(`${name}: the notices were ${JSON.stringify(all.map((x) => x.text))}`);
    return notes;
  }
  await broken("a deleted experiment", q({ experiment: "e00000000" }), async () => (await panel()).view === "home", /no longer exists/);
  await broken("a result gone", q({ experiment: spring, result: "no_such" }),
    async () => (await panel()).exp === spring && (await panel()).tab === "results", /no longer returns/);
  await broken("a row gone", q({ experiment: spring, result: "group_means", row: JSON.stringify({ group: "nowhere" }), column: "mean_score" }),
    async () => (await panel()).result?.[spring] === "group_means" && (await page.locator(".result-shown .layr-table").count()) > 0, /No row/);
  if (await page.locator(".result-shown td.cell-focus").count()) throw new Error("a row that isn't there is highlighted");
  await broken("a function gone", q({ method: "survey-scores", function: "no_such_fn" }),
    async () => (await panel()).method === "survey-scores" && (await panel()).view === "work", /no longer has a function/);
  await broken("a malformed link", `experiment=${spring}&row=%7Bnot-json`, async () => (await panel()).view === "home", /can't be opened/);

  // ------------------------------------------------------------------ started by a link
  step("a panel started by a link (layr.info.params.ref) lands at the number");
  await errors.check();
  await page.context().close();
  await h.params({ ref: links.number.ref });
  ({ page, errors } = await open());
  await landsOnNumber("started with the link", { exp: spring, result: "group_means", rowText: "urban" });
  await errors.check();
});

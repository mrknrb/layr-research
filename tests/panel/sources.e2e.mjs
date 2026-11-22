// Experiments that read other experiments (result inputs), in the Experiments panel: the comparison example's picker
// in Settings (a source removed and added again, a loop refused in plain words, then run), a new experiment made in
// the panel with a result input, out of date after a source is calculated again with another setting (the banner
// and the home card), the flow's input box and its side panel, "+ Input" › a result of other experiments, provenance
// ending at the source › "Open it there", and deleting an experiment others read (refused, then forced).
// Screenshots (dark and light) in tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/sources.e2e.mjs
import { bench } from "./lib.mjs";

// a tiny method that reads the scores of other experiments and doubles them (a formula on a column, so a doubled
// number's provenance ends at the source)
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

await bench("sources", async ({ h, open, shot, until, step }) => {
  step("the comparison example (two survey waves and a comparison of their scores), run through the tools");
  const cmp = await h.tool("lab_sample", { which: "compare" });
  const [spring, autumn] = cmp.made;
  const r0 = await h.tool("run", { experiment: cmp.experiment, wait_s: 55 });
  if (r0.status !== "done") throw new Error(`the comparison didn't run: ${r0.status} ${JSON.stringify(r0.error)}`);
  const titles = Object.fromEntries((await h.tool("experiments")).map((e) => [e.experiment, e.title]));

  const { page, errors } = await open();
  async function stop(name) {
    await page.waitForTimeout(300);
    await shot(page, `${name}-dark`);
    await h.theme("light");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await page.waitForTimeout(150);
    await shot(page, `${name}-light`);
    await h.theme("dark");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  }
  const panel = () => page.evaluate(() => window.__lab.panel());
  const sourcesOf = async (id) => ((await h.tool("experiment_get", { experiment: id })).sources?.results || []).map((s) => s.experiment);
  const notices = async (since) => (await h.log(since, "notify")).map((n) => n.text);
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });

  // ------------------------------------------------------------------ home: what reads what
  step("home: each card says quietly what it reads and what reads it");
  const cmpCard = page.locator(".exp-card", { hasText: cmp.title });
  await cmpCard.locator(".exp-links", { hasText: "Reads" }).waitFor();
  const readsText = await cmpCard.locator(".exp-links").first().innerText();
  if (!readsText.includes(titles[spring]) || !readsText.includes(titles[autumn])) throw new Error(`the card says: ${readsText}`);
  await page.locator(".exp-card", { hasText: titles[spring] }).locator(".exp-links", { hasText: "Read by" }).waitFor();
  await stop("71-home-reads");

  // ------------------------------------------------------------------ Settings: remove a source, the picker
  step("Settings: the two sources as chips; one removed with its ×");
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "settings" }), cmp.experiment);
  const row = page.locator(".src-input");
  await until("two chips", async () => (await row.locator(".src-chip").count()) === 2, 30_000);
  await row.locator(".src-chip", { hasText: titles[autumn] }).locator(".src-x").click();
  await until("one chip left", async () => (await page.locator(".src-input .src-chip").count()) === 1, 20_000);
  if ((await sourcesOf(cmp.experiment)).join() !== spring) throw new Error("the removed source is still read");
  await page.locator(".banner.outdated", { hasText: "No longer picked" }).waitFor({ timeout: 20_000 });

  step("a new experiment made in the panel: it reads the comparison's summary");
  await page.evaluate(() => window.__lab.show({ view: "new", newMethod: "compare-experiments" }));
  await page.locator("h2", { hasText: "New experiment" }).waitFor();
  await page.locator(".layr-label", { hasText: "What it reads" }).waitFor();
  await page.locator(".page input.layr-input").first().fill("Comparison of comparisons");
  await page.locator("button", { hasText: "Create" }).click();
  await page.locator(".layr-error", { hasText: "Pick the experiments for" }).waitFor(); // said in the page, plainly
  await page.locator(".src-input button", { hasText: "Pick experiments…" }).click();
  const dlg = page.locator("#dialog");
  await dlg.locator(".src-row").first().waitFor();
  const cmpRow = dlg.locator(`.src-row[data-exp="${cmp.experiment}"]`);
  await cmpRow.locator(".src-table-pick").selectOption("summary");
  if (!(await cmpRow.locator("input[type=checkbox]").isChecked())) throw new Error("picking a table didn't tick its experiment");
  await dlg.locator(".src-summary li", { hasText: "summary" }).waitFor();
  await stop("72-picker-new");
  await dlg.locator("button", { hasText: "Read these" }).click();
  await page.locator(".src-input .src-chip", { hasText: "summary" }).waitFor();
  await stop("73-new-experiment");
  await page.locator("button", { hasText: "Create" }).click();
  await until("the new experiment's settings", async () => (await panel()).tab === "settings" && (await panel()).exp !== cmp.experiment, 20_000);
  const meta = (await panel()).exp;
  await page.locator(".src-input .src-chip", { hasText: cmp.title }).waitFor({ timeout: 20_000 });
  // calculated (it compares the comparison's means), so it has tables the comparison could read
  await h.tool("experiment_update", { experiment: meta, params: { column: "mean", group_column: "group" } });
  const rm = await h.tool("run", { experiment: meta, wait_s: 55 });
  if (rm.status !== "done") throw new Error(`the new experiment didn't run: ${rm.status} ${JSON.stringify(rm.error)}`);

  step("the comparison's picker: a loop is refused in plain words; autumn added back; run");
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "settings" }), cmp.experiment);
  await page.locator(".src-input .src-chip", { hasText: titles[spring] }).waitFor({ timeout: 20_000 });
  await page.locator(".src-input button", { hasText: "Change…" }).click();
  await dlg.locator(`.src-row[data-exp="${meta}"] input[type=checkbox]`).check();
  await dlg.locator("button", { hasText: "Read these" }).click();
  await dlg.locator(".layr-error", { hasText: "loop" }).waitFor({ timeout: 15_000 });
  await stop("74-picker-loop");
  await dlg.locator(`.src-row[data-exp="${meta}"] input[type=checkbox]`).uncheck();
  await dlg.locator(`.src-row[data-exp="${autumn}"] input[type=checkbox]`).check();
  const order = await dlg.locator(".src-summary li").allInnerTexts();
  if (!order[0].includes(titles[spring]) || !order[1].includes(titles[autumn])) throw new Error(`the reading order: ${order}`);
  await dlg.locator("button", { hasText: "Read these" }).click();
  await until("two chips again", async () => (await page.locator(".src-input .src-chip").count()) === 2, 20_000);
  if ((await sourcesOf(cmp.experiment)).join() !== [spring, autumn].join()) throw new Error("the picks weren't kept in order");
  await page.locator(".actions .run-btn").click();
  await until("the comparison calculated", async () => (await h.tool("experiment_get", { experiment: cmp.experiment })).outdated.length === 0 &&
    !(await page.locator(".banner.outdated").count()), 60_000);

  step("at 700 px: the picker stacks");
  await page.setViewportSize({ width: 700, height: 800 });
  await page.locator(".src-input button", { hasText: "Change…" }).click();
  await dlg.locator(".src-row").first().waitFor();
  await stop("75-picker-700");
  await dlg.locator("button", { hasText: "Cancel" }).click();
  await page.setViewportSize({ width: 1360, height: 860 });

  // ------------------------------------------------------------------ out of date
  step("spring calculated again with another setting: the comparison is out of date (banner and home card)");
  await h.tool("experiment_update", { experiment: spring, params: { min_answers: 6 } });
  const rs = await h.tool("run", { experiment: spring, wait_s: 55 });
  if (rs.status !== "done") throw new Error(`spring didn't run: ${rs.status}`);
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "results" }), cmp.experiment);
  const banner = page.locator(".banner.outdated");
  await banner.waitFor({ timeout: 20_000 });
  const bannerText = await banner.innerText();
  if (!bannerText.includes(titles[spring]) || !/newer run .* other numbers in scores/.test(bannerText))
    throw new Error(`the banner says: ${bannerText}`);
  await stop("76-outdated-banner");
  await page.evaluate(() => window.__lab.show({ view: "home" }));
  await cmpCard.locator(".layr-badge", { hasText: "out of date" }).waitFor({ timeout: 20_000 });
  await cmpCard.locator(".exp-why", { hasText: "other numbers" }).waitFor();
  await stop("77-home-outdated");

  // ------------------------------------------------------------------ the flow's input box
  step("the flow: the input box shows what it reads; read under the flow, it lists the sources and the table read");
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "flow", code: false }), cmp.experiment);
  const inBox = page.locator('.fl-node.src.from[data-id="in:results"]');
  await inBox.waitFor({ timeout: 30_000 });
  const inText = await inBox.innerText();
  if (!/2 experiments · scores/.test(inText)) throw new Error(`the input box says: ${inText}`);
  await inBox.locator(".fl-nh").click();
  const side = page.locator(".mv-grid.reading .mv-read-code"); // a box without code: read in the middle
  await side.locator(".fl-srcchip").nth(1).waitFor({ timeout: 20_000 });
  await side.locator(".fl-tv tbody tr").first().waitFor({ timeout: 20_000 });
  await stop("78-flow-input");
  await side.locator("button", { hasText: "Pick experiments…" }).click();
  await dlg.locator(".src-row.on").nth(1).waitFor();
  await dlg.locator("button", { hasText: "Cancel" }).click();
  await side.locator(".fl-srcchip", { hasText: titles[autumn] }).click();
  await until("autumn opened from its chip", async () => (await panel()).exp === autumn, 20_000);

  // ------------------------------------------------------------------ provenance › Open it there
  step("provenance ends at the source; Open it there lands on spring's table at that number, explained");
  const made = await h.tool("method_create", { method: "doubled-scores", name: "Doubled scores", source: READER });
  if (made.saved === false) throw new Error(`the reader method: ${JSON.stringify(made)}`);
  const reader = await h.tool("experiment_create", {
    title: "Spring doubled", method: "doubled-scores", sources: { results: [{ experiment: spring, result: "scores" }] },
  });
  const rr = await h.tool("run", { experiment: reader.experiment, wait_s: 55 });
  if (rr.status !== "done") throw new Error(`the reader didn't run: ${rr.status} ${JSON.stringify(rr.error)}`);
  const doubled = await h.tool("result_table", { experiment: reader.experiment, table: "doubled", limit: 500 });
  const pick = doubled.rows.find((r) => typeof r.score === "number" && r.score % 1 !== 0);
  await page.evaluate((exp) => window.__lab.show({ exp, result: "doubled" }), reader.experiment);
  await page.locator(".result-shown .layr-table").waitFor({ timeout: 30_000 });
  const heads = await page.locator(".result-shown .layr-table thead th").allInnerTexts();
  const ci = (c) => heads.indexOf(c);
  const tr = page.locator(".result-shown .layr-table tbody tr").filter({
    has: page.locator(`td:nth-child(${ci("respondent") + 1})`, { hasText: new RegExp(`^${pick.respondent}$`) }),
  });
  await tr.locator(`td:nth-child(${ci("doubled") + 1})`).click();
  const src = dlg.locator(".prov-source");
  await src.waitFor({ timeout: 20_000 });
  if (!(await src.innerText()).includes(titles[spring])) throw new Error(`the source step: ${await src.innerText()}`);
  await stop("79-provenance-source");
  await src.locator("button", { hasText: "Open it there" }).click();
  await until("spring's Results at scores", async () => {
    const p = await panel();
    return p.exp === spring && p.tab === "results" && p.result[spring] === "scores";
  }, 20_000);
  const focus = page.locator(".result-shown td.cell-focus");
  await focus.waitFor({ timeout: 20_000 });
  const focusRow = await focus.locator("xpath=..").innerText();
  if (!new RegExp(`(^|\\s)${pick.respondent}(\\s|$)`).test(focusRow)) throw new Error(`the highlighted row: ${focusRow}`);
  await dlg.locator("h3", { hasText: "How score =" }).waitFor({ timeout: 20_000 });
  await stop("80-open-it-there");
  await dlg.locator("button", { hasText: "Close" }).click();

  // ------------------------------------------------------------------ + Input › a result of other experiments
  step("the flow's + Input › A result of other experiments adds a [result] input to the code");
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "flow", code: false }), cmp.experiment);
  await page.locator('.fl-node.src.from[data-id="in:results"]').waitFor({ timeout: 30_000 });
  await page.locator(".fl-bar button", { hasText: "+ Input" }).click();
  await page.locator(".fl-menu .fl-mi", { hasText: "A result of other experiments" }).click();
  const form = page.locator(".fl-form", { hasText: "Add a result of other experiments" });
  await form.locator("input").first().fill("Earlier comparisons");
  await form.locator("input").nth(2).fill("summary");
  await stop("81-add-result-input");
  await form.locator("button", { hasText: "Add it" }).click();
  await until("the new input in the code", () =>
    page.evaluate(() => window.__lab.editor().getValue().includes("earlier_comparisons: Earlier comparisons [result: summary]")), 15_000);
  await page.locator('.fl-node.src.from[data-id="in:earlier_comparisons"]').waitFor({ timeout: 20_000 });

  // ------------------------------------------------------------------ deleting a source
  step("deleting autumn, which the comparison reads: refused in plain words, then deleted anyway");
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "settings" }), autumn);
  await page.locator("#dialog h3", { hasText: "Leave without saving?" }).waitFor(); // the unsaved input
  await page.locator("#dialog button", { hasText: "Leave" }).click();
  await page.locator("button", { hasText: "Delete experiment" }).click();
  await dlg.locator("button", { hasText: "Delete" }).click();
  await dlg.locator("h3", { hasText: "Other experiments read it" }).waitFor({ timeout: 15_000 });
  const why = await dlg.locator(".dialog-body").innerText();
  if (!why.includes(cmp.title) || /force|\be[0-9a-f]{8}\b/.test(why)) throw new Error(`the refusal: ${why}`);
  await stop("82-delete-refused");
  await dlg.locator("button", { hasText: "Delete anyway" }).click();
  await until("home, autumn gone", async () => (await panel()).view === "home" &&
    !(await h.tool("experiments")).some((e) => e.experiment === autumn), 20_000);
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "settings" }), cmp.experiment);
  await page.locator(".src-input .src-chip.bad").waitFor({ timeout: 20_000 });
  await page.locator(".banner.outdated", { hasText: "deleted" }).waitFor();
  await stop("83-source-deleted");

  await errors.check();
});

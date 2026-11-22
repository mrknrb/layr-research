// Every round of a loop: the rounds tables under a function's code, in the flow's "look inside" panel and beside the
// code editor. The survey's loops (160 rounds; an `if` whose column has blanks), and a small method made here with a
// loop in a loop and a loop of 10,500 rounds (more than are kept: the cut notice, paging, the panel stays quick).
// A row picked → the code shows that round; a number of rounds of an inner loop → its table for that round; Copy as
// CSV. Screenshots (dark, light, 700 px) in tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/rounds.e2e.mjs
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { bench } from "./lib.mjs";

const LOOPS = `"""Loops for the rounds table

A small method with a loop in a loop, and a long loop.
"""

import pandas as pd
from toolbox import read_table


def grid_cells(table, extra):
    """Each row's cells: a loop in a loop."""
    cells = []
    for row in table.to_dict("records"):
        width = int(row["n"]) + extra
        for col in range(width):
            cell = row["n"] * 10 + col
            cells.append(cell)
        count = len(cells)
    return cells


def long_sum(rounds):
    """A long loop: many rounds, a line that runs in every third one."""
    total = 0.0
    for i in range(rounds):
        total += i * 0.5
        if i % 3 == 0:
            third = i
        label = f"r{i}"
    return total


def ratios(table, stop):
    """Fails in the round where n is stop (a division by zero)."""
    out = []
    for row in table.to_dict("records"):
        ratio = 100 // (stop - row["n"])
        out.append(ratio)
    return out


def loops(table_file, extra=2, rounds=10500, stop=0):
    """Loops.

    Inputs:
        table_file: A small table (CSV) [.csv]

    Settings:
        extra: Extra cells per row.
        rounds: Rounds of the long loop.
        stop: The n where the ratios fail (0: none).

    Returns:
        summary: The counts
    """
    table = read_table(table_file)
    cells = grid_cells(table, extra)
    total = long_sum(rounds)
    parts = ratios(table, stop)
    return {"summary": pd.DataFrame([{"cells": len(cells), "total": total, "ratios": len(parts)}])}
`;

await bench("rounds", async ({ h, open, shot, until, step }) => {
  step("the survey example and a method with loops, made and run through the tools");
  const survey = await h.tool("lab_sample", { which: "survey" });
  const made = await h.tool("method_create", { method: "loops", name: "Loops", source: LOOPS });
  if (!made.saved) throw new Error(`the loops method wasn't saved: ${JSON.stringify(made)}`);
  const csv = join(mkdtempSync(join(tmpdir(), "rounds-")), "rows.csv");
  writeFileSync(csv, "n,name\n1,a\n2,b\n3,c\n");
  const loopsExp = await h.tool("experiment_create", { title: "Loops", method: "loops", files: { table_file: csv } });
  for (const id of [survey.experiment, loopsExp.experiment]) {
    const r = await h.tool("run", { experiment: id, wait_s: 90 });
    if (r.status !== "done") throw new Error(`${id}: ${r.status} ${JSON.stringify(r.error)}`);
  }
  const failing = await h.tool("experiment_create", { title: "Loops that fail", method: "loops", files: { table_file: csv },
    params: { stop: 2, rounds: 10 } });
  const bad = await h.tool("run", { experiment: failing.experiment, wait_s: 90 });
  if (bad.status !== "failed") throw new Error(`the failing loop: ${bad.status}`);

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
  const chips = (where) => page.locator(`${where} .cmx-chip-text`).allInnerTexts();
  const rowsOnPage = (where) => page.locator(`${where} .rt tbody tr[data-k]`).count();
  async function scrollTable(where, rows) {
    await page.locator(`${where} .rt-scroll`).first().evaluate((el, y) => (el.scrollTop = y), rows * 22);
    await page.waitForTimeout(120);
  }
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });

  // ------------------------------------------------------------------ the flow's side panel
  step("the survey's scale scores, looked inside in the flow: its loop of 160 rounds as a table");
  await page.evaluate((exp) => window.__lab.show({ exp, code: false }), survey.experiment);
  await until("the flow", async () => (await page.locator(".fl-node").count()) > 3, 30_000);
  const nodeId = await until("the scale scores box", () =>
    page.evaluate(() => window.__lab.flow()?.graph?.nodes.find((n) => n.fn === "scale_scores")?.id));
  await page.locator(`.fl-node[data-id="${nodeId}"] .fl-nh .fl-nt`).click();
  const side = ".fl-side.open";
  await page.locator(`${side} .rt`).first().waitFor({ timeout: 20_000 });
  await until("its rows", async () => (await rowsOnPage(side)) > 5, 20_000);
  const heads = await page.locator(`${side} .rt thead th`).allInnerTexts();
  for (const want of ["Round", "row", "values", "score"])
    if (!heads.some((t) => t.trim() === want)) throw new Error(`the table's columns: ${heads.join(" | ")}`);
  if (!/160 rounds/.test(await page.locator(`${side} .rt-count`).first().innerText()))
    throw new Error("the loop's title doesn't say 160 rounds");
  await until("the code at round 1", async () => (await chips(side)).includes("round 1 of 160"));
  if ((await rowsOnPage(side)) > 60) throw new Error("more rows on the page than are in view");

  step("a round that keeps its whole values (2), then one the table alone knows (40)");
  await page.locator(`${side} .rt tr[data-k="2"]`).click();
  await until("the code at round 2", async () => (await chips(side)).includes("round 2 of 160"));
  await scrollTable(side, 30);
  await page.locator(`${side} .rt tr[data-k="40"]`).click();
  await until("the code at round 40", async () => (await chips(side)).includes("round 40 of 160"));
  const row40 = await page.locator(`${side} .rt tr[data-k="40"] td`).allInnerTexts();
  const scoreChip = (await chips(side)).find((t) => t.startsWith("score = "));
  const scoreAt = heads.findIndex((t) => t.trim() === "score");
  if (!scoreChip || scoreChip.slice(8) !== row40[scoreAt])
    throw new Error(`the code's score (${scoreChip}) isn't the table's (${row40.join(" | ")})`);
  if ((await page.locator(`${side} .rt tr.on`).getAttribute("data-k")) !== "40") throw new Error("row 40 isn't shaded");
  await stop("51-rounds-flow-side");

  step("Copy as CSV: every round, with the names as the header");
  const since = (await h.log(0)).at(-1)?.seq ?? 0;
  await page.locator(`${side} .rt-copy`).first().click();
  const clip = await until("the clipboard", async () => (await h.log(since, "clipboard"))[0]?.text);
  const lines = clip.split(/\r?\n/);
  if (!lines[0].startsWith("round,row,values,score") || lines.length !== 161) throw new Error(`the CSV: ${lines.slice(0, 3)}`);

  step("the keyboard: down picks the next round");
  await page.locator(`${side} .rt-scroll`).first().focus();
  await page.keyboard.press("ArrowDown");
  await until("the code at round 41", async () => (await chips(side)).includes("round 41 of 160"));

  // ------------------------------------------------------------------ beside the code editor
  step("the code shown: answer_checks' loops beside it, the `if` column with blanks");
  await page.locator(`${side} button[aria-label="Close"]`).click();
  await page.evaluate(() => window.__lab.show({ fn: "answer_checks" }));
  const insp = ".mv-inspector";
  await until("its two loops", async () => (await page.locator(`${insp} .rt`).count()) === 2, 30_000);
  const second = page.locator(`${insp} .rt`).nth(1);
  await until("the second loop's rows", async () => (await second.locator("tbody tr[data-k]").count()) > 5, 20_000);
  const heads2 = await second.locator("thead th").allInnerTexts();
  if (!heads2.some((t) => t.trim() === "message")) throw new Error(`the second loop's columns: ${heads2.join(" | ")}`);
  if (!(await second.locator("td.rt-none").count())) throw new Error("no blank cell where the `if` didn't run");
  // a round where the `if` did run: its message shows in the code
  const loopLine = await page.evaluate(() => [...window.__lab.rounds().el.querySelectorAll(".rt-code")][1].textContent);
  const inside = await h.tool("function_inside", { experiment: survey.experiment, function: "answer_checks" });
  const head2 = inside.statements.filter((s) => s.loop)[1];
  const all = await h.tool("loop_rounds", { experiment: survey.experiment, function: "answer_checks", line: head2.line });
  const mcol = all.columns.findIndex((c) => c.name === "message") + 1;
  const hit = all.rows.find((r) => r[mcol] != null);
  if (!hit) throw new Error(`no round of "${loopLine}" gave a message`);
  await second.locator(".rt-scroll").evaluate((el, y) => (el.scrollTop = y), Math.max(0, hit[0] - 3) * 22);
  await second.locator(`tr[data-k="${hit[0]}"]`).click();
  await until("the message in the code", async () =>
    (await chips(".mv-editor")).some((t) => t.startsWith("message = ") && t.includes(String(hit[mcol]).slice(0, 12))));
  await stop("52-rounds-beside-code");

  // ------------------------------------------------------------------ a loop in a loop
  step("a loop in a loop: the outer table's number of rounds opens the inner one for that round");
  await page.evaluate((exp) => window.__lab.show({ exp, fn: "grid_cells" }), loopsExp.experiment);
  await until("the outer loop's rows", async () => (await page.locator(`${insp} .rt:not(.inner) tbody tr[data-k]`).count()) === 3,
    30_000);
  const innerSec = page.locator(`${insp} .rt.inner`);
  if (await innerSec.locator(".rt-body").isVisible()) throw new Error("the inner loop's table should start folded");
  await page.locator(`${insp} .rt:not(.inner) tr[data-k="2"] .rt-inner`).click(); // row n=2: 4 rounds
  await until("the inner table for round 2", async () => /round 2 of/.test(await innerSec.locator(".rt-filter").innerText()));
  if ((await innerSec.locator("tbody tr[data-k]").count()) !== 4) throw new Error("not the 4 rounds of round 2");
  await innerSec.locator("tbody tr[data-k]").nth(2).click();
  await until("the code at the inner round", async () => {
    const c = await chips(".mv-editor");
    return c.includes("round 3 of 4") && c.includes("cell = 22") && c.includes("round 2 of 3");
  });
  await stop("53-rounds-inner");
  await innerSec.locator(".rt-filter a", { hasText: "Show every round" }).click();
  await until("every inner round", async () => (await innerSec.locator("tbody tr[data-k]").count()) === 3 + 4 + 5);

  // ------------------------------------------------------------------ a round that failed
  step("a loop that stopped with an error: its round is marked and shown at first");
  await page.evaluate((exp) => window.__lab.show({ exp, fn: "ratios" }), failing.experiment);
  const ratiosTable = page.locator(`${insp} .rt`).first();
  await until("the failed round", async () => (await ratiosTable.locator('tr.failed[data-k="2"]').count()) === 1, 30_000);
  const said = await ratiosTable.locator(".rt-note.bad").innerText();
  if (!/stopped in round 2/.test(said) || !/division|modulo/.test(said)) throw new Error(`the failed round's note: ${said}`);
  await until("the code at the failed round", async () => {
    const c = await chips(".mv-editor");
    return c.includes("round 2 of 2") && c.includes("stopped here");
  });
  if ((await ratiosTable.locator("tr.on").getAttribute("data-k")) !== "2") throw new Error("the failed round isn't the one shown");
  await stop("57-rounds-failed");

  // ------------------------------------------------------------------ 10,500 rounds
  step("a loop of 10,500 rounds: the cut notice, pages of rows, the panel stays quick");
  await page.evaluate((exp) => window.__lab.show({ exp, fn: "long_sum" }), loopsExp.experiment);
  const big = page.locator(`${insp} .rt`).first();
  await until("its rows", async () => (await big.locator("tbody tr[data-k]").count()) > 5, 30_000);
  const note = await big.locator(".rt-note.warn").innerText();
  if (!/first 10000 rounds/.test(note)) throw new Error(`the cut notice: ${note}`);
  if (!/10,500 rounds/.test(await big.locator(".rt-count").innerText())) throw new Error("the title doesn't say 10,500");
  const t0 = Date.now();
  await big.locator(".rt-scroll").evaluate((el) => (el.scrollTop = el.scrollHeight));
  await until("the last kept rounds", async () => (await big.locator('tr[data-k="10000"]').count()) === 1, 20_000);
  const ms = Date.now() - t0;
  if ((await big.locator("tbody tr").count()) > 90) throw new Error("too many rows on the page");
  await big.locator('tr[data-k="9997"]').click(); // i = 9996: the `if` ran (every third round)
  await until("the code at round 9,997", async () => {
    const c = await chips(".mv-editor");
    return c.includes("round 9,997 of 10,500") && c.some((t) => t.startsWith("total = ")) && c.includes("third = 9996");
  });
  const lag = await page.evaluate(() => new Promise((r) => {
    const t = performance.now();
    requestAnimationFrame(() => r(performance.now() - t));
  }));
  step(`the last page came in ${ms} ms; a frame after it took ${lag.toFixed(0)} ms`);
  if (lag > 500) throw new Error(`the panel hangs (${lag} ms for a frame)`);
  await stop("54-rounds-10500");

  step("the same in the flow's side panel, folded, and at 700 px");
  await page.evaluate(() => window.__lab.show({ code: false }));
  await until("the code folded", async () => (await page.locator(".mv-split.folded").count()) === 1);
  const sumId = await until("the long sum box", () =>
    page.evaluate(() => window.__lab.flow()?.graph?.nodes.find((n) => n.fn === "long_sum")?.id));
  await page.locator(`.fl-node[data-id="${sumId}"] .fl-nh .fl-nt`).click();
  await until("its rows", async () => (await rowsOnPage(side)) > 5, 20_000);
  await page.setViewportSize({ width: 700, height: 800 });
  await page.waitForTimeout(400);
  await stop("55-rounds-flow-700");
  await page.locator(`${side} button[aria-label="Close"]`).click();
  await page.evaluate((exp) => window.__lab.show({ exp, fn: "scale_scores" }), survey.experiment);
  await until("the table beside the code", async () => (await page.locator(`${insp} .rt tbody tr[data-k]`).count()) > 5, 30_000);
  await until("the table in view", async () => {
    await page.locator(`${insp} .rt`).first().scrollIntoViewIfNeeded({ timeout: 2000 });
    return true;
  });
  await stop("56-rounds-code-700");

  await errors.check();
});

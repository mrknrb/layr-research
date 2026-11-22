// Every resizable place of the Experiments panel (panels/resize.js): a box read under the flow (the right side, a
// whole table, the rounds table), the outline and the explanation beside the code, the flow / code divider,
// the Results list, a review card's list. Each is dragged, remembered after a reload, put back by a double click,
// and sane at 700 px. Screenshots (dark and light) in tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/resize.e2e.mjs
import { bench } from "./lib.mjs";

await bench("resize", async ({ h, open, shot, until, step }) => {
  step("the survey and the GC examples, made and run through the tools");
  const survey = await h.tool("lab_sample", { which: "survey" });
  const gc = await h.tool("lab_sample", { which: "gc" });
  for (const e of [survey, gc]) {
    const r = await h.tool("run", { experiment: e.experiment, wait_s: 55 });
    if (r.status !== "done") throw new Error(`${e.title}: ${r.status} ${JSON.stringify(r.error)}`);
  }
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
  const sizes = async () => (await page.evaluate(() => window.__lab.panel())).sizes || {};
  const width = (sel) => page.locator(sel).first().evaluate((el) => el.getBoundingClientRect().width);
  const height = (sel) => page.locator(sel).first().evaluate((el) => el.getBoundingClientRect().height);
  const near = (a, b, what) => {
    if (Math.abs(a - b) > 3) throw new Error(`${what}: ${a} where ${b} was expected`);
  };
  /** The strip sits on the edge of the box it resizes. */
  async function onEdge(handleSel, boxSel, edge) {
    const hb = await page.locator(handleSel).first().boundingBox();
    const bb = await page.locator(boxSel).first().boundingBox();
    const x = hb.x + hb.width / 2;
    const want = edge === "left" ? bb.x : bb.x + bb.width;
    if (Math.abs(x - want) > 6 || hb.y < bb.y - 6 || hb.y + hb.height > bb.y + bb.height + 6)
      throw new Error(`${handleSel} isn't on the ${edge} edge of ${boxSel}: ${JSON.stringify(hb)} ${JSON.stringify(bb)}`);
  }
  /** Drags the handle by (dx, dy) with the mouse, in small steps as a hand does. */
  async function drag(sel, dx, dy) {
    const handle = page.locator(sel).first();
    await handle.scrollIntoViewIfNeeded();
    const b = await handle.boundingBox();
    if (!b) throw new Error(`no handle ${sel}`);
    const x = b.x + b.width / 2;
    const y = b.y + b.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + dx / 2, y + dy / 2, { steps: 5 });
    await page.mouse.move(x + dx, y + dy, { steps: 5 });
    await page.mouse.up();
    await page.waitForTimeout(80);
  }
  async function lookInside(fn) {
    const id = await until(`the ${fn} box`, () =>
      page.evaluate((f) => window.__lab.flow()?.graph?.nodes.find((n) => n.fn === f)?.id, fn));
    await page.locator(`.fl-node[data-id="${id}"] .fl-nh .fl-nt`).click();
    await page.locator(".mv-grid.reading .mv-read-code .fl-editor").waitFor({ timeout: 20_000 });
    return id;
  }
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });

  // ------------------------------------------------------------------ a box read under the flow
  step("a box read under the flow: the right side wider by its left edge, the rounds table, a whole table");
  await page.evaluate((exp) => window.__lab.show({ exp, code: false }), survey.experiment);
  await until("the flow", async () => (await page.locator(".fl-node").count()) > 3, 30_000);
  await until("the code folded", async () => (await page.locator(".mv-split.folded").count()) === 1);
  const nodeId = await lookInside("scale_scores");
  const info = ".mv-grid.reading .mv-read-info";
  const code = ".mv-grid.reading .mv-read-code";
  await onEdge(".mv-col > .rz-left", ".mv-col:last-child", "left");
  const w0 = await width(info);
  await drag(".mv-col > .rz-left", -100, 0);
  near(await width(info), w0 + 100, "the right side after the drag");
  await page.dblclick(".mv-col > .rz-left");
  near(await width(info), w0, "the right side after a double click");
  await page.locator(`${info} .rt tbody tr[data-k]`).first().waitFor({ timeout: 20_000 });
  const r0 = await height(`${info} .rt-scroll`);
  await drag(`${info} .rt-scroll + .rz-bar`, 0, -80);
  near(await height(`${info} .rt-scroll`), r0 - 80, "the rounds table's height");
  near((await sizes())["flow.rounds"], r0 - 80, "the rounds table's remembered height");
  await page.hover(`${info} .rt-scroll + .rz-bar`);
  await stop("62-resize-reading");
  await page.locator(`.fl-node[data-id="${nodeId}"] .fl-out[data-out="scores"] .fl-on`).click();
  await page.locator(`${code} .fl-scroll.big`).waitFor({ timeout: 20_000 });
  await drag(`${code} .fl-scroll.big + .rz-bar`, 0, -120);
  const t1 = await height(`${code} .fl-scroll.big`);
  near((await sizes())["flow.table"], t1, "the whole table's remembered height");

  // ------------------------------------------------------------------ beside the code
  step("the code shown: the outline, the explanation and the flow / code divider");
  await page.keyboard.press("Escape");
  await page.evaluate(() => window.__lab.show({ fn: "scale_scores" }));
  await until("the code shown", async () => (await page.locator(".mv-split:not(.folded) .mv-grid").count()) === 1);
  await page.locator(".mv-inspector .rt tbody tr[data-k]").first().waitFor({ timeout: 20_000 });
  await onEdge(".mv-col > .rz-right", ".mv-col", "right");
  await onEdge(".mv-col > .rz-left", ".mv-col:last-child", "left");
  const o0 = await width(".mv-outline");
  await drag(".mv-col > .rz-right", 70, 0);
  near(await width(".mv-outline"), o0 + 70, "the outline's width");
  const i0 = await width(".mv-inspector");
  await drag(".mv-col > .rz-left", -90, 0);
  near(await width(".mv-inspector"), i0 + 90, "the explanation's width");
  const f0 = await height(".mv-flow");
  await drag(".code-bar .code-grip", 0, 90);
  near(await height(".mv-flow"), f0 + 90, "the flow's height");
  const frac = (await page.evaluate(() => window.__lab.panel())).flowFrac;
  if (!(frac > 0.42)) throw new Error(`the flow's share wasn't remembered: ${frac}`);
  const ri0 = await height(".mv-inspector .rt-scroll");
  await drag(".mv-inspector .rt-scroll + .rz-bar", 0, 60);
  near(await height(".mv-inspector .rt-scroll"), ri0 + 60, "the rounds table's height beside the code");
  const s1 = await sizes();
  for (const k of ["work.outline", "work.inspector", "work.rounds"])
    if (!(s1[k] > 0)) throw new Error(`${k} wasn't remembered: ${JSON.stringify(s1)}`);
  await page.hover(".mv-col > .rz-left");
  await stop("63-resize-code");

  // ------------------------------------------------------------------ Results and a review card
  step("the Results list");
  await page.evaluate(() => window.__lab.show({ tab: "results" }));
  await page.locator(".result-item.active").waitFor();
  await onEdge(".result-side > .rz-right", ".result-side", "right");
  const l0 = await width(".result-side");
  await drag(".result-side > .rz-right", 90, 0);
  near(await width(".result-side"), l0 + 90, "the Results list's width");
  near((await sizes())["results.list"], l0 + 90, "its remembered width");
  await stop("64-resize-results");

  step("a review card's list (the GC example's Peaks page)");
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "page:0" }), gc.experiment);
  await page.locator(".review-grid .inj-col").waitFor({ timeout: 30_000 });
  await page.locator(".review-grid svg.chrom").waitFor({ timeout: 30_000 });
  await onEdge(".inj-col > .rz-right", ".inj-col", "right");
  const v0 = await width(".inj-col");
  await drag(".inj-col > .rz-right", 120, 0);
  await until("the list wider", async () => Math.abs((await width(".inj-col")) - (v0 + 120)) <= 3);
  near((await sizes())["review.list"], v0 + 120, "its remembered width");
  await page.locator(".review-grid svg.chrom").waitFor({ timeout: 30_000 });
  await stop("65-resize-review");

  // ------------------------------------------------------------------ remembered after a reload
  step("a reload: every size as she left it");
  const before = await sizes();
  await page.reload();
  await page.waitForFunction(() => window.layr && window.layr.connected, null, { timeout: 30_000 });
  await page.locator(".review-grid .inj-col").waitFor({ timeout: 60_000 });
  await until("the review list's width again", async () => Math.abs((await width(".inj-col")) - before["review.list"]) <= 3, 20_000);
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "results" }), survey.experiment);
  await page.locator(".result-item.active").waitFor();
  near(await width(".result-side"), before["results.list"], "the Results list after a reload");
  await page.evaluate(() => window.__lab.show({ tab: "flow", fn: "scale_scores" }));
  await until("the code shown", async () => (await page.locator(".mv-split:not(.folded) .mv-grid").count()) === 1);
  await page.locator(".mv-inspector .rt tbody tr[data-k]").first().waitFor({ timeout: 20_000 });
  near(await width(".mv-outline"), before["work.outline"], "the outline after a reload");
  near(await width(".mv-inspector"), before["work.inspector"], "the explanation after a reload");
  near(await height(".mv-inspector .rt-scroll"), before["work.rounds"], "the rounds table after a reload");
  await page.evaluate(() => window.__lab.show({ code: false }));
  await until("the code folded", async () => (await page.locator(".mv-split.folded").count()) === 1);
  await lookInside("scale_scores");
  await page.locator(`${info} .rt tbody tr[data-k]`).first().waitFor({ timeout: 20_000 });
  near(await height(`${info} .rt-scroll`), before["flow.rounds"], "the rounds table read after a reload");

  // ------------------------------------------------------------------ 700 px
  step("700 px: remembered sizes that don't fit are left aside, nothing is cut off");
  await page.setViewportSize({ width: 700, height: 800 });
  await page.waitForTimeout(400);
  const cw = await width(code);
  if (cw > 700) throw new Error(`the code read is ${cw} px wide at 700 px`);
  await stop("66-resize-700-reading");
  await page.keyboard.press("Escape");
  await page.evaluate(() => window.__lab.show({ fn: "scale_scores" }));
  await until("the code shown", async () => (await page.locator(".mv-split:not(.folded) .mv-grid").count()) === 1);
  for (const sel of [".mv-col > .rz-right", ".mv-col > .rz-left"])
    if (await page.locator(sel).isVisible()) throw new Error(`${sel} can be dragged where the boxes are stacked`);
  const ow = await width(".mv-outline");
  if (ow < 600) throw new Error(`the stacked outline is ${ow} px wide`);
  await stop("67-resize-700-code");
  await page.evaluate(() => window.__lab.show({ tab: "results" }));
  await page.locator(".result-item.active").waitFor();
  if (await page.locator(".result-side > .rz-right").isVisible()) throw new Error("the stacked Results list has a handle");
  await stop("68-resize-700-results");
  step("back to a wide panel: the sizes she gave come back");
  await page.setViewportSize({ width: 1360, height: 860 });
  await until("the Results list's width again", async () => Math.abs((await width(".result-side")) - before["results.list"]) <= 3);

  await errors.check();
});

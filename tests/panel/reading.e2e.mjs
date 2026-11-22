// Reading a box under the flow (no side panel): a click on a step's box unfolds the code in reading mode, the outline
// on the left, the step's code with this run's values in the middle and the rest (what went in, what came out…) on
// the right; ✎ Edit the code switches to the editor and ✓ Back to reading back; a step picked in the outline is read.
// Screenshots (dark) in tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/reading.e2e.mjs
import { bench } from "./lib.mjs";

await bench("reading", async ({ h, open, shot, until, step }) => {
  step("the survey example, made and run through the tools");
  const survey = await h.tool("lab_sample", { which: "survey" });
  const r = await h.tool("run", { experiment: survey.experiment, wait_s: 55 });
  if (r.status !== "done") throw new Error(`${r.status} ${JSON.stringify(r.error)}`);

  const { page } = await open();
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });
  await page.evaluate((exp) => window.__lab.show({ exp, tab: "flow", code: false }), survey.experiment);
  await until("the flow", async () => (await page.locator(".fl-node").count()) > 3, 30_000);
  if (await page.locator(".mv-grid").isVisible()) throw new Error("the code should start folded");

  step("a click on a step's box: read under the flow, no side panel");
  const g = await page.evaluate(() => window.__lab.flow().graph);
  const box = g.nodes.find((n) => n.kind === "call" && n.fn && n.fn_kind !== "toolbox" && n.fn_kind !== "builtin");
  if (!box) throw new Error("no step of its own in the survey's flow");
  await page.locator(`.fl-node[data-id="${box.id}"] .fl-nh .fl-nt`).click();
  await until("the code unfolded", async () => page.locator(".mv-grid.reading").isVisible(), 10_000);
  if (await page.locator(".fl-side.open").count()) throw new Error("the side panel opened");
  await page.locator(".mv-read-code .fl-editor .cm-content").waitFor({ timeout: 20_000 });
  await until("its code in the middle", async () =>
    /def |\w/.test(await page.locator(".mv-read-code .cm-content").textContent()), 10_000);
  await until("what went in on the right", async () => /What went in|What came out/i.test(
    await page.locator(".mv-read-info").innerText()), 20_000);
  if (!(await page.locator(".mv-outline .ol-item").first().isVisible())) throw new Error("no outline on the left");
  if (await page.locator(".mv-editor").isVisible()) throw new Error("the editor shows while reading");
  const st = await page.evaluate(() => window.__lab.state());
  if (st.codeMode !== "read") throw new Error(`mode ${st.codeMode}`);
  await page.waitForTimeout(400);
  await shot(page, "61-reading");

  step("✎ Edit the code: the editor; ✓ Back to reading: the box again");
  await page.locator(".code-bar button", { hasText: "Edit the code" }).click();
  await until("editing", async () => (await page.locator(".mv-editor").isVisible()) &&
    !(await page.locator(".mv-read-code").isVisible()), 10_000);
  if (!(await page.locator(".mv-inspector").isVisible())) throw new Error("no explanation while editing");
  await shot(page, "62-editing");
  await page.locator(".code-bar button", { hasText: "Back to reading" }).click();
  await until("reading again", async () => page.locator(".mv-read-code .fl-editor").isVisible(), 20_000);

  step("a step picked in the outline is read");
  const other = await page.locator(".mv-outline .ol-item:not(.toolbox):not(.active) .ol-name").first().innerText();
  await page.locator(".mv-outline .ol-item:not(.toolbox):not(.active)").first().click();
  await until(`${other} read`, async () => (await page.locator(".mv-read-code .cm-content").textContent()).includes(other) ||
    (await page.locator(".mv-read-info").innerText()).includes(other), 20_000);
  await page.waitForTimeout(300);
  await shot(page, "63-outline-read");

  step("Edit this step (the button on the right) opens the editor at it");
  const edit = page.locator(".mv-read-info button", { hasText: "Edit this step" });
  if (await edit.count()) {
    await edit.click();
    await until("editing", async () => page.locator(".mv-editor").isVisible(), 10_000);
  }

  step("folded again, then a box: reading");
  await page.locator(".code-bar button", { hasText: "Hide the code" }).click();
  await until("folded", async () => !(await page.locator(".mv-grid").isVisible()), 10_000);
  await page.locator(`.fl-node[data-id="${box.id}"] .fl-nh .fl-nt`).click();
  await until("reading the box", async () => page.locator(".mv-grid.reading .mv-read-code .fl-editor").isVisible(),
    20_000);
});

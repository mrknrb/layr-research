// The workspace's code tools: More ▾ (Versions, the reference experiment, Format, Duplicate the method), a change an
// AI agent proposed (its banner, its review in place of the flow and the code, back, accept), and the flow's
// "+ Input", which offers the readers the backend lists. Screenshots (dark and light) in tests/panel/out/ (or
// $SHOTS). No page errors allowed.
//   node tests/panel/code.e2e.mjs
import { bench } from "./lib.mjs";

await bench("code", async ({ h, open, shot, until, step }) => {
  step("the survey example, made and run through the tools");
  const survey = await h.tool("lab_sample", { which: "survey" });
  const r = await h.tool("run", { experiment: survey.experiment, wait_s: 55 });
  if (r.status !== "done") throw new Error(`${r.status} ${JSON.stringify(r.error)}`);

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
  const state = () => page.evaluate(() => window.__lab.state());
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });
  await page.evaluate((exp) => window.__lab.show({ exp, code: true }), survey.experiment);
  await until("the code", async () => /import /.test(await page.locator(".cm-content").first().textContent()), 30_000);

  step("an AI agent proposes a change: a banner on the Flow & code tab, a quiet line on the others");
  const m = await h.tool("method_get", { method: "survey-scores" });
  const changed = m.source.replace('"""The standard error of a mean."""', '"""The standard error of a mean (sd over the root of n)."""');
  if (changed === m.source) throw new Error("the line to change wasn't found");
  const w = await h.tool("method_write", { method: "survey-scores", source: changed, change: "A clearer docstring" },
    { caller: { kind: "agent", label: "Claude" } });
  if (!w.draft) throw new Error(`an agent's change wasn't a draft: ${JSON.stringify(w)}`);
  const banner = page.locator(".mv-banners .banner.draft", { hasText: "Claude proposed a change" });
  await banner.waitFor({ timeout: 20_000 });
  await page.locator(".tabs .tab-results").click();
  await page.locator(".banner.quiet", { hasText: "Claude proposed a change" }).waitFor();
  await page.locator(".tabs .tab-flow").click();
  await stop("41-draft-banner");

  step("its review takes the place of the flow and the code; back, then accept it");
  await banner.locator("button", { hasText: "Review the changes" }).click();
  await page.locator(".code-review .code-review-merge").waitFor();
  await stop("42-draft-review");
  await page.locator(".code-review button", { hasText: "Back to the code" }).click();
  await page.locator(".mv-split .mv-flow").waitFor();
  if (!(await page.locator(".mv-grid").isVisible())) throw new Error("the code didn't come back after the review");
  await page.locator(".mv-banners button", { hasText: "Accept all" }).click();
  await until("version 2", async () => (await state())?.version === 2, 20_000);
  await until("the accepted line in the code", async () =>
    page.evaluate(() => window.__lab.editor().getValue().includes("(sd over the root of n)")), 10_000);
  await until("the banner gone", async () => (await banner.count()) === 0);
  await until("the experiment run with version 2", async () =>
    (await h.tool("run_get", { experiment: survey.experiment })).method?.version === 2, 30_000);

  step("More ▾ › Versions…");
  await h.answer("menu", "Versions…");
  await page.locator(".actions button", { hasText: "More" }).click();
  await page.locator("#dialog h3", { hasText: "Versions" }).waitFor();
  await page.locator("#dialog li", { hasText: "Version 2" }).locator("button", { hasText: "Changed lines" }).click();
  await page.locator("#dialog .versions-merge").waitFor();
  await stop("43-versions");
  await page.locator("#dialog .dialog-buttons button", { hasText: "Close" }).click();

  step("More ▾ › the reference experiment, off and on again");
  await h.answer("menu", "Reference experiment");
  await page.locator(".actions button", { hasText: "More" }).click();
  await until("no reference", async () => !(await h.tool("method_get", { method: "survey-scores" })).reference);
  await h.answer("menu", "Reference experiment");
  await page.locator(".actions button", { hasText: "More" }).click();
  await until("the reference again", async () => (await h.tool("method_get", { method: "survey-scores" })).reference === survey.experiment);

  step("More ▾ › Format the code");
  await h.answer("menu", "Format the code");
  await page.locator(".actions button", { hasText: "More" }).click();
  await until("Format's answer", async () => (await h.log(0, "notify")).some((n) => /tidy|Tidied/.test(n.text)), 15_000);

  step("the flow's + Input offers the backend's readers and a result of other experiments");
  await page.locator(".fl-bar button", { hasText: "+ Input" }).click();
  const readers = (await h.tool("blocks")).readers.map((x) => x.label);
  const offered = await page.locator(".fl-menu .fl-mi > span").allInnerTexts();
  if (offered.join("|") !== [...readers, "A result of other experiments"].join("|"))
    throw new Error(`+ Input offers: ${offered.join(" | ")}`);
  await page.locator(".fl-menu .fl-mi").first().click();
  const form = page.locator(".fl-form", { hasText: "Add a raw file" });
  await form.waitFor();
  const kinds = await form.locator("select option").allInnerTexts();
  if (kinds.join("|") !== readers.join("|")) throw new Error(`the kinds of file: ${kinds.join(" | ")}`);
  await form.locator("input.layr-input").first().fill("plate reads");
  await stop("44-add-raw-file");
  await form.locator("button", { hasText: "Add it" }).click();
  await until("the new input in the code", async () =>
    page.evaluate(() => window.__lab.editor().getValue().includes("plate_reads_file")), 15_000);
  if (!(await state()).dirty) throw new Error("a change in the flow didn't change the code");
  await page.waitForTimeout(1500);
  await stop("45-raw-file-added");

  step("More ▾ › Duplicate the method (the change isn't saved: leaving asks)");
  await h.answer("menu", "Duplicate the method");
  await page.locator(".actions button", { hasText: "More" }).click();
  await page.locator("#dialog h3", { hasText: "Duplicate the method" }).waitFor();
  await page.locator("#dialog button", { hasText: "Duplicate" }).click();
  await page.locator("#dialog h3", { hasText: "Leave without saving?" }).waitFor();
  await page.locator("#dialog button", { hasText: "Leave" }).click();
  await until("the copy, without data", async () => {
    const p = await page.evaluate(() => window.__lab.panel());
    return p.method === "survey-scores-copy" && p.exp === null && p.code === true;
  }, 20_000);
  await until("the copy's flow", async () => (await page.locator(".fl-node").count()) > 3, 30_000);
  await stop("46-duplicate");

  await errors.check();
});

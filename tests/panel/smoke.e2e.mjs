// Smoke test of the Experiments panel (panels/lab.html) as it is today: home, the survey example made and run
// through the tools, its card on the home screen, its Flow and Results tabs, its method's code, back home. A
// screenshot (dark and light) at each stop, in tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/smoke.e2e.mjs
import { bench } from "./lib.mjs";

await bench("smoke", async ({ h, open, shot, until, step }) => {
  const { page, errors } = await open();

  // a screenshot in the dark theme, then the same screen in the light one (the SDK's theme event), back to dark
  async function stop(name) {
    await page.waitForTimeout(250); // let the last redraw settle
    await shot(page, `${name}-dark`);
    await h.theme("light");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await page.waitForTimeout(150);
    await shot(page, `${name}-light`);
    await h.theme("dark");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  }

  step("home screen");
  await page.locator("h2", { hasText: "Experiments" }).waitFor({ timeout: 180_000 }); // the panel polls `methods`
  await page.locator("h2", { hasText: "Methods" }).waitFor();
  await stop("01-home");

  step("the survey example, made and run through the tools");
  const exp = await h.tool("lab_sample", { which: "survey" });
  const run = await h.tool("run", { experiment: exp.experiment });
  if (run.status !== "done")
    throw new Error(`the example's run ${run.status}: ${JSON.stringify(run.error ?? run)}\n${await h.backendLog(2000)}`);

  step("its card on the home screen (redrawn on the research:lab event)");
  const card = page.locator(".exp-card", { hasText: exp.title });
  await card.waitFor();
  await until("the card to say calculated", async () => (await card.textContent()).includes("calculated"));
  await stop("02-home-with-example");

  step("the Flow tab draws boxes");
  await card.click();
  await until("flow boxes", async () => (await page.locator(".fl-node").count()) > 0, 30_000);
  await stop("03-flow");

  step("the Results tab shows a table");
  await page.locator(".tabs .tab-results").click();
  await page.locator(".layr-table tbody tr").first().waitFor();
  await stop("04-results");

  step("its method's code in the editor");
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  await page.locator("li.click", { hasText: exp.method_name }).first().click();
  // CodeMirror draws only the lines in view: the top of the file (its docstring and imports) is enough
  await until("the code", async () => /import /.test(await page.locator(".cm-content").first().textContent()), 30_000);
  await stop("05-method-code");

  step("back home");
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  await card.waitFor();
  await stop("06-home-again");

  const titles = (await h.log(0, "title")).map((e) => e.title);
  if (!titles.includes(exp.title)) throw new Error(`the panel's title never became the experiment's: ${titles}`);
  await errors.check();
});

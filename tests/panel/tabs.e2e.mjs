// The workspace's other tabs, reached through the panel's one navigation function (window.__lab.show, which links
// call too) and through a link's params: a page of the experiment, Results with every kind of entry picked once
// (numbers, a text, a table and how one of its numbers was calculated, a step's table, a figure), Checks,
// Corrections, Settings with a `rows` setting of the GC example changed and run; then New experiment from a file
// (the open dialog), the panel opened with a file, and the narrow panel (700 px). Screenshots (dark and light) in
// tests/panel/out/ (or $SHOTS). No page errors allowed.
//   node tests/panel/tabs.e2e.mjs
import { bench } from "./lib.mjs";

await bench("tabs", async ({ h, open, shot, until, step }) => {
  step("the survey and the GC examples, made and run through the tools");
  const survey = await h.tool("lab_sample", { which: "survey" });
  const gc = await h.tool("lab_sample", { which: "gc" });
  for (const e of [survey, gc]) {
    const r = await h.tool("run", { experiment: e.experiment, wait_s: 55 });
    if (r.status !== "done") throw new Error(`${e.title}: ${r.status} ${JSON.stringify(r.error)}\n${await h.backendLog(2000)}`);
  }

  const { page, errors } = await open();
  async function stop(name, p = page) {
    await p.waitForTimeout(300);
    await shot(p, `${name}-dark`);
    await h.theme("light");
    await p.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await p.waitForTimeout(150);
    await shot(p, `${name}-light`);
    await h.theme("dark");
    await p.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  }
  const showAt = (target, p = page) => p.evaluate((t) => window.__lab.show(t), target);
  const panel = (p = page) => p.evaluate(() => window.__lab.panel());
  await page.locator(".exp-card").first().waitFor({ timeout: 180_000 });

  step("a page of the experiment (show: experiment and tab)");
  await showAt({ exp: survey.experiment, tab: "page:0" });
  await page.locator(".tabs .tab-page-0.active").waitFor();
  await page.locator(".page-card").first().waitFor();
  await until("its cards drawn", async () => (await page.locator(".page-card .layr-spinner").count()) === 0, 20_000);
  if ((await panel()).code) throw new Error("an experiment opened with the code shown");
  await stop("21-page");

  step("Results: Numbers first");
  await page.locator(".tabs .tab-results").click();
  await page.locator('.result-item.active[data-id=":numbers"]').waitFor();
  await page.locator(".big-number").first().waitFor();
  if (await page.locator(".result-shown img").count()) throw new Error("a figure is shown without being picked");
  await stop("22-results-numbers");

  step("Results: a text");
  await page.locator(".result-item.kind-text").first().click();
  await until("the text", async () => ((await page.locator(".result-shown .text-result").textContent()) || "").length > 200);
  await stop("23-results-text");

  step("Results: a table, and how one of its numbers was calculated");
  await page.locator(".result-item.kind-table", { hasText: "Scores per respondent" }).click();
  await page.locator(".result-shown .layr-table tbody tr").first().waitFor();
  await page.locator(".result-shown td.num.click").nth(1).click();
  await page.locator("#dialog h3", { hasText: "was calculated" }).waitFor();
  await stop("24-results-provenance");
  await page.locator("#dialog button", { hasText: "Close" }).click();

  step("Results: a step's table (show: result)");
  await showAt({ exp: survey.experiment, result: "step:coded_answers" });
  await page.locator('.result-item.active[data-id="step:coded_answers"]').waitFor();
  await page.locator(".result-shown .layr-table tbody tr").first().waitFor();
  await stop("25-results-step");

  step("the pick is remembered per experiment");
  await page.locator(".tabs .tab-checks").click();
  await page.locator(".tabs .tab-checks.active").waitFor();
  await page.locator(".tabs .tab-results").click();
  await page.locator('.result-item.active[data-id="step:coded_answers"]').waitFor();

  step("Checks (through a link's params)");
  await h.params({ exp: survey.experiment, tab: "checks" });
  await page.locator(".tabs .tab-checks.active").waitFor();
  await page.locator(".checks li").first().waitFor();
  await stop("26-checks");

  step("Corrections");
  await page.locator(".tabs .tab-corrections").click();
  await page.locator("button", { hasText: "Add a correction" }).waitFor();
  await stop("27-corrections");

  step("Results of the GC example: a figure");
  await showAt({ exp: gc.experiment, tab: "results" });
  await page.locator(".result-item.kind-figure").first().click();
  await until("the figure", async () => (await page.locator(".result-shown img.fig").count()) > 0, 20_000);
  await stop("28-results-figure");

  step("Settings of the GC example: a rows setting changed, saved and run");
  await page.locator(".tabs .tab-settings").click();
  const rows = page.locator("#set-compounds .rows-editor");
  await rows.waitFor();
  const before = (await h.tool("experiment_get", { experiment: gc.experiment })).resolved_params.compounds;
  const first = rows.locator("tr").nth(1).locator("input.num").first();
  const old = Number(await first.inputValue());
  await first.fill(String(+(old + 0.01).toFixed(3)));
  await first.dispatchEvent("change");
  await rows.locator("button", { hasText: "+ Add a row" }).click(); // an empty row is left out when saved
  await stop("29-settings-rows");
  const runs = (await h.tool("runs", { experiment: gc.experiment })).length;
  await page.locator("button", { hasText: "Save and run" }).click();
  await until("the setting saved", async () => {
    const now = (await h.tool("experiment_get", { experiment: gc.experiment })).resolved_params.compounds;
    return now.length === before.length && Math.abs(now[0].rt_min - (old + 0.01)) < 1e-9;
  });
  await until("the run with the new setting", async () => (await h.tool("runs", { experiment: gc.experiment })).length > runs, 60_000);
  await until("it to be done", async () => (await h.tool("run_get", { experiment: gc.experiment })).status === "done", 60_000);
  const used = (await h.tool("run_get", { experiment: gc.experiment })).settings?.compounds?.[0]?.rt_min;
  if (used !== undefined && Math.abs(used - (old + 0.01)) > 1e-9) throw new Error(`the run used rt_min ${used}`);

  step("New experiment from a file (the open dialog)");
  const answers = (await h.tool("experiment_get", { experiment: survey.experiment })).inputs.answers_file;
  answers.path = `${h.data}/plugin/lab/${answers.path}`; // the store keeps it relative to its folder
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  await page.locator(".method-section", { hasText: "Survey scale scores" }).locator("button", { hasText: "New experiment" }).click();
  await page.locator("h2", { hasText: "New experiment" }).waitFor();
  await page.locator(".page input.layr-input").first().fill("My own survey");
  await h.answer("open", { path: answers.path });
  await page.locator("button", { hasText: "Choose file…" }).click();
  await page.locator(".file-row b", { hasText: answers.name }).waitFor({ timeout: 30_000 });
  await stop("30-new-experiment");
  await page.locator("button", { hasText: "Create" }).click();
  await page.locator(".tabs .tab-settings.active").waitFor();
  await until("its title", async () => (await page.locator(".crumb-pick").inputValue()) !== ":none");
  const mine = (await h.tool("experiments")).find((e) => e.title === "My own survey");
  if (!mine || mine.method !== "survey-scores") throw new Error("the new experiment wasn't made with the survey's method");
  await stop("31-new-experiment-made");

  step("the panel opened with a file: New experiment, the file under the input that reads it");
  const ref = await h.addFile({ path: answers.path, name: "answers.csv" });
  await h.setPanelState(null);
  await h.params({ file: ref });
  const second = await open();
  await second.page.locator("h2", { hasText: "New experiment" }).waitFor({ timeout: 60_000 });
  await second.page.locator(".file-row b", { hasText: "answers.csv" }).waitFor();
  const method = await second.page.locator(".page select.layr-select").inputValue();
  if (method !== "survey-scores") throw new Error(`a CSV file picked the method ${method}`);
  await stop("32-opened-with-a-file", second.page);
  await second.errors.check();
  await second.context.close();
  await h.params({});

  step("the narrow panel (700 px)");
  const narrow = await open({ viewport: { width: 700, height: 820 } });
  const np = narrow.page;
  await np.locator(".exp-card, .crumb-pick, h2").first().waitFor({ timeout: 60_000 });
  await showAt({ exp: survey.experiment, tab: "flow", code: false }, np);
  await until("flow boxes", async () => (await np.locator(".fl-node").count()) > 3, 30_000);
  await stop("33-narrow-folded", np);
  await np.locator(".code-toggle").click();
  await np.locator(".mv-grid").waitFor({ state: "visible" });
  await np.waitForTimeout(800);
  await stop("34-narrow-code", np);
  await np.locator(".tabs .tab-results").click();
  await np.locator(".result-item").first().waitFor();
  await stop("35-narrow-results", np);
  await np.locator("#crumbs a", { hasText: "Experiments" }).click();
  await np.locator(".exp-card").first().waitFor();
  await stop("36-narrow-home", np);
  await narrow.errors.check();

  await errors.check();
});

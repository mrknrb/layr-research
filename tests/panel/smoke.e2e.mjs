// Smoke test of the Experiments panel (panels/lab.html): the home grouped by method with both examples, and the
// workspace of the survey example: the flow, the code folded away and shown, looking inside a box both ways, a change
// of the code tried by itself, saved as a new version and run, the experiment picker, the method without data,
// leaving with unsaved code. A screenshot (dark and light) at each stop, in tests/panel/out/ (or $SHOTS). No page
// errors allowed.
//   node tests/panel/smoke.e2e.mjs
import { bench } from "./lib.mjs";

await bench("smoke", async ({ h, open, shot, until, step }) => {
  const { page, errors } = await open();

  // a screenshot in the dark theme, then the same screen in the light one (the SDK's theme event), back to dark
  async function stop(name) {
    await page.waitForTimeout(300); // let the last redraw settle
    await shot(page, `${name}-dark`);
    await h.theme("light");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await page.waitForTimeout(150);
    await shot(page, `${name}-light`);
    await h.theme("dark");
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  }
  const lab = (fn, arg) => page.evaluate(fn, arg);
  const state = () => lab(() => window.__lab.state());
  const box = (title) => page.locator(".fl-node", { has: page.locator(".fl-nt", { hasText: title }) }).first();
  const flowText = () => page.locator(".mv-flow").innerText();

  step("home: every method is a section, none has an experiment yet");
  await page.locator(".method-section").first().waitFor({ timeout: 180_000 }); // the panel polls `methods`
  if ((await page.locator(".exp-card").count()) !== 0) throw new Error("a fresh home shows experiments");
  await page.locator(".no-exp", { hasText: "No experiment yet" }).first().waitFor();
  await stop("01-home-empty");

  step("Try an example › A survey: made, opened in its workspace and run");
  await h.answer("menu", "A survey");
  await page.getByRole("button", { name: /Try an example/ }).click();
  await until("the survey's workspace", async () => (await page.locator(".crumb-pick").count()) > 0, 30_000);
  await until("its run to be done", async () => (await h.tool("experiments")).some((e) => e.last_run?.status === "done"), 60_000);
  const survey = (await h.tool("experiments"))[0];
  if (!/survey/i.test(survey.title)) throw new Error(`the example made is ${survey.title}`);

  step("the GC example and a second survey, made through the tools");
  const gc = await h.tool("lab_sample", { which: "gc" });
  const survey2 = await h.tool("lab_sample", { which: "survey" });
  survey2.title = "A second survey";
  await h.tool("experiment_update", { experiment: survey2.experiment, title: survey2.title });
  for (const e of [gc, survey2]) {
    const r = await h.tool("run", { experiment: e.experiment, wait_s: 55 });
    if (r.status !== "done") throw new Error(`${e.title}: ${r.status} ${JSON.stringify(r.error)}\n${await h.backendLog(2000)}`);
  }

  step("home: grouped by method, both examples in their method's section");
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  const surveySection = page.locator(".method-section", { has: page.locator("h2", { hasText: "Survey scale scores" }) });
  await until("two cards in the survey's section", async () => (await surveySection.locator(".exp-card").count()) === 2);
  const gcSection = page.locator(".method-section", { has: page.locator(".exp-card", { hasText: gc.title }) });
  await gcSection.waitFor();
  await until("the cards to say calculated", async () => (await surveySection.innerText()).includes("calculated"));
  await stop("02-home-grouped");

  step("the survey's workspace: the flow fills the tab, the code is folded away");
  await surveySection.locator(".exp-card", { hasText: survey.title }).click();
  await until("flow boxes", async () => (await page.locator(".fl-node").count()) > 3, 30_000);
  if (await page.locator(".mv-grid").isVisible()) throw new Error("an experiment opened with the code shown");
  if ((await page.locator(".tabs .tab").allInnerTexts()).join("|").indexOf("Flow & code") !== 0)
    throw new Error("the first tab isn't Flow & code");
  await until("the numbers in the flow", async () => /alpha\s*0\.9/.test(await flowText()), 20_000);
  await stop("03-work-folded");

  step("folded: a box is looked into in the flow's own side panel");
  await box("Scale scores").locator(".fl-nh").click();
  await page.locator(".fl-side.open").waitFor();
  await until("the side panel's content", async () => (await page.locator(".fl-side.open").innerText()).length > 40);
  await stop("04-inside-side-panel");

  step("the code shown: the side panel closes, a box shows its function in the code");
  await page.locator(".code-toggle").click();
  await page.locator(".mv-grid").waitFor({ state: "visible" });
  await until("the side panel to close", async () => (await page.locator(".fl-side.open").count()) === 0);
  await until("the code", async () => /import /.test(await page.locator(".cm-content").first().textContent()), 30_000);
  await box("Scale reliability").locator(".fl-nh").click();
  await until("the function at the cursor", async () => (await state())?.fn === "scale_reliability", 10_000);
  await until("its explanation", async () => /Scale reliability/i.test(await page.locator(".mv-inspector").innerText()), 20_000);
  if (await page.locator(".fl-side.open").count()) throw new Error("the side panel opened while the code shows");
  await until("the shared note", async () => /Shared with 1 other experiment/.test(await page.locator(".mv-status").innerText()));
  await stop("05-inside-in-code");

  step("a change of the code (a constant) is tried on the experiment by itself; the numbers follow");
  const before = await lab(() => {
    const ed = window.__lab.editor();
    const doc = ed.view.state.doc;
    for (let i = 1; i <= doc.lines; i++) {
      const l = doc.line(i);
      if (l.text.trim().startsWith("return k / (k - 1) * (1 - sum_item_variances")) {
        const at = l.from + l.text.indexOf("k /");
        ed.view.dispatch({ changes: { from: at, insert: "0.5 * " }, userEvent: "input.type" }); // as if typed
        ed.reveal(i);
        return i;
      }
    }
    return null;
  });
  if (!before) throw new Error("the line of cronbach_alpha wasn't found");
  await until("the trial run", async () => /calculated just now/.test((await state())?.trialNote || ""), 40_000);
  await until("alpha halved in the flow", async () => /alpha\s*0\.46/.test(await flowText()), 20_000);
  if (!(await page.locator(".tab-flow .dirty-dot").count())) throw new Error("the tab doesn't say the code is changed");
  if (await page.locator(".save-btn").isDisabled()) throw new Error("Save isn't enabled with changed code");
  await stop("06-trial-run");

  step("the other tabs show the last real run and say so");
  await page.locator(".tabs .tab-results").click();
  await page.locator(".banner.quiet", { hasText: "isn't saved" }).waitFor();
  await page.locator(".big-number", { hasText: "0.92" }).waitFor();
  await page.locator(".tabs .tab-flow").click();

  step("leaving with unsaved code asks first");
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  await page.locator("#dialog h3", { hasText: "Leave without saving?" }).waitFor();
  await page.locator("#dialog button", { hasText: "Stay" }).click();
  if (!(await state())?.dirty) throw new Error("staying lost the change");

  step("Save: a new version of the shared method, then the experiment runs for real");
  const runsBefore = (await h.tool("runs", { experiment: survey.experiment })).length;
  await page.locator(".save-btn").click();
  await page.locator("#dialog", { hasText: "shared with 1 other experiment" }).waitFor();
  await stop("07-save-dialog");
  await page.locator("#dialog .layr-button.primary", { hasText: "Save" }).click();
  await until("version 2", async () => (await state())?.version === 2 && !(await state()).dirty, 20_000);
  await until("the real run", async () => (await h.tool("runs", { experiment: survey.experiment })).length > runsBefore, 30_000);
  const latest = await h.tool("run_get", { experiment: survey.experiment });
  if (latest.status !== "done" || latest.method?.version !== 2)
    throw new Error(`the run after saving: ${latest.status}, version ${latest.method?.version}`);
  const notes = (await h.log(0, "notify")).map((n) => n.text);
  if (!notes.some((t) => /Saved as version 2 · 1 other experiment is out of date/.test(t)))
    throw new Error(`no notice about the other experiment: ${notes.join(" | ")}`);
  await until("the latest run's numbers in the flow", async () => /alpha\s*0\.46/.test(await flowText()), 20_000);
  await page.locator(".tabs .tab-results").click();
  await page.locator(".big-number", { hasText: "0.46" }).waitFor();
  await stop("08-saved-and-run");

  step("the experiment picker: the other survey, the same tab, the code as it was");
  await page.locator(".crumb-pick").selectOption({ label: `${survey2.title} · calculated` });
  await until("the other experiment", async () => (await state())?.exp === survey2.experiment, 15_000);
  await until("its title in the panel's title", async () => (await h.log(0, "title")).some((t) => t.title === survey2.title));
  if (!(await page.locator(".tabs .tab-results.active").count())) throw new Error("switching experiments left the tab");
  await page.locator(".tabs .tab-flow").click();
  if (!(await page.locator(".mv-grid").isVisible())) throw new Error("switching experiments folded the code");
  await until("its old numbers (run with version 1)", async () => /alpha\s*0\.9/.test(await flowText()), 20_000);
  await stop("09-picker-other-experiment");

  step("No data: just the method");
  await page.locator(".crumb-pick").selectOption({ label: "No data: just the method" });
  await until("no experiment", async () => (await state())?.exp === null, 15_000);
  await page.locator(".tabs-note").waitFor();
  if ((await page.locator(".tabs .tab").count()) !== 1) throw new Error("the method without data shows experiment tabs");
  await until("the inspector to say there's no data", async () => /No data/.test(await page.locator(".mv-inspector").innerText()));
  await stop("10-just-the-method");

  step("the home's Open goes to the method with its reference experiment, code folded");
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  await surveySection.locator("button", { hasText: "Open" }).click();
  await until("the reference experiment", async () => (await state())?.exp === survey.experiment, 15_000);
  if (await page.locator(".mv-grid").isVisible()) throw new Error("the method opened with an experiment shows the code");

  step("back home");
  await page.locator("#crumbs a", { hasText: "Experiments" }).click();
  await surveySection.waitFor();
  await errors.check();
});

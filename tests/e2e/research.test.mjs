// Moved from layr's plugins/dev (task 132): it needs Playwright, and the paths it names as plugins/research
// are this repository's folder. Run it from a layr checkout's plugins/dev with this folder linked.
// End-to-end test of the Research plugin's Experiments panel in headless Chromium, against a running core.
//
// From the repository:  powershell -NoProfile -File scripts/test-core.ps1 start -Link plugins/research -Json
// Then from plugins/dev:  node research.test.mjs --port <port> --token <token> [--home <its home>] [--out <dir>]
// It makes the example experiment and walks the screens the user opens:
// - the experiment: the flow first (hover data, the inside panel with the file's line numbers), each tab opened from
//   its box (Review the peaks with a manual correction, a result's star, the checks, the corrections, a list
//   setting), where a number comes from, a setting changed in a box (calculated again, steps reused, new numbers in
//   Results), an earlier run picked in the flow, Edit this step;
// - the method: the flow above the code, box and code selecting each other, the run's values in the code with their
//   cards and a folded check, completion, hover, the call's inputs, go to definition, Format, a ready-made step added
//   in the flow (its line in the code, a trial run, its table in the new box), a typed change calculated by itself,
//   undo, saving with the numbers that moved on the reference experiment, the versions, the toolbox, an AI draft
//   reviewed with one change rejected (with --home);
// - a new method from nothing to a result; the Python guide and the results database;
// with screenshots in dark, light, 900 pixels and phone width.
import { chromium } from "playwright";
import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const PORT = Number(arg("port", 4300));
const TOKEN = arg("token", "dev");
const HOME = arg("home", null);
const OUT = arg("out", join(tmpdir(), "layr-research-shots"));
mkdirSync(OUT, { recursive: true });

function shellCall(ws, method, params) {
  return new Promise((resolve, reject) => {
    const id = Math.floor(Math.random() * 1e9);
    const on = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id !== id) return;
      ws.removeEventListener("message", on);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    };
    ws.addEventListener("message", on);
    ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}

const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures.push(what);
};

const ws = new WebSocket(`ws://127.0.0.1:${PORT}/_layr/ws?token=${TOKEN}`);
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = () => reject(new Error("is layr serve running?"));
});
async function tool(name, args = {}) {
  const r = await shellCall(ws, "tools.call", { plugin: "research", tool: name, args });
  if (r.isError) throw new Error(r.content?.map((c) => c.text).join(" "));
  const v = r.structuredContent ?? JSON.parse(r.content[0].text);
  return v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 && "result" in v ? v.result : v;
}
// A linked folder with a Python backend runs only in developer mode (docs/ARCHITECTURE.md › Developer mode). This
// script is the shell of a throwaway test home, so it turns it on there.
await shellCall(ws, "settings.set", { key: "developerMode", value: true });
const browser = await chromium.launch();
const errors = [];

async function open(width = 1280, height = 820, theme = "light") {
  const created = await shellCall(ws, "panels.create", { plugin: "research", panel: "experiments" });
  const page = await browser.newPage({ viewport: { width, height }, colorScheme: theme });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto(created.url);
  await page.waitForFunction(() => window.layr?.info, null, { timeout: 60000 });
  await setTheme(page, theme);
  return page;
}
// layr.js takes the theme from the shell (the test home starts dark): set the one wanted on the page itself.
const setTheme = (page, theme) =>
  page.evaluate((t) => {
    document.documentElement.dataset.theme = t;
    new MutationObserver(() => {
      if (document.documentElement.dataset.theme !== t) document.documentElement.dataset.theme = t;
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }, theme);

const shot = (page, name) => page.screenshot({ path: join(OUT, `${name}.png`) });
/** Nothing calculating: the toolbar's status is empty, and on the method screen the code is checked and calculated. */
const idle = (page, timeout = 120000) =>
  page.waitForFunction(
    () =>
      !document.getElementById("status").textContent &&
      !document.querySelector(".actions .layr-button.danger") &&
      !document.querySelector(".fl.loading, .fl.settling, .mv-inspector .layr-spinner") &&
      !window.__lab.state()?.busy,
    null,
    { timeout },
  );
/** Shows a box of the flow on screen (the canvas pans; nothing scrolls it into view by itself). */
const showBox = (page, id) => page.evaluate((id) => window.__lab.flow().select(id), id);
async function hoverFlow(page, selector, id) {
  await showBox(page, id);
  await page.mouse.move(5, 5);
  await page.hover(selector);
  await page.waitForSelector(".fl-card.open", { timeout: 10000 });
  await page.waitForFunction(() => !document.querySelector(".fl-card.open .fl-wait"), null, { timeout: 20000 });
}
/** A cell of the Results table, by its row number and column name. */
async function cell(page, row, column) {
  const headers = await page.locator(".tab-body .layr-table thead th").allTextContents();
  return page.locator(".tab-body .layr-table tbody tr").nth(row).locator("td").nth(headers.indexOf(column));
}
/** Back to the Flow tab, once it has drawn the newest numbers. */
const toFlow = async (page) => {
  await page.click(".tab.tab-flow");
  // the screen is drawn again: once the flow is back in it, it has started loading what changed (if anything)
  await page.waitForSelector(".tab-body.flow-tab .flow-host", { timeout: 30000 });
  await idle(page);
};
const tabActive = (page, id) => page.waitForSelector(`.tab.tab-${id}.active`, { timeout: 30000 });

try {
  // ======================================================================================== the experiment
  const page = await open();
  await page.waitForSelector("text=Try the example", { timeout: 240000 });
  await shot(page, "01-home");
  await page.click("text=Try the example");
  await page.waitForSelector(".flow-host .fl-node", { timeout: 120000 });
  await idle(page);
  await page.waitForSelector(".flow-host .fl-node[data-id=activities] .fl-os:not(:empty)", { timeout: 60000 });
  const tabs = await page.locator(".tabs .tab").allTextContents();
  check(
    tabs[0] === "Flow" && !tabs.includes("Method"),
    `the flow is the first tab, no Method tab (${tabs.join(", ")})`,
  );
  check((await page.locator(".tab.tab-flow.active").count()) === 1, "and it is the one shown");
  check((await page.locator(".flow-host .fl-node").count()) >= 15, "the method's boxes with this experiment's data");
  await shot(page, "02-experiment-flow");

  await hoverFlow(page, ".fl-node[data-id=ratios] .fl-out", "ratios");
  check(
    (await page.locator(".fl-card.open table.fl-tv tbody tr").count()) >= 3,
    "hovering a box's output shows its first rows",
  );
  await shot(page, "03-hover-data");
  await hoverFlow(page, ".fl-node[data-id=figure] .fl-res", "figure");
  await page.waitForSelector(".fl-card.open img.fl-fig", { timeout: 20000 });
  check(true, "hovering a figure the method returns shows the figure");
  await page.mouse.move(5, 5);

  // the inside panel: the step's code with this run's values, numbered as in the method's file
  await showBox(page, "activities");
  await page.click(".fl-node[data-id=activities] .fl-nt");
  await page.waitForSelector(".fl-side.open .cmx-chip", { timeout: 30000 });
  check((await page.locator(".fl-side .cmx-chip").count()) > 5, "inside a step: its values at the lines");
  const firstNo = Number(await page.locator(".fl-side .cm-lineNumbers .cm-gutterElement").nth(1).textContent());
  check(firstNo > 100, `the inside panel numbers lines as the file does (starts at ${firstNo})`);
  check(
    (await page.locator(".fl-side .cmx-guard.plain").count()) === 1,
    "its description is folded with a plain look (not a warning)",
  );
  check(
    (await page.locator(".fl-side .cmx-values.below").count()) >= 1,
    "values of a long line go on their own row under it",
  );
  await page.waitForSelector(".fl-side .fl-fm .katex", { timeout: 10000 });
  await shot(page, "04-inside-panel");
  await page.click(".fl-side button[title=Close]");

  // each tab opens from its box: the peaks from the box that integrates them
  await showBox(page, "peaks");
  await page.click(".fl-node[data-id=peaks] .fl-act:text('Review the peaks')");
  await tabActive(page, "peaks");
  await page.waitForSelector("svg.chrom", { timeout: 30000 });
  check((await page.locator(".inj-list li").count()) > 20, "Review the peaks opens the peaks, each injection listed");
  await shot(page, "05-peaks");
  const handle = page.locator(".chrom .handle").nth(1).locator("rect");
  const box = await handle.boundingBox();
  await page.mouse.move(box.x + 6, box.y + 9);
  await page.mouse.down();
  await page.mouse.move(box.x + 40, box.y + 9, { steps: 5 });
  await page.mouse.up();
  check(await page.locator("button:text('Save these boundaries…')").isEnabled(), "dragging enables saving");
  await page.click("button:text('Save these boundaries…')");
  await page.fill(".dialog textarea", "Test: tail cut too early by the automatic end");
  await page.click(".dialog button:text('Save')");
  await page.waitForSelector(".active-corr", { timeout: 60000 });
  await idle(page, 60000);

  // a result's star opens Results at that table; a number's chain of formulas
  await toFlow(page);
  await page.waitForSelector(".flow-host .fl-node[data-id=activities] .fl-res");
  await showBox(page, "activities");
  await page.click(".fl-node[data-id=activities] .fl-res");
  await tabActive(page, "results");
  await page.waitForSelector(".tab-body .layr-table", { timeout: 30000 });
  check(
    (await page.locator(".section-head select").inputValue()) === "activities",
    "a result's star opens Results at that table",
  );
  await page.waitForSelector(".tab-body .figs img.fig", { timeout: 20000 });
  check(true, "Results shows the figure (read through value_get)");
  await shot(page, "06-results");
  const activity1 = await (await cell(page, 0, "activity_U_per_gCDW")).textContent();
  await (await cell(page, 0, "activity_U_per_gCDW")).click();
  await page.waitForSelector(".dialog[open] .prov-step", { timeout: 15000 });
  check((await page.locator(".dialog .prov-step").count()) >= 2, "an activity traces back to its formula and line");
  await shot(page, "07-provenance");
  await page.click(".dialog button:text('Close')");

  // the checks from the folded quality box; the corrections from their box; a list setting from its box
  await toFlow(page);
  await page.waitForSelector(".flow-host .fl-node.folded.q .fl-act");
  await showBox(page, "Q");
  await page.click(".fl-node.folded.q .fl-act:text('See the checks')");
  await tabActive(page, "checks");
  await page.waitForSelector(".checks li");
  check(true, "the quality checks box opens the checks");
  await shot(page, "08-checks");
  await toFlow(page);
  await showBox(page, "in:corrections");
  await page.click(".fl-node[data-id='in:corrections'] .fl-act");
  await tabActive(page, "corrections");
  await page.waitForSelector("text=Test: tail cut too early");
  check(true, "the corrections box opens the corrections, with the manual one");
  await toFlow(page);
  await showBox(page, "samples");
  await page.click(".fl-node[data-id=samples] .fl-port.set .fl-sv");
  await tabActive(page, "settings");
  await page.waitForSelector("#set-sample_patterns.focus", { timeout: 15000 });
  check(true, "a list setting in a box opens Settings at that setting");
  await shot(page, "09-settings");

  // a setting changed in a box: calculated again by itself, the steps before it reused, new numbers in Results
  await toFlow(page);
  await showBox(page, "concentrations");
  await page.click(".fl-node[data-id=concentrations] .fl-port[data-port=concentration_factor] .fl-sv");
  await page.keyboard.press("Control+A");
  await page.keyboard.type("2");
  // a remembered run is back in well under a second: the status is watched from before the key is pressed
  await page.evaluate(() => {
    window.__sawCalc = false;
    const s = document.getElementById("status");
    new MutationObserver(() => s.textContent.startsWith("Calculating") && (window.__sawCalc = true)).observe(s,
      { childList: true, characterData: true, subtree: true });
  });
  const setAt = Date.now();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => window.__sawCalc, null, { timeout: 15000 });
  check(true, "changing a setting in a box calculates the experiment again by itself");
  await idle(page);
  console.log(`     (calculated and drawn ${Date.now() - setAt} ms after the change)`);
  await page.waitForFunction(
    () => document.querySelectorAll(".flow-host .fl-node .fl-badge").length > 0 &&
      [...document.querySelectorAll(".flow-host .fl-badge")].some((b) => b.textContent === "reused"),
    null,
    { timeout: 30000 },
  );
  const reused = await page.locator(".flow-host .fl-badge:text('reused')").count();
  check(reused >= 3, `the steps before it were reused, and marked (${reused})`);
  await shot(page, "10-setting-recalculated");
  await page.click(".tab.tab-results");
  await page.waitForSelector(".tab-body .layr-table", { timeout: 30000 });
  const activity2 = await (await cell(page, 0, "activity_U_per_gCDW")).textContent();
  check(
    Math.abs(Number(activity2) / Number(activity1) - 2) < 0.01,
    `the new numbers in Results (${activity1} → ${activity2})`,
  );

  // an earlier run picked in the flow: said clearly, and Results follows it
  await toFlow(page);
  await page.waitForSelector(".fl-pick select >> nth=1");
  const runOptions = await page.locator(".fl-pick select").nth(1).locator("option").count();
  await page.locator(".fl-pick select").nth(1).selectOption({ index: runOptions - 1 });
  await page.waitForSelector(".banner.earlier", { timeout: 30000 });
  check(/earlier run/.test(await page.locator(".banner.earlier").textContent()), "an earlier run is said clearly");
  await shot(page, "11-earlier-run");
  await page.click(".tab.tab-results");
  await page.waitForSelector(".tab-body .layr-table", { timeout: 30000 });
  const old = await (await cell(page, 0, "activity_U_per_gCDW")).textContent();
  check(old !== activity2, `Results shows the earlier run's numbers (${old})`);
  await page.click(".banner.earlier a:text('Back to the latest run')");
  await page.waitForFunction(() => !document.querySelector(".banner.earlier"));
  await toFlow(page);

  // Edit this step: the method's screen at that function, with this experiment's data
  await showBox(page, "ratios");
  await page.click(".fl-node[data-id=ratios] .fl-nt");
  await page.waitForSelector(".fl-side.open button:text('Edit this step')", { timeout: 30000 });
  await page.click(".fl-side button:text('Edit this step')");
  await page.waitForSelector(".mv-flow .fl-node", { timeout: 60000 });
  await page.waitForSelector(".mv-editor .cm-editor", { timeout: 30000 });
  check(
    (await page.evaluate(() => window.__lab.state().fn)) === "area_ratios",
    "Edit this step opens the method's code at that function",
  );

  // ======================================================================================== the method
  await idle(page);
  check((await page.locator(".ol-item").count()) > 50, "outline: recipe, steps, formulas, helpers and the toolbox");
  check((await page.locator(".lint-count.ok").count()) === 1, "the built-in method passes the style checker");
  check((await page.locator("script[src*='codemirror']").count()) === 0, "no CodeMirror 5 on the page");
  await page.waitForSelector(".mv-editor .cmx-chip", { timeout: 30000 });
  await shot(page, "12-method");

  const code = () => page.evaluate(() => window.__lab.editor().getValue());
  const cursorLine = () =>
    page.evaluate(() => {
      const ed = window.__lab.editor();
      return ed.getValue().split("\n")[ed.cursor().line - 1];
    });
  /** Replaces the whole text as typing would (so the panel sees an edit). */
  const typeAll = (src) =>
    page.evaluate((text) => {
      const v = window.__lab.editor().view;
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: text }, userEvent: "input" });
    }, src);
  /** Puts the cursor at the end of the first line that has `text`. */
  const cursorAfter = (text) =>
    page.evaluate((want) => {
      const v = window.__lab.editor().view;
      const n = v.state.doc.toString().split("\n").findIndex((l) => l.includes(want)) + 1;
      v.dispatch({ selection: { anchor: v.state.doc.line(n).to } });
      v.focus();
    }, text);
  /** Empties the cursor's line but for its indent, as typing would. */
  const clearLine = () =>
    page.evaluate(() => {
      const v = window.__lab.editor().view;
      const l = v.state.doc.lineAt(v.state.selection.main.head);
      const from = l.from + /^\s*/.exec(l.text)[0].length;
      v.dispatch({ changes: { from, to: l.to }, selection: { anchor: from }, userEvent: "delete" });
    });
  /**
   * Takes the selected completion. CodeMirror ignores Enter on a list shown for less than its interactionDelay
   * (75 ms), so that a key pressed just as the list appears doesn't pick from it: wait until the list has been there
   * that long, as a person's keypress would.
   */
  const takeCompletion = async (label) => {
    await page.waitForFunction(
      (want) =>
        document.querySelector(".cm-tooltip-autocomplete li[aria-selected] .cm-completionLabel")?.textContent === want,
      label,
      { timeout: 30000 },
    );
    await page.waitForTimeout(120);
    await page.keyboard.press("Enter");
  };
  const original = await code();

  // a box selects its function in the code; the cursor in a function or a recipe line selects its box
  await showBox(page, "calibration");
  await page.click(".mv-flow .fl-node[data-id=calibration] .fl-nt");
  await page.waitForFunction(() => window.__lab.state().fn === "fit_calibration", null, { timeout: 15000 });
  check(/def fit_calibration\(/.test(await cursorLine()), "a box shows its function in the code");
  await cursorAfter("concentrations = quantify(");
  await page.waitForFunction(() => document.querySelector(".mv-flow .fl-node.sel")?.dataset.id === "concentrations",
    null, { timeout: 15000 });
  check(true, "a line of the recipe selects its box");
  await cursorAfter("def area_ratios(");
  await page.waitForFunction(() => document.querySelector(".mv-flow .fl-node.sel")?.dataset.id === "ratios", null,
    { timeout: 15000 });
  check(true, "the cursor in a step's function selects its box");
  await idle(page);
  await shot(page, "13-box-and-code");

  // this run's values at the ends of the lines, a value's card, a folded check
  await page.click(".ol-item:has-text('area_ratios')");
  await page.waitForSelector(".mv-editor .cmx-chip", { timeout: 30000 });
  const chips = await page.locator(".mv-editor .cmx-chip").allTextContents();
  check(
    chips.some((c) => /^standard = table \d+ × \d+/.test(c)) && chips.some((c) => c.startsWith("→ table")),
    `the run's values at the ends of the lines (${chips.length}: ${chips.slice(0, 3).join(" | ")}…)`,
  );
  await page.locator(".mv-editor .cmx-chip", { hasText: "usable = table" }).hover();
  await page.waitForSelector(".cmx-card.open .value-card table", { timeout: 15000 });
  check(
    (await page.locator(".cmx-card.open .value-card th").allTextContents()).includes("area"),
    "a table's card shows its first rows (value_get)",
  );
  await shot(page, "14-values-card");
  await page.mouse.move(5, 5);
  await page.waitForFunction(() => !document.querySelector(".cmx-card.open"));
  const guard = page.locator(".mv-editor .cmx-guard:not(.plain)");
  check(
    (await guard.count()) === 1 && (await guard.textContent()).includes("stops with a message if standard.empty"),
    "the check (an if that only stops with a message) is folded",
  );
  await page.click(".folds-toggle");
  check((await guard.count()) === 0, "the status line's button unfolds it");
  await page.click(".folds-toggle");
  check((await guard.count()) === 1, "and folds it again");

  // help while typing: completion (with the run's columns), the call's inputs, go to definition, hover
  await cursorAfter("usable = standard[");
  await page.keyboard.press("Enter");
  await page.keyboard.type("x = usable.");
  await page.waitForSelector(".cm-tooltip-autocomplete li", { timeout: 30000 });
  const items = await page.locator(".cm-tooltip-autocomplete li .cm-completionLabel").allTextContents();
  check(
    items.slice(0, 30).includes("status") && items[0] === "area",
    `completion after usable. starts with its columns (${items.slice(0, 5)})`,
  );
  await shot(page, "15-completion");
  await page.keyboard.press("Escape");
  await clearLine();
  await page.keyboard.type('x = usable["ar');
  await takeCompletion("area");
  const taken = (await cursorLine()).trim();
  check(
    taken === 'x = usable["area"]',
    `taking a column inside usable["…"] closes the quote and the bracket (${taken})`,
  );
  await clearLine();
  await page.keyboard.type("y = area_ratio(");
  await page.waitForSelector(".cmx-signature", { timeout: 30000 });
  const bold = (await page.locator(".cmx-signature-label b").textContent()).trim();
  check(bold === "area", `the call's inputs while typing (${bold})`);
  await shot(page, "16-signature");
  await page.keyboard.press("Escape");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("F12");
  await page.waitForFunction(() => window.__lab.editor().cursor().line < 40, null, { timeout: 30000 });
  check((await cursorLine()).startsWith("def area_ratio("), "F12 goes to where the function is defined");
  await typeAll(original);
  await page.click(".ol-item:has-text('area_ratios')");
  // hover once the code is checked and calculated: until then its values and folds are drawn again under the pointer
  await idle(page);
  const fnToken = page
    .locator(".mv-editor .cm-line", { hasText: 'ratios["ratio"] = area_ratio(' })
    .locator(".tok-fn")
    .first();
  await page.mouse.move(5, 5);
  await fnToken.hover();
  await page.waitForSelector(".cm-tooltip-hover .code-hover", { timeout: 15000 });
  check(
    /area_ratio\(/.test(await page.locator(".cm-tooltip-hover .code-hover").textContent()),
    "hover shows what a function is (its signature and sentence)",
  );
  await shot(page, "17-hover");
  await page.mouse.move(5, 5);

  // Format (More ▾ › Format the code)
  await cursorAfter("usable = standard[");
  await page.keyboard.press("Enter");
  await page.keyboard.type("z  =  usable");
  await page.evaluate(() => window.__lab.format());
  await page.waitForFunction(() => window.__lab.editor().getValue().includes("    z = usable\n"), null, {
    timeout: 30000,
  });
  check(true, "Format tidies the code (code_format)");
  await typeAll(original);
  await idle(page);

  // a ready-made step added in the flow: its line in the code, a trial run, its table in the new box; then undone
  await showBox(page, "ratios");
  await page.hover(".mv-flow .fl-node[data-id=ratios]");
  await page.click(".mv-flow .fl-node[data-id=ratios] .fl-add");
  await page.click(".fl-menu .fl-mi:has-text('Pick rows')");
  await page.waitForSelector(".fl-form select");
  await page.locator(".fl-form select").first().selectOption("role");
  await page.fill(".fl-form label:has-text('Equals') input", "sample");
  await page.fill(".fl-form label:has-text('Name of what it makes') input", "sample_ratios");
  await page.click(".fl-form button:text('Add the step')");
  await page.waitForFunction(() => /sample_ratios = pick_rows\(ratios/.test(window.__lab.editor().getValue()), null,
    { timeout: 30000 });
  check(true, "a step added in the flow is a line in the code");
  check((await page.locator(".mv-status .dirty").count()) === 1, "not saved yet");
  await idle(page);
  const trial = await page.evaluate(() => window.__lab.state());
  check(!!trial.trialRun && /calculated just now/.test(trial.trialNote),
    `a trial run calculated it by itself (${trial.trialNote})`);
  await page.waitForSelector(".mv-flow .fl-node[data-id=sample_ratios] .fl-os:not(:empty)", { timeout: 30000 });
  check(
    /\d+ × \d+/.test(await page.locator(".mv-flow .fl-node[data-id=sample_ratios] .fl-os").textContent()),
    "the new box has its table",
  );
  await showBox(page, "sample_ratios");
  await idle(page);
  // its box explains the ready-made step with the trial run's numbers (not the saved run's "0 calls")
  await page.click(".mv-flow .fl-node[data-id=sample_ratios] .fl-nt");
  await page.waitForFunction(() => window.__lab.state().fn === "pick_rows", null, { timeout: 15000 });
  await idle(page);
  const calls = await page.locator(".mv-inspector h4", { hasText: "In the last run" }).textContent();
  check(/In the last run: 1 call/.test(calls), `the explanation counts the trial run's calls (${calls})`);
  await shot(page, "18-block-added");
  await page.evaluate(() => window.__lab.editor().focus());
  await page.keyboard.press("Control+z");
  await page.waitForFunction(() => !/pick_rows/.test(window.__lab.editor().getValue()), null, { timeout: 15000 });
  await idle(page);
  await page.waitForFunction(() => !document.querySelector(".mv-flow .fl-node[data-id=sample_ratios]"), null,
    { timeout: 30000 });
  check(true, "Ctrl+Z takes the step out of the code and the flow");

  // a typed change is calculated by itself: the flow and the values show the trial run's numbers
  const before = await page.evaluate(() => window.__lab.state().trialRun);
  await typeAll(
    original.replace("rate_mM_per_min * 1000 / biomass_gCDW_per_L", "rate_mM_per_min * 500 / biomass_gCDW_per_L"),
  );
  await idle(page);
  const after = await page.evaluate(() => window.__lab.state());
  check(after.trialRun && after.trialRun !== before, `a typed change gets its own trial run (${after.trialNote})`);
  check(/\d+ of \d+ reused/.test(after.trialNote), "only what changed is calculated again");
  check(
    (await page.evaluate(() => window.__lab.flow().graph.run?.id)) === after.trialRun,
    "the flow shows the trial run's values",
  );
  const halved = await page.evaluate(async (run) => {
    const t = await layr.tools.run("result_table", { run, table: "activities" });
    return t.rows[0].activity_U_per_gCDW;
  }, after.trialRun);
  const latest = await page.evaluate(async (experiment) => {
    const t = await layr.tools.run("result_table", { experiment, table: "activities" });
    return t.rows[0].activity_U_per_gCDW;
  }, after.numbersExp);
  check(Math.abs(halved / latest - 0.5) < 0.01, `with the new numbers (${latest} → ${halved})`);
  await shot(page, "19-typed-and-calculated");
  await page.evaluate(() => window.__lab.editor().focus());
  await page.keyboard.press("Control+z");
  await page.waitForFunction((src) => window.__lab.editor().getValue() === src, original, { timeout: 15000 });
  await idle(page);
  check((await page.locator(".mv-status .dirty").count()) === 0, "Ctrl+Z brings the saved code back");

  // code the checker can't read: the flow keeps its last drawing and says so, with the line
  await typeAll(original.replace("    ratios = area_ratios(peaks, internal_standard)",
    "    ratios = area_ratios(peaks, internal_standard"));
  await page.waitForSelector(".mv-flow .fl-note:has-text(\"can't be read yet\")", { timeout: 30000 });
  check(
    (await page.locator(".mv-flow .fl-node").count()) > 10 &&
      /line \d+/.test(await page.locator(".mv-flow .fl-note").textContent()),
    "code that can't be read: the flow keeps its boxes and says which line",
  );
  await shot(page, "19b-cant-read");
  await typeAll(original);
  await page.waitForFunction(() => !/can't be read/.test(document.querySelector(".mv-flow .fl-note")?.textContent),
    null, { timeout: 30000 });
  await idle(page);

  // "Describe it, Claude writes it": the test core has no Claude, so its answer is given here; the rest is real
  // (the new function and its step in the code by flow_edit, the prompt by ai_prompt, the review, the AI mark)
  await page.evaluate(() => {
    const run = layr.tools.run.bind(layr.tools);
    layr.tools.run = (name, args, o) =>
      name === "ask"
        ? Promise.resolve({
            text:
              "```python\ndef keep_only_samples(ratios):\n" +
              '    """Keep the rows of the samples (not the standards or blanks)."""\n' +
              '    return ratios[ratios.role == "sample"]\n```\nIt keeps the samples\' rows.',
          })
        : run(name, args, o);
  });
  await page.evaluate(() =>
    window.__lab.claudeStep({ wire: "ratios", from: "ratios", columns: ["role"], sentence: "Keep only the samples." }),
  );
  await page.waitForSelector(".review .cmx-merge", { timeout: 60000 });
  const left = await page.evaluate(() => window.__lab.merge().counts().left);
  check(left >= 1, "Claude's step comes as a change to review");
  await shot(page, "20a-claude-step-review");
  await page.click(".review button:text('Put it in the code')");
  await page.waitForFunction(
    () => /def keep_only_samples\(ratios\):[\s\S]*= keep_only_samples\(ratios\)/.test(window.__lab.editor().getValue()),
    null,
    { timeout: 15000 },
  );
  check(true, "accepted, its function and its step are in the code");
  await page.waitForSelector(".banner.ai-step", { timeout: 15000 });
  const mark = await page.locator(".banner.ai-step").textContent();
  check(/Written by AI/.test(mark), "marked as written by AI until saved");
  await idle(page);
  await shot(page, "20b-claude-step-in");
  await page.evaluate(() => window.__lab.editor().focus());
  await page.keyboard.press("Control+z");
  await page.waitForFunction((src) => window.__lab.editor().getValue() === src, original, { timeout: 15000 });
  await idle(page);
  check((await page.locator(".banner.ai-step").count()) === 0, "one Ctrl+Z takes it out again");

  // change the code, the checker notes it, saving makes version 2 and the reference numbers that moved
  await typeAll(
    original
      .replace("rate_mM_per_min * 1000 / biomass_gCDW_per_L", "rate_mM_per_min * 500 / biomass_gCDW_per_L")
      .replace('    """How fast the concentration changed between two time points, in mM per minute."""\n', ""),
  );
  await page.waitForSelector(".mv-status .lint-count.warning", { timeout: 15000 });
  // the explanation on the right lists the checker's messages once it has drawn
  await page.locator(".mv-inspector .lint-list li", { hasText: "rate_between` has no docstring" }).first().waitFor({
    timeout: 15000,
  });
  check(true, "the checker says why");
  await page.evaluate(() => {
    const ed = window.__lab.editor();
    ed.reveal(ed.getValue().split("\n").findIndex((l) => l.startsWith("def rate_between")) + 1);
  });
  await page.waitForSelector(".mv-editor .cm-lint-marker-warning", { timeout: 5000 });
  check(true, "and marks the line in the gutter");
  await idle(page);
  await page.click(".actions button:text('Save')");
  await page.fill(".dialog input", "Test: half the activity");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".mv-status:has-text('version 2')", { timeout: 30000 });
  check(true, "Enter in the save dialog saves");
  // the comparison's end comes as an event (the panel doesn't poll for it)
  await page.waitForSelector(".banner .comparison", { timeout: 120000 });
  const summary = await page.locator(".banner .comparison").first().textContent();
  check(
    /numbers in \d+ tables moved/.test(summary),
    `numbers that moved on the reference experiment: ${summary.slice(0, 60)}`,
  );
  check(
    (await page.locator(".banner .comparison details.cmp-tables:not([open])").count()) === 1,
    "which numbers moved is folded to one line (the flow keeps its room)",
  );
  await shot(page, "20-method-saved");
  await page.click(".actions button:text('Versions')");
  await page.waitForSelector(".dialog .versions li");
  check((await page.locator(".dialog .versions li").count()) === 2, "two versions");
  await page.locator(".dialog .versions li").first().locator("button:text('Changed lines')").click();
  await page.waitForSelector(".dialog .cmx-merge .cm-changedLine, .dialog .cmx-merge .cm-deletedChunk", {
    timeout: 15000,
  });
  check((await page.locator(".dialog .cmx-merge [data-act]").count()) === 0, "the versions' changes, read only");
  await shot(page, "21-versions");
  await page.click(".dialog button:text('Close')");

  // the toolbox: read only, copied on request
  await page.click("summary:has-text('Toolbox')");
  await page.click(".ol-item.toolbox:has-text('fit_line')");
  await page.waitForSelector(".mv-inspector button:text('Copy into my method to change it')", { timeout: 15000 });
  check((await page.locator(".mv-inspector .formula .katex").count()) >= 3, "toolbox formulas from its docstring");
  check((await page.locator(".mv-inspector .cmx-highlight .tok-kw").count()) > 0, "its code, highlighted");

  // an AI draft waits for the user (written into the test home, as the method_write tool does for an agent):
  // reviewed chunk by chunk, one change kept and one dropped
  if (HOME) {
    const dir = join(HOME, "plugin-data", "research", "lab", "methods", "gc-activity-assay");
    const current = readFileSync(join(dir, "2.py"), "utf-8");
    writeFileSync(
      join(dir, "draft.py"),
      current
        .replace("* 500 /", "* 1000 /")
        .replace("This cancels differences in pipetting", "This evens out differences in pipetting"),
    );
    writeFileSync(
      join(dir, "draft.json"),
      JSON.stringify({
        by: "AI agent (test)",
        at: new Date().toISOString(),
        change: "Back to 1000, and a clearer docstring",
        base_version: 2,
        messages: [],
      }),
    );
    await page.click(".crumbs a:text('Experiments')");
    await page.waitForSelector("text=change proposed");
    await page.click("li:has-text('GC-FID activity assay')");
    await page.waitForSelector(".banner.draft", { timeout: 30000 });
    await page.click(".banner.draft button:text('Review the changes')");
    await page.waitForSelector(".review .cmx-merge [data-act=accept]:visible", { timeout: 30000 });
    check((await page.evaluate(() => window.__lab.merge().counts().left)) === 2, "the draft's two changes to review");
    await shot(page, "22-draft-review");
    await page.locator(".review [data-act=accept]:visible").first().click();
    await page.waitForFunction(() => window.__lab.merge().counts().accepted === 1);
    await page.locator(".review [data-act=reject]:visible").first().click();
    await page.waitForFunction(() => window.__lab.merge().counts().rejected === 1);
    check(
      (await page.locator(".review button:text('Save what stands')").count()) === 1,
      "with a change rejected, Accept becomes Save what stands",
    );
    await page.click(".review button:text('Save what stands')");
    await page.waitForSelector(".mv-status:has-text('version 3')", { timeout: 30000 });
    const saved = readFileSync(join(dir, "3.py"), "utf-8");
    check(
      saved.includes("* 1000 /") && saved.includes("This cancels differences") && !saved.includes("evens out"),
      "version 3 has the kept change and not the rejected one",
    );
    check((await page.locator(".banner.draft").count()) === 0, "the draft is gone");
  } else console.log("skip (no --home) the AI draft check");

  // ======================================================================================== a new method from nothing
  await page.click(".crumbs a:text('Experiments')");
  await page.click("button:text('New method')");
  await page.fill(".dialog input", "Plate growth");
  await page.fill(".dialog textarea", "Growth of cultures read on a plate.");
  await page.click(".dialog button:text('Make it')");
  await page.waitForSelector(".mv-flow .fl-empty.open button:text('Add a raw file')", { timeout: 60000 });
  check(true, "a new method opens with an empty flow that says what to do first");
  await idle(page);
  await shot(page, "23-new-method");
  await page.click(".mv-flow .fl-empty button:text('Add a raw file')");
  await page.fill(".fl-form label:has-text('What it holds') input", "plate reads");
  await page.click(".fl-form button:text('Add it')");
  await page.waitForSelector(".mv-flow .fl-node[data-id=plate_reads]", { timeout: 30000 });
  await idle(page);
  await showBox(page, "plate_reads");
  await page.hover(".mv-flow .fl-node[data-id=plate_reads]");
  await page.click(".mv-flow .fl-node[data-id=plate_reads] .fl-add");
  await page.click(".fl-menu .fl-mi:has-text('Summarize by group')");
  await page.fill(".fl-form label:has-text('Values') input", "od600");
  await page.locator(".fl-form details summary").click();
  await page.fill(".fl-form label:has-text('For each') input", "strain");
  await page.fill(".fl-form label:has-text('Name of what it makes') input", "summary");
  await page.click(".fl-form button:text('Add the step')");
  await page.waitForSelector(".mv-flow .fl-node[data-id=summary]", { timeout: 30000 });
  await showBox(page, "summary");
  await page.click(".mv-flow .fl-node[data-id=summary] .fl-more");
  await page.click(".fl-menu .fl-mi:has-text('Make it a result')");
  await page.click(".fl-form button:text('Make it a result')");
  await page.waitForSelector(".mv-flow .fl-node[data-id=summary] .fl-res", { timeout: 30000 });
  await idle(page);
  check(/summarize\(plate_reads, "od600"/.test(await code()), "built in the flow: the code has its steps");
  await page.evaluate(() => window.__lab.editor().focus());
  await page.keyboard.press("Control+s");
  await page.fill(".dialog input", "Built in the flow");
  await page.click(".dialog button:text('Save')");
  await page.waitForSelector(".mv-status:has-text('version 2')", { timeout: 30000 });
  // the experiment with a file (picking a file needs the app's file dialog: made here with the tool)
  const csv = join(OUT, "plate.csv");
  writeFileSync(csv, "strain,well,od600\nWT,A1,0.51\nWT,A2,0.49\nmut,B1,0.30\nmut,B2,0.34\n");
  const methodId = (await tool("methods")).find((m) => m.name === "Plate growth").id;
  const made = await tool("experiment_create", {
    title: "Plate test",
    method: methodId,
    files: { plate_reads_file: csv },
  });
  const r = await tool("run", { experiment: made.experiment ?? made.id, wait_s: 55 });
  check(r.status === "done", `the method built from nothing runs (${r.status})`);
  await page.click(".crumbs a:text('Experiments')");
  await page.click("li:has-text('Plate growth')");
  await page.waitForSelector(".mv-flow .fl-node[data-id=summary] .fl-os:not(:empty)", { timeout: 60000 });
  check(
    /2 × \d+/.test(await page.locator(".mv-flow .fl-node[data-id=summary] .fl-os").textContent()),
    "and its flow shows the result's table",
  );
  await showBox(page, "summary");
  await idle(page);
  await shot(page, "24-new-method-result");

  await page.click(".actions button:text('Python guide')");
  await page.waitForSelector(".guide section");
  check((await page.locator(".guide section").count()) >= 10, "the Python guide");
  check((await page.locator(".guide .cmx-highlight .tok-kw").count()) > 10, "its examples highlighted");
  await shot(page, "25-guide");
  await page.click(".crumbs a:text('Experiments')");
  await page.click("button:text('Results database')");
  await page.waitForSelector(".layr-table");
  await shot(page, "26-database");

  // ======================================================================================== a look in each size
  for (const [w, hgt, theme, tag] of [
    [1280, 820, "dark", "dark"],
    [900, 760, "light", "900px"],
    [400, 800, "dark", "phone"],
  ]) {
    const p = await open(w, hgt, theme);
    await p.waitForSelector(".exp-card", { timeout: 60000 });
    await p.click(".exp-card:has-text('Example')");
    await p.waitForSelector(".flow-host .fl-node[data-id=activities] .fl-os:not(:empty)", { timeout: 60000 });
    await idle(p);
    await shot(p, `27-${tag}-experiment`);
    await showBox(p, "activities");
    await p.click(".fl-node[data-id=activities] .fl-nt");
    await p.waitForSelector(".fl-side.open .cmx-chip", { timeout: 30000 });
    await shot(p, `28-${tag}-inside`);
    await p.click(".crumbs a:text('Experiments')");
    await p.click("li:has-text('GC-FID activity assay')");
    await p.waitForSelector(".mv-flow .fl-node", { timeout: 60000 });
    await p.click(".ol-item:has-text('area_ratios')");
    await p.waitForSelector(".mv-editor .cmx-chip", { timeout: 30000 });
    await idle(p);
    await shot(p, `29-${tag}-method`);
    await p.close();
  }
} catch (err) {
  failures.push(String(err));
  console.error(err);
} finally {
  check(errors.length === 0, `no page errors${errors.length ? ": " + errors.join(" | ") : ""}`);
  await browser.close();
  ws.close();
  console.log(`screenshots in ${OUT}`);
  process.exit(failures.length ? 1 : 0);
}

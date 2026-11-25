// Moved from layr's plugins/dev (task 132): it needs Playwright, and the paths it names as plugins/research
// are this repository's folder. Run it from a layr checkout's plugins/dev with this folder linked.
// End-to-end test of the Research plugin's flow (plugins/research/panels/flow.js) in headless Chromium, against a
// running core: it opens panels/dev-flow.html (a small host page that mounts the flow) as a page of the plugin.
//
// From the repository:  powershell -NoProfile -File scripts/test-core.ps1 start -Link plugins/research -Json
// Then from plugins/dev:  node research-flow.test.mjs --port <port> --token <token> [--out <dir for screenshots>]
// It makes the example experiment and runs it twice, then checks the boxes and wires, hover cards with real rows, the
// inside panel's values, the data picker, folding, a setting edit, adding a ready-made step, connecting, renaming,
// removing, and a new method built up from nothing in the flow until it runs; with screenshots (dark, light, narrow).
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const PORT = Number(arg("port", 4300));
const TOKEN = arg("token", "dev");
const OUT = arg("out", join(tmpdir(), "layr-research-flow-shots"));
const ONLY = arg("only", null); // a part to run alone while working on it: view, edit, new
mkdirSync(OUT, { recursive: true });

const ws = new WebSocket(`ws://127.0.0.1:${PORT}/_layr/ws?token=${TOKEN}`);
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = () => reject(new Error("is the test core running?"));
});
function shellCall(method, params) {
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
async function tool(name, args = {}) {
  const r = await shellCall("tools.call", { plugin: "research", tool: name, args });
  if (r.isError) throw new Error(r.content?.map((c) => c.text).join(" "));
  const v = r.structuredContent ?? JSON.parse(r.content[0].text);
  return v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 && "result" in v ? v.result : v;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures.push(what);
};

await shellCall("settings.set", { key: "developerMode", value: true });
for (let i = 0; ; i++) {
  try {
    await tool("methods");
    break;
  } catch (e) {
    if (i > 80) throw e;
    await sleep(3000);
  }
}
// the example experiment, calculated twice (the second time its steps are reused)
let exp = (await tool("experiments")).find((e) => e.method === "gc-activity-assay");
if (!exp) {
  const made = await tool("lab_sample");
  exp = { experiment: made.experiment ?? made.id };
}
const EXP = exp.experiment;
let runs = (await tool("runs", { experiment: EXP })).filter((r) => !r.trial);
while (runs.length < 2) {
  const r = await tool("run", { experiment: EXP, wait_s: 55 });
  check(r.status === "done", `the example runs (${r.status})`);
  runs = (await tool("runs", { experiment: EXP })).filter((r) => !r.trial);
}

const browser = await chromium.launch();
const errors = [];
async function open({ width = 1280, height = 820, theme = "dark", params = "" } = {}) {
  const created = await shellCall("panels.create", { plugin: "research", panel: "experiments" });
  const url = created.url.replace("/panels/lab.html", "/panels/dev-flow.html") +
    `&method=gc-activity-assay&experiment=${EXP}${params}`;
  const page = await browser.newPage({ viewport: { width, height }, colorScheme: theme });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto(url);
  await page.waitForFunction(() => window.T?.ready, null, { timeout: 60000 });
  await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
  await page.waitForSelector(".fl-node", { timeout: 60000 });
  await page.waitForSelector(".fl-pick select", { timeout: 30000 });
  await page.waitForTimeout(300);
  return page;
}
const shot = (page, name) => page.screenshot({ path: join(OUT, `${name}.png`) });
const logOf = (page, cb) => page.evaluate((cb) => window.T.log.filter((x) => x.cb === cb).map((x) => x.args), cb);
const lastSource = (page) => page.evaluate(() => window.T.source);
async function waitLog(page, cb, n = 1, timeout = 30000) {
  await page.waitForFunction(([cb, n]) => window.T.log.filter((x) => x.cb === cb).length >= n, [cb, n], { timeout });
  return (await logOf(page, cb)).at(-1);
}
/** Pans the flow so a box is on screen (the canvas clips: nothing scrolls it into view by itself). */
async function reveal(page, id) {
  await page.evaluate((id) => window.T.flow.select(id), id);
  await page.waitForTimeout(80);
}
async function hoverCard(page, selector) {
  const id = /data-id=([\w:-]+)/.exec(selector)?.[1];
  if (id) await reveal(page, id);
  await page.mouse.move(5, 5);
  await page.waitForTimeout(150);
  await page.hover(selector);
  await page.waitForSelector(".fl-card.open", { timeout: 10000 });
  await page.waitForFunction(() => !document.querySelector(".fl-card.open .fl-wait"), null, { timeout: 20000 });
}

try {
  if (!ONLY || ONLY === "view") {
    // ---------------------------------------------------------------- reading
    const page = await open();
    const boxes = await page.locator(".fl-node").count();
    check(boxes === 18, `boxes: 2 inputs, 15 steps and the folded checks (${boxes})`);
    check((await page.locator(".fl-seg").count()) > 20, "wires drawn");
    check((await page.locator(".fl-node.folded.q").count()) === 1, "the quality checks folded into one box");
    check((await page.locator(".fl-node.folded .fl-qbody .fl-badge").count()) >= 1, "with their flag counts");
    check((await page.locator(".fl-node[data-id=activities] .fl-res").count()) === 1, "a result's star");
    check((await page.locator(".fl-port.set .fl-sv").count()) > 10, "settings as fields in the boxes");
    await shot(page, "01-flow-dark");

    await hoverCard(page, ".fl-node[data-id=ratios] .fl-out");
    const rows = await page.locator(".fl-card.open table.fl-tv tbody tr").count();
    check(rows >= 3, `hovering an output shows its first rows (${rows})`);
    check(/made by .* used by/.test(await page.locator(".fl-card.open").textContent()), "who made it, who uses it");
    await shot(page, "02-hover-output");
    await hoverCard(page, ".fl-node[data-id=calibration] .fl-port[data-port=standards]");
    check((await page.locator(".fl-card.open table.fl-tv tbody tr").count()) >= 1, "hovering a port shows rows");
    await hoverCard(page, ".fl-node[data-id=standards] .fl-port.set:has-text('Highest')");
    check(/a setting/.test(await page.locator(".fl-card.open").textContent()), "hovering a setting explains it");
    await hoverCard(page, ".fl-node[data-id=figure] .fl-out");
    check((await page.locator(".fl-card.open img.fl-fig").count()) === 1,
      "a figure's card shows the picture, read like any value (value_get and its file)");
    await hoverCard(page, ".fl-node[data-id=references] .fl-port[data-port=compounds]");
    const compounds = await page.locator(".fl-card.open").textContent();
    check(/SUB: rt 3\.62/.test(compounds) && !/[{"]/.test(compounds.replace(/“|”/g, "")),
      `a table setting reads as lines, not as code (${compounds.slice(0, 80)})`);
    await page.mouse.move(5, 5);

    // the keyboard: a box's head is reached and opened with Enter; Escape closes a menu, then the inside panel
    await reveal(page, "ratios");
    await page.focus(".fl-node[data-id=ratios] .fl-nh");
    await page.keyboard.press("Enter");
    await page.waitForSelector(".fl-side.open .fl-st:text('Area ratios')", { timeout: 20000 });
    check(true, "Enter on a box's head (reached by Tab) looks inside");
    await page.click(".fl-node[data-id=ratios] .fl-more");
    await page.waitForSelector(".fl-menu button.fl-mi:focus");
    check(true, "a box's menu opens with its first choice focused (arrow keys move)");
    await page.keyboard.press("Escape");
    check((await page.locator(".fl-menu").count()) === 0 && (await page.locator(".fl-side.open").count()) === 1,
      "Escape closes the menu first");
    check(await page.evaluate(() => document.activeElement?.classList.contains("fl-more")),
      "and gives the focus back to the button that opened it");
    await page.keyboard.press("Escape");
    check((await page.locator(".fl-side.open").count()) === 0, "a second Escape closes the inside panel");
    check(await page.evaluate(() => !!document.activeElement?.closest(".fl-node[data-id=ratios]")),
      "and the focus is back on the box");
    check((await page.locator(".fl-zoom button[aria-label^=Zoom]").count()) === 2 &&
      (await page.locator(".fl-more[aria-label]").count()) > 10, "the flow's buttons have names a screen reader says");

    // the inside panel
    await reveal(page, "activities");
    await page.click(".fl-node[data-id=activities] .fl-nt");
    await page.waitForSelector(".fl-side.open .cmx-chip", { timeout: 30000 });
    const chips = await page.locator(".fl-side .cmx-chip").count();
    check(chips > 5, `inside a step: values at the lines (${chips})`);
    check((await page.locator(".fl-side .fl-rounds").textContent()).includes("round"),
      "a loop's rounds to step through");
    check((await waitLog(page, "onSelect")).node === "activities", "onSelect with the node");
    await page.waitForSelector(".fl-side .fl-fm .katex", { timeout: 10000 });
    check((await page.locator(".fl-side .fl-fm .katex").count()) >= 2,
      "formulas drawn as math with this run's numbers");
    await shot(page, "03-inside");
    await page.click(".fl-side a.fl-chip:has-text('biomass_for')");
    await page.waitForSelector(".fl-side .fl-crumbs", { timeout: 20000 });
    check((await page.locator(".fl-side .fl-crumbs a").count()) === 1,
      "a function it calls opens with a breadcrumb back");
    await page.click(".fl-side .fl-crumbs a");
    await page.waitForSelector(".fl-side .fl-st:text('Initial rates')");
    await page.click(".fl-side button:text('Only what feeds it')");
    check((await page.locator(".fl-world.fade .fl-node.keep").count()) >= 6, "only what feeds it: the rest dimmed");
    await shot(page, "04-trace");
    await page.click(".fl-side button:text('Edit this step')");
    check((await waitLog(page, "onOpen")).what === "function", "Edit this step asks the host to open the code");
    await page.click(".fl-side button[title=Close]");

    // a wire's table in full
    await reveal(page, "concentrations");
    await page.click(".fl-node[data-id=concentrations] .fl-out");
    await page.waitForSelector(".fl-side .fl-pager", { timeout: 20000 });
    check(/rows 1–34 of 34/.test(await page.locator(".fl-side .fl-pager").textContent()), "a wire's whole table");
    await page.click(".fl-side button[title=Close]");

    // the picker: the experiment and its runs
    const options = await page.locator(".fl-pick select").nth(1).locator("option").allTextContents();
    check(options.length >= 2 && options[0].startsWith("Latest"), `the runs, newest first (${options.length})`);
    const older = runs.at(-1).id;
    await page.locator(".fl-pick select").nth(1).selectOption(older);
    const picked = await waitLog(page, "onPick");
    check(picked.run === older, "picking a run calls onPick");
    await page.waitForFunction((id) => window.T.flow.graph?.run?.id === id, older, { timeout: 30000 });
    check(true, "and the flow shows that run's data");
    await page.locator(".fl-pick select").nth(1).selectOption("");
    await page.waitForFunction((id) => window.T.flow.graph?.run?.id !== id, older, { timeout: 30000 });
    const cached = await page.evaluate(() => window.T.flow.graph.nodes.filter((n) => n.status === "cached" &&
      !n.quality).length);
    check((await page.locator(".fl-node .fl-badge:text('reused')").count()) === cached,
      `steps reused from before are marked (${cached})`);

    // folding
    await page.click(".fl-bar button:has-text('Unfold the checks')");
    check((await page.locator(".fl-node").count()) === 27, "unfolded: every check is its own box");
    await shot(page, "05-unfolded");
    await page.click(".fl-bar button:has-text('Fold the checks')");
    check((await page.locator(".fl-node").count()) === 18, "folded again");

    // a setting edited in place
    await reveal(page, "references");
    await page.click(".fl-node[data-id=references] .fl-port.set .fl-sv >> nth=1");
    await page.keyboard.press("Control+A");
    await page.keyboard.type("0.02");
    await page.keyboard.press("Enter");
    const set = await waitLog(page, "onSetting");
    check(set.id === "rt_tolerance_min" && set.value === 0.02,
      `a setting edit calls onSetting (${JSON.stringify(set)})`);

    // keeps the view and the selection across update()
    await reveal(page, "concentrations");
    await page.click(".fl-node[data-id=concentrations] .fl-nt");
    const before = await page.evaluate(() => document.querySelector(".fl-world").style.transform);
    await page.evaluate(() => window.T.flow.update({}));
    await page.waitForTimeout(500);
    check((await page.locator(".fl-node.sel").getAttribute("data-id")) === "concentrations",
      "update keeps the selection");
    check((await page.evaluate(() => document.querySelector(".fl-world").style.transform)) === before, "and the view");
    await page.click(".fl-side button[title=Close]");
    await page.click(".fl-bar button:text('Python')");
    await page.waitForSelector(".fl-py.open .fl-pl");
    await page.click(".fl-pl:has-text('fit_calibration')");
    check((await page.locator(".fl-node.sel").getAttribute("data-id")) === "calibration",
      "a Python line selects its box");
    check((await page.locator(".fl-pl.sel").count()) >= 1, "and the box's line is marked");
    await page.evaluate(() => window.T.flow.setRunning({ step: "fit_calibration" }));
    check((await page.locator(".fl-node.running").count()) === 1, "setRunning lights up the step");
    await shot(page, "06-python-running");
    await page.evaluate(() => window.T.flow.setRunning(null));
    await page.click(".fl-bar button:text('Python')");
    await reveal(page, "activities");
    await page.click(".fl-node[data-id=activities] .fl-res");
    const res = (await logOf(page, "onOpen")).at(-1);
    check(res.what === "results" && res.table === "activities", "a result's star opens the results");
    await reveal(page, "samples");
    await page.click(".fl-node[data-id=samples] .fl-port.set .fl-sv");
    const form = (await logOf(page, "onOpen")).at(-1);
    check(form.what === "settings" && form.setting === "sample_patterns", "a list setting opens in the host's form");
    await page.close();

    // a step that fails: its box is red with the plain-words message
    await tool("experiment_update", { experiment: EXP, params: { internal_standard: "nonane" } });
    const bad = await tool("run", { experiment: EXP, wait_s: 55 });
    check(bad.status === "failed", `a run with a wrong internal standard fails (${bad.status})`);
    const failed = await open();
    const red = failed.locator(".fl-node.st-failed");
    check((await red.count()) >= 1, "the failed step's box is marked");
    const id = await red.first().getAttribute("data-id");
    await reveal(failed, id);
    check((await failed.locator(`.fl-node[data-id=${id}] .fl-err`).textContent()).length > 10, "with what went wrong");
    check(/stopped/.test(await failed.locator(".fl-note").textContent()), "and the bar says the calculation stopped");
    await shot(failed, "06b-failed");
    await failed.close();
    await tool("experiment_update", { experiment: EXP, params: { internal_standard: null } });
    await tool("run", { experiment: EXP, wait_s: 55 });

    const light = await open({ theme: "light" });
    await shot(light, "07-flow-light");
    await reveal(light, "peaks");
    await light.click(".fl-node[data-id=peaks] .fl-nt");
    await light.waitForSelector(".fl-side.open .cmx-chip", { timeout: 30000 });
    await shot(light, "08-inside-light");
    await light.close();

    const narrow = await open({ width: 900, height: 700 });
    await shot(narrow, "09-900px");
    await narrow.close();
    const phone = await open({ width: 400, height: 760 });
    check(await phone.locator(".fl-bar .fl-legend-bar").isVisible() && !(await phone.locator(".fl-hint").isVisible()),
      "on a narrow flow the colours' key sits in the bar, not over the boxes");
    await shot(phone, "10-phone");
    await phone.close();
  }

  if (!ONLY || ONLY === "edit") {
    // ---------------------------------------------------------------- editing the GC method
    const page = await open({ params: "&editable=1" });
    await reveal(page, "ratios");
    await page.hover(".fl-node[data-id=ratios]");
    await reveal(page, "ratios");
    await page.click(".fl-node[data-id=ratios] .fl-add");
    await page.waitForSelector(".fl-menu .fl-mi:has-text('Pick rows')");
    check((await page.locator(".fl-menu .fl-mi").count()) >= 8, "the add menu: ready-made steps and the method's own");
    await shot(page, "11-add-menu");
    await page.click(".fl-menu .fl-mi:has-text('Pick rows')");
    await page.waitForSelector(".fl-form select");
    const colOptions = await page.locator(".fl-form select").first().locator("option").allTextContents();
    check(colOptions.includes("role"), "the block's column picker lists the incoming table's columns");
    await page.locator(".fl-form select").first().selectOption("role");
    await page.fill(".fl-form label:has-text('Equals') input", "sample");
    await page.fill(".fl-form label:has-text('Name of what it makes') input", "sample_ratios");
    await shot(page, "12-block-form");
    await page.click(".fl-form button:text('Add the step')");
    const added = await waitLog(page, "onSource");
    let src = await lastSource(page);
    if (process.env.DEBUG) console.log(src.split(/\n/).filter((l) => /pick_rows|^from toolbox/.test(l)).join(" | "));
    check(/sample_ratios = pick_rows\(ratios, "role", (equals=)?"sample"\)/.test(src),
      "adding a block writes its line");
    check(/from toolbox import \(?[^)]*\bpick_rows\b/.test(src), "and imports it");
    check(added.added.includes("sample_ratios"), "the new box is there");
    await page.waitForSelector(".fl-node[data-id=sample_ratios]");
    await shot(page, "13-added");

    // connect peaks to the new step's table input
    const fromDot = page.locator(".fl-node[data-id=peaks] .fl-out .fl-dot");
    const to = page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=table]");
    await page.locator(".fl-node[data-id=peaks]").scrollIntoViewIfNeeded();
    const a = await fromDot.boundingBox(), b = await to.boundingBox();
    await page.mouse.move(a.x + 4, a.y + 4);
    await page.mouse.down();
    await page.mouse.move(a.x + 40, a.y + 20, { steps: 4 });
    await page.mouse.move(b.x + 30, b.y + b.height / 2, { steps: 8 });
    check((await page.locator(".fl-port.drop-ok").count()) > 3, "while dragging, inputs that fit light up");
    await shot(page, "14-connecting");
    await page.mouse.up();
    await waitLog(page, "onSource", 2);
    src = await lastSource(page);
    if (process.env.DEBUG) console.log(src.split(/\n/).filter((l) => /pick_rows/.test(l)).join(" | "));
    check(/sample_ratios = pick_rows\(peaks, "role", (equals=)?"sample"\)/.test(src),
      "dragging to an input connects it");

    // its inputs in the box are the form's fields: a column picked from the incoming table, a text without quotes
    await reveal(page, "sample_ratios");
    const colField = page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=column] select");
    check((await colField.count()) === 1 && (await colField.inputValue()) === "role" &&
      (await colField.locator("option").allTextContents()).includes("compound"),
    "a block's column is a list of the incoming table's columns");
    const equals = page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=equals] input");
    const shown = await equals.inputValue();
    check(shown === "sample", `a block's text is shown without quotes (${shown})`);
    check((await page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=equals] .fl-sl").textContent()) ===
      "Equals", "and is labelled as in the form");
    await equals.fill("blank");
    await equals.press("Enter");
    await waitLog(page, "onSource", 3);
    check(/sample_ratios = pick_rows\(peaks, "role", (equals=)?"blank"\)/.test(await lastSource(page)),
      "changing it in the box writes the code (connect with a value)");
    await reveal(page, "sample_ratios");
    await page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=column] select").selectOption("compound");
    await waitLog(page, "onSource", 4);
    check(/sample_ratios = pick_rows\(peaks, "compound", (equals=)?"blank"\)/.test(await lastSource(page)),
      "and so does picking another column");
    await reveal(page, "sample_ratios");
    await page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=column] select").selectOption("role");
    await waitLog(page, "onSource", 5);
    await reveal(page, "sample_ratios");
    await page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=equals] input").fill("sample");
    await page.locator(".fl-node[data-id=sample_ratios] .fl-port[data-port=equals] input").press("Enter");
    await waitLog(page, "onSource", 6);

    // rename its output
    await reveal(page, "sample_ratios");
    await page.hover(".fl-node[data-id=sample_ratios]");
    await reveal(page, "sample_ratios");
    await page.click(".fl-node[data-id=sample_ratios] .fl-more");
    await page.click(".fl-menu .fl-mi:has-text('Rename its result')");
    await page.fill(".fl-form input", "sample_peaks");
    await page.click(".fl-form button:text('Rename')");
    await waitLog(page, "onSource", 7);
    check(/sample_peaks = pick_rows\(peaks/.test(await lastSource(page)), "renaming the output renames the variable");

    // a mistake is said where it happened, and nothing is lost
    await reveal(page, "calibration");
    await page.hover(".fl-node[data-id=calibration]");
    await reveal(page, "calibration");
    await page.click(".fl-node[data-id=calibration] .fl-more");
    await page.click(".fl-menu .fl-mi:has-text('Remove this step')");
    await page.waitForSelector(".fl-bubble", { timeout: 15000 });
    check(/used by|result/.test(await page.locator(".fl-bubble").textContent()), "a refused change says why, in words");
    await shot(page, "15-refused");

    // remove the new step with the Delete key (the same as the menu's Remove this step)
    await reveal(page, "sample_peaks");
    await page.click(".fl-node[data-id=sample_peaks] .fl-nt");
    await page.keyboard.press("Escape"); // the inside panel closes; the box stays selected
    await page.keyboard.press("Delete");
    await waitLog(page, "onSource", 8);
    check(!/pick_rows/.test(await lastSource(page)), "removing the step removes its line and its import");
    check((await page.locator(".fl-node[data-id=sample_peaks]").count()) === 0, "and its box");

    // group three steps that follow each other, fold the group, unfold it, ungroup
    for (const id of ["standards", "calibration", "calibration_points"]) {
      await reveal(page, id);
      await page.click(`.fl-node[data-id=${id}] .fl-nt`, { modifiers: ["Shift"] });
    }
    await page.click(".fl-bar button:has-text('Group 3 steps')");
    await page.fill(".fl-form input", "Calibration");
    await page.click(".fl-form button:text('Group them')");
    await waitLog(page, "onSource", 9);
    check(/# --- Calibration/.test(await lastSource(page)), "grouping writes the group's comment line");
    await page.waitForSelector(".fl-group .fl-gt:text('Calibration')");
    await shot(page, "16-group");
    await page.click(".fl-group button:text('fold')");
    await page.waitForSelector(".fl-node.folded[data-id='G:group-calibration']");
    check((await page.locator(".fl-node").count()) === 16, "a folded group is one box");
    await reveal(page, "G:group-calibration");
    await shot(page, "17-group-folded");
    await page.click(".fl-node.folded[data-id='G:group-calibration'] .fl-nh");
    await page.waitForSelector(".fl-group button:text('ungroup')");
    await page.click(".fl-group button:text('ungroup')");
    await waitLog(page, "onSource", 10);
    check(!/# --- Calibration/.test(await lastSource(page)), "ungrouping takes the line away");

    // a port made a setting, then given a value written in place
    await reveal(page, "concentrations");
    await page.click(".fl-node[data-id=concentrations] .fl-port[data-port=ratios]");
    await page.click(".fl-menu .fl-mi:has-text('Show the data')");
    await page.waitForSelector(".fl-side .fl-pager", { timeout: 20000 });
    check(true, "a port's menu shows its data");
    await page.click(".fl-side button[title=Close]");
    await page.close();
  }

  if (!ONLY || ONLY === "new") {
    // ---------------------------------------------------------------- a new method from nothing
    const page = await open({ params: "&editable=1" });
    const fresh = await tool("flow_new", { name: "Plate growth", description: "Growth of cultures read on a plate." });
    await page.evaluate((source) => window.T.mount({ source, method: null, experiment: null, editable: true }),
      fresh.source);
    await page.waitForSelector(".fl-empty.open button:text('Add a raw file')", { timeout: 30000 });
    await shot(page, "16-new-empty");
    await page.click(".fl-empty button:text('Add a raw file')");
    await page.fill(".fl-form label:has-text('What it holds') input", "plate reads");
    await page.click(".fl-form button:text('Add it')");
    await waitLog(page, "onSource");
    await page.waitForSelector(".fl-node[data-id=plate_reads]");
    check((await page.locator(".fl-node.src").count()) === 1, "a raw file and its reader");
    // a ready-made step on what the reader gives
    await reveal(page, "plate_reads");
    await page.hover(".fl-node[data-id=plate_reads]");
    await reveal(page, "plate_reads");
    await page.click(".fl-node[data-id=plate_reads] .fl-add");
    await page.click(".fl-menu .fl-mi:has-text('Summarize by group')");
    await page.click(".fl-form button:text('Add the step')");
    await page.waitForFunction(() =>
      /Fill in “Values”/.test(document.querySelector(".fl-form .fl-fmsg")?.textContent));
    check(true, "a form says what it still needs, in its own words, and stays open");
    await page.fill(".fl-form label:has-text('Values') input", "od600");
    await page.locator(".fl-form details summary").click();
    await page.fill(".fl-form label:has-text('For each') input", "strain");
    await page.fill(".fl-form label:has-text('Name of what it makes') input", "summary");
    await page.click(".fl-form button:text('Add the step')");
    await waitLog(page, "onSource", 2);
    await page.waitForSelector(".fl-node[data-id=summary]");
    await reveal(page, "summary");
    await page.click(".fl-node[data-id=summary] .fl-more");
    await page.click(".fl-menu .fl-mi:has-text('Make it a result')");
    await page.click(".fl-form button:text('Make it a result')");
    const last = await waitLog(page, "onSource", 3);
    check(last.ok, "the new method passes the checker");
    check((await page.locator(".fl-node[data-id=summary] .fl-port[data-port=values] input").inputValue()) === "od600",
      "with no run yet, a block's column is a text field without quotes");
    // `by` becomes a setting of the method
    await page.click(".fl-node[data-id=summary] .fl-port[data-port=by] .fl-sl");
    await page.click(".fl-menu .fl-mi:has-text('Make it a setting')");
    await page.fill(".fl-form label:has-text('Name in the code') input", "group_by");
    await page.fill(".fl-form label:has-text('What it is') input", "Group the cultures by");
    await page.click(".fl-form button:text('Make it a setting')");
    await waitLog(page, "onSource", 4);
    check(/def analysis\([^)]*group_by="strain"/.test(await lastSource(page)),
      "a port made a setting: in the signature");
    check(/summarize\(plate_reads, "od600", by=group_by/.test(await lastSource(page)), "and connected to the port");
    await page.waitForSelector(".fl-node[data-id=summary] .fl-port.set:has-text('Group the cultures by')");
    // a step described for Claude
    await page.click(".fl-node[data-id=summary] .fl-add");
    await page.click(".fl-menu .fl-mi:has-text('Describe it, Claude writes it')");
    await page.fill(".fl-form textarea", "Divide each mean by the wild type's mean.");
    await page.click(".fl-form button:text('Ask Claude')");
    const claude = await waitLog(page, "onOpen");
    check(claude.what === "claude" && claude.wire === "summary" && claude.sentence.includes("wild type"),
      "Describe it: the host is asked to have Claude write it, with the wire and the sentence");
    // a step of her own
    await page.click(".fl-node[data-id=summary] .fl-add");
    await page.click(".fl-menu .fl-mi:has-text('A new step of your own')");
    await page.fill(".fl-form label:has-text('Its name') input", "relative_to_wild_type");
    await page.fill(".fl-form textarea", "Each strain's mean divided by the wild type's.");
    await page.click(".fl-form button:text('Make it')");
    await waitLog(page, "onSource", 5);
    const own = await lastSource(page);
    check(/def relative_to_wild_type\(summary\):/.test(own) && /relative_to_wild_type\(summary\)/.test(own),
      "a new step of her own: its function and its line");
    check((await logOf(page, "onOpen")).at(-1).what === "function", "and the editor is asked to open it");
    await page.click(".fl-node:has(.fl-nt:text('Relative to wild type')) .fl-more");
    await page.click(".fl-menu .fl-mi:has-text('Remove this step')");
    await waitLog(page, "onSource", 6);
    await page.waitForSelector(".fl-node[data-id=summary] .fl-res");
    await shot(page, "17-new-built");
    const source = await lastSource(page);
    // it really runs: save it, give it a file, calculate
    const csv = join(OUT, "plate.csv");
    writeFileSync(csv, "strain,well,od600\nWT,A1,0.51\nWT,A2,0.49\nmut,B1,0.30\nmut,B2,0.34\n");
    const id = `plate-growth-${Date.now().toString(36)}`;
    const m = await tool("method_create", { method: id, name: "Plate growth", source });
    const e = await tool("experiment_create", { title: "Plate test", method: m.id ?? m.method,
      files: { plate_reads_file: csv } });
    const r = await tool("run", { experiment: e.experiment ?? e.id, wait_s: 55 });
    check(r.status === "done",
      `the method built in the flow runs (${r.status}${r.error ? ": " + r.error.message : ""})`);
    await page.evaluate(([method, experiment]) => window.T.mount({ method, experiment, editable: true }),
      [m.id ?? m.method, e.experiment ?? e.id]);
    await page.waitForSelector(".fl-node[data-id=summary] .fl-os:text('2 × 4')", { timeout: 30000 }).catch(() => {});
    check((await page.locator(".fl-node[data-id=summary] .fl-os").textContent()).includes("×"), "and shows its data");
    await shot(page, "18-new-run");
    // on an experiment's screen (not editable) the same inputs read as plain values
    await page.evaluate(([method, experiment]) => window.T.mount({ method, experiment, editable: false }),
      [m.id ?? m.method, e.experiment ?? e.id]);
    await page.waitForSelector(".fl-node[data-id=summary] .fl-port[data-port=values] .fl-sv", { timeout: 30000 });
    const plain = await page.locator(".fl-node[data-id=summary] .fl-port[data-port=values] .fl-sv").textContent();
    check(plain === "od600", `read only, a block's value has no quotes (${plain})`);
    await page.close();
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

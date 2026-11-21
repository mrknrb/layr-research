// Moved from layr's plugins/dev (task 132): it needs Playwright, and the paths it names as plugins/research
// are this repository's folder. Run it from a layr checkout's plugins/dev with this folder linked.
// Test of the Research plugin's code editor (plugins/research/panels/editor.js, CodeMirror 6) in headless Chromium.
// Needs no running core: it serves plugins/research/panels itself, with the Content-Security-Policy layr sends for
// the plugin's pages (core/layr_core/src/server.rs › content_security_policy), and opens dev-editor.html.
//
// From plugins/dev:  node research-editor.test.mjs [--out <dir for screenshots>]
import { chromium } from "playwright";
import http from "node:http";
import { mkdirSync, readFileSync, existsSync, statSync } from "node:fs";
import { join, extname, dirname, resolve, sep } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const PANELS = resolve(here, "..", "research", "panels");
const LAYR_CSS = resolve(here, "..", "..", "core", "layr_core", "assets", "layr.css");
const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const OUT = arg("out", join(tmpdir(), "layr-research-editor-shots"));
mkdirSync(OUT, { recursive: true });

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
};

const server = http.createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  const file = path === "/_layr/layr.css" ? LAYR_CSS : resolve(PANELS, "." + path);
  if ((file !== LAYR_CSS && !file.startsWith(PANELS + sep)) || !existsSync(file) || !statSync(file).isFile()) {
    res.writeHead(404).end("not found");
    return;
  }
  const host = req.headers.host;
  // As layr's server builds it for a plugin without the network, eval or wasm permissions.
  const csp =
    `default-src 'self' 'unsafe-inline' data: blob:; connect-src 'self' ws://${host} blob: data:; ` +
    "img-src 'self' data: blob:; media-src 'self' data: blob:; frame-src 'self' blob:; form-action 'self'";
  res.writeHead(200, {
    "content-type": TYPES[extname(file)] || "application/octet-stream",
    "content-security-policy": csp,
    "cache-control": "no-store",
  });
  res.end(readFileSync(file));
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const URL_ = `http://127.0.0.1:${server.address().port}/dev-editor.html`;

const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
  if (!ok) failures.push(what);
};
const problems = [];
const browser = await chromium.launch();

async function open(options = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "dark", ...options });
  const page = await context.newPage();
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`),
    );
  });
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
  await page.goto(URL_);
  await page.waitForFunction(() => window.T?.ready, null, { timeout: 15000 });
  return page;
}
const shot = (page, name, opts) => page.screenshot({ path: join(OUT, `${name}.png`), ...opts });
const value = (page) => page.evaluate(() => window.T.ed.getValue());
/** Puts the cursor at the end of a line (1-based) of the main editor. */
const cursorAtEnd = (page, line) =>
  page.evaluate((n) => {
    const v = window.T.ed.view;
    const l = v.state.doc.line(n);
    v.dispatch({ selection: { anchor: l.to } });
    v.focus();
  }, line);
const lineText = (page, n) => page.evaluate((k) => window.T.ed.view.state.doc.line(k).text, n);

try {
  const page = await open();
  check((await page.locator("#ed .cm-editor").count()) === 1, "the editor is drawn");
  check(
    (await page.locator("#ed .tok-kw").count()) > 5 && (await page.locator("#ed .tok-def").count()) >= 3,
    "Python is highlighted (keywords, function names)",
  );
  check((await page.locator("#ed .cm-lineNumbers .cm-gutterElement").count()) > 10, "line numbers");
  await shot(page, "01-dark");

  // ---- typing, undo, Tab, save
  const before = await value(page);
  await cursorAtEnd(page, 29);
  await page.keyboard.press("Enter");
  await page.keyboard.type("x = 1");
  check((await value(page)).includes("\n    x = 1"), "typing (Enter keeps the indent)");
  check((await page.evaluate(() => window.T.changes)) > 0, "onChange is called");
  check((await page.evaluate(() => window.T.cursorAt.line)) === 30, "onCursor gives the line");
  await page.keyboard.press("Control+z");
  await page.waitForTimeout(50);
  for (let i = 0; i < 3 && (await value(page)) !== before; i++) await page.keyboard.press("Control+z");
  check((await value(page)) === before, "undo brings the text back");
  await cursorAtEnd(page, 3);
  await page.keyboard.press("Tab");
  check((await lineText(page, 3)) === "    ", "Tab on an empty line inserts 4 spaces");
  await page.keyboard.press("Shift+Tab");
  check((await lineText(page, 3)) === "", "Shift+Tab takes them away");
  await page.evaluate(() => {
    const v = window.T.ed.view;
    v.dispatch({ selection: { anchor: v.state.doc.line(17).from, head: v.state.doc.line(18).to } });
  });
  await page.keyboard.press("Tab");
  check(
    (await lineText(page, 17)).startsWith("        lines") && (await lineText(page, 18)).startsWith("        for"),
    "Tab with a selection indents the lines",
  );
  await page.keyboard.press("Shift+Tab");
  check((await lineText(page, 17)) === "    lines = {}", "Shift+Tab with a selection unindents");
  await page.keyboard.press("Control+/");
  check((await lineText(page, 17)).trimStart().startsWith("#"), "Ctrl+/ comments the lines");
  await page.keyboard.press("Control+/");
  check((await lineText(page, 17)) === "    lines = {}", "Ctrl+/ again uncomments");
  await page.keyboard.press("Control+s");
  check((await page.evaluate(() => window.T.saved)) === 1, "Ctrl+S calls onSave");

  // ---- completion and signature
  await cursorAtEnd(page, 29);
  await page.keyboard.press("Enter");
  await page.keyboard.type("pi");
  await page.waitForSelector(".cm-tooltip-autocomplete li", { timeout: 4000 });
  check(
    await page.locator(".cm-tooltip-autocomplete").getByText("pick_rows").isVisible(),
    "completion from the page's complete() while typing",
  );
  await shot(page, "02-completion");
  await page.keyboard.press("Enter");
  check((await lineText(page, 30)).trim() === "pick_rows", "Enter takes the completion");
  await page.keyboard.type("(peaks.");
  await page.waitForSelector(".cm-tooltip-autocomplete li:has-text('retention_time_min')", { timeout: 4000 });
  check(true, "after a dot: the columns");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Control+a");
  await page.evaluate(() =>
    window.T.ed.setValue(window.T.ed.getValue().split("\n").slice(0, 29).join("\n") + "\n", { keepHistory: true }),
  );
  await cursorAtEnd(page, 30);
  await page.keyboard.type("    fit_line(1, ");
  await page.waitForSelector(".cmx-signature", { timeout: 4000 });
  check(
    (await page.locator(".cmx-signature-label b").textContent()) === "y",
    "the call's signature, the parameter being typed bold",
  );
  await shot(page, "03-signature");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(50);
  check((await page.locator(".cmx-signature").count()) === 0, "Escape closes the signature");
  await page.evaluate(() =>
    window.T.ed.setValue(window.T.ed.getValue().split("\n").slice(0, 29).join("\n") + "\n", { keepHistory: true }),
  );
  check((await value(page)) === before, "setValue with keepHistory puts the text back");

  // ---- hover
  const fit = page
    .locator("#ed .cm-line", { hasText: "line = fit_line(" })
    .locator("span.tok-fn", { hasText: "fit_line" });
  await fit.hover();
  await page.waitForSelector(".hover-test", { timeout: 4000 });
  check(
    await page.locator(".cm-tooltip-hover .hover-test .cmx-highlight").isVisible(),
    "hover shows hover()'s element",
  );
  await shot(page, "04-hover");
  await page.mouse.move(5, 5);
  await page.waitForTimeout(300);

  // ---- go to definition, rename
  await fit.click({ modifiers: ["Control"] });
  const def = await page.evaluate(() => window.T.definition);
  check(
    def?.line === 19 &&
      def.source === before &&
      (await page.evaluate(() => window.T.ed.view.state.selection.ranges.length)) === 1,
    `Ctrl-click on a name calls onDefinition with its line (${def?.line}:${def?.column})`,
  );
  await page.evaluate(() => (window.T.definition = null));
  await cursorAtEnd(page, 26);
  await page.keyboard.press("F12");
  check((await page.evaluate(() => window.T.definition?.line)) === 26, "F12 calls onDefinition at the cursor");
  await page.keyboard.press("F2");
  check((await page.evaluate(() => window.T.rename?.line)) === 26, "F2 calls onRename at the cursor");
  await page.evaluate(() => {
    const v = window.T.ed.view;
    const l = v.state.doc.line(26);
    v.dispatch({ selection: { anchor: l.from + 4, head: l.from + 9 } });
  });
  check((await page.evaluate(() => window.T.ed.selection())) === "peaks", "selection() gives the selected text");

  // ---- diagnostics
  check(
    (await page.locator("#ed .cm-lint-marker-error").count()) === 1 &&
      (await page.locator("#ed .cm-lint-marker-warning").count()) === 1,
    "diagnostics in the gutter (an error, a warning)",
  );
  check((await page.locator("#ed .cm-lintRange-error").count()) >= 1, "the error's line is underlined");

  // ---- values at line ends
  check((await page.locator("#ed .cmx-chip").count()) === 7, "7 value chips");
  check((await page.locator("#ed .cmx-chip.t-table .cmx-dot").count()) === 3, "table chips have their dot");
  const std = page.locator("#ed .cmx-chip", { hasText: "std = 27 × 2" });
  await std.hover();

  await page.waitForSelector(".cmx-card.open table", { timeout: 3000 });
  check(
    (await page.locator(".cmx-card.open .cmx-card-head").textContent()).includes("internal standard"),
    "a chip's hover card shows card()",
  );
  await shot(page, "05-value-card");
  const cardBox = await page.locator(".cmx-card.open").boundingBox();
  await page.mouse.move(cardBox.x + 10, cardBox.y + 10, { steps: 4 });
  await page.waitForTimeout(400);
  check(await page.locator(".cmx-card.open").isVisible(), "the card stays open while the pointer is on it");
  await page.mouse.move(cardBox.x + cardBox.width + 200, cardBox.y + cardBox.height + 200);
  await page.waitForTimeout(400);
  check((await page.locator(".cmx-card.open").count()) === 0, "and closes when the pointer leaves");
  await page.locator("#ed .cmx-chip", { hasText: "out = 54 × 8" }).click();
  check((await page.evaluate(() => window.T.clicked)) === 1, "a click on a chip calls onClick");
  await cursorAtEnd(page, 11);
  await page.keyboard.type(" * 2");
  check((await page.locator("#ed .cmx-values.stale").count()) === 1, "an edited line's chips grey out");
  await page.keyboard.press("Control+z");
  check((await page.locator("#ed .cmx-values.stale").count()) === 0, "undo makes them current again");
  await cursorAtEnd(page, 3);
  await page.keyboard.press("Enter");
  check(
    (await page.locator("#ed .cmx-chip").count()) === 7 && (await page.locator("#ed .cmx-values.stale").count()) === 0,
    "a new line above keeps the chips with their lines",
  );
  await page.keyboard.press("Control+z");

  // ---- folds
  const content = () => page.locator("#ed .cm-content").textContent();
  check(
    (await page.locator("#ed .cmx-guard").count()) === 1 && !(await content()).includes("raise ValueError"),
    "a check starts folded as a pill",
  );
  await shot(page, "06-fold", { clip: { x: 0, y: 40, width: 1280, height: 400 } });
  await page.locator("#ed .cmx-guard").click();
  check(
    (await content()).includes("raise ValueError") && (await page.locator("#ed .cmx-guard").count()) === 0,
    "one click unfolds it",
  );
  check(
    (await page.evaluate(() => window.T.ed.toggleFolds())) === false &&
      (await page.locator("#ed .cmx-guard").count()) === 1,
    "toggleFolds folds them all again",
  );

  // ---- a block under a line
  check(await page.locator("#ed .cmx-below .below-test table").isVisible(), "a block under a line");
  await page.evaluate(() => window.T.ed.reveal(28));
  await page.waitForTimeout(200);
  await shot(page, "07-below");

  // ---- merge view, side by side
  check((await page.evaluate(() => window.T.merge.mode())) === "side", "a wide merge view is side by side");
  check((await page.evaluate(() => window.T.merge.counts().left)) === 3, "the draft has 3 changed chunks");
  await page.locator("#merge").scrollIntoViewIfNeeded();
  await shot(page, "08-merge-side", { clip: await page.locator("#merge").boundingBox() });
  await page.locator("#merge [data-act=accept]:visible").first().click();
  await page.waitForTimeout(50);
  await page.locator("#merge [data-act=reject]:visible").first().click();
  await page.waitForTimeout(50);
  const counts = await page.evaluate(() => window.T.merge.counts());
  check(
    counts.accepted === 1 && counts.rejected === 1 && counts.left === 1,
    `accept and reject per chunk (${JSON.stringify(counts)})`,
  );
  const result = await page.evaluate(() => window.T.merge.getResult());
  check(
    result.includes('out = out[out["ratio"] > 0]') &&
      result.includes('groupby("compound")') &&
      result.includes("read the peaks, ratio them"),
    "getResult: the accepted change, the old text for the rejected one, the undecided one as proposed",
  );
  check(
    JSON.stringify(await page.evaluate(() => window.T.mergeCounts)) === JSON.stringify(counts),
    "onChange gets the counts",
  );

  // ---- merge view, unified (narrow)
  await page.evaluate(() => {
    const box = document.createElement("div");
    box.id = "narrow";
    box.style.cssText = "width:520px;height:320px;margin-top:12px";
    document.body.append(box);
    window.T.narrow = window.T.createMerge(box, { before: window.T.ed.getValue(), after: window.T.AFTER });
  });
  check((await page.evaluate(() => window.T.narrow.mode())) === "unified", "a narrow merge view is unified");
  await page.locator("#narrow").scrollIntoViewIfNeeded();
  await shot(page, "09-merge-unified", { clip: await page.locator("#narrow").boundingBox() });
  await page.locator("#narrow .cmx-chunk-reject").first().dispatchEvent("mousedown");
  await page.locator("#narrow .cmx-chunk-accept").first().dispatchEvent("mousedown");
  const n = await page.evaluate(() => window.T.narrow.counts());
  check(n.accepted === 1 && n.rejected === 1 && n.left === 1, `unified accept / reject (${JSON.stringify(n)})`);
  const nr = await page.evaluate(() => window.T.narrow.getResult());
  check(!nr.includes('out = out[out["ratio"] > 0]') && nr.includes("groupby(compound_column)"), "unified getResult");
  await page.evaluate(() => window.T.narrow.acceptAll());
  check((await page.evaluate(() => window.T.narrow.counts().left)) === 0, "acceptAll");

  // ---- highlight()
  const snippet = page.locator("#snippet .cmx-highlight");
  check(
    (await snippet.locator(".tok-def").count()) === 1 &&
      (await snippet.locator(".cmx-hl-ln").first().textContent()) === "5",
    "highlight(): colours and line numbers from firstLine",
  );

  // ---- light
  const bg = () => page.evaluate(() => getComputedStyle(document.querySelector("#ed .cm-editor")).backgroundColor);
  const darkBg = await bg();
  await page.click("#theme");
  await page.waitForTimeout(100);
  check((await bg()) !== darkBg, `the editor follows the light theme without a reload (${darkBg} → ${await bg()})`);
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot(page, "10-light");
  await std.hover();
  await page.waitForSelector(".cmx-card.open table", { timeout: 3000 });
  await shot(page, "11-light-card");
  await page.mouse.move(5, 5);
  await page.locator("#merge").scrollIntoViewIfNeeded();
  await shot(page, "12-light-merge", { clip: await page.locator("#merge").boundingBox() });

  const csp = await page.evaluate(() => window.__csp);
  check(csp.length === 0, `no CSP violations${csp.length ? ": " + csp.join(", ") : ""}`);
  await page.context().close();

  // ---- a phone
  const phone = await open({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    colorScheme: "light",
  });
  check(
    await phone.evaluate(() => document.documentElement.scrollWidth <= 391),
    "nothing wider than a 390-pixel screen",
  );
  const scroller = await phone.evaluate(() => {
    const s = document.querySelector("#ed .cm-scroller");
    return { w: s.scrollWidth > s.clientWidth, h: s.scrollHeight > s.clientHeight };
  });
  check(scroller.w && scroller.h, "the code scrolls inside the editor both ways");
  const chip = await phone.locator("#ed .cmx-chip", { hasText: "std" }).boundingBox();
  const edBox = await phone.locator("#ed").boundingBox();
  check(
    chip && chip.x + chip.width <= edBox.x + edBox.width + 1,
    "a long line's chip stays inside the editor's right edge",
  );
  await phone.screenshot({ path: join(OUT, "13-phone.png") });
  await phone.locator("#ed .cmx-chip", { hasText: "std" }).tap();
  await phone.waitForSelector(".cmx-card.open table", { timeout: 3000 });
  check(true, "a tap on a chip opens its card");
  await phone.screenshot({ path: join(OUT, "14-phone-card.png") });
  await phone.evaluate(() => (document.querySelector("#ed .cm-scroller").scrollLeft = 400));
  await phone.waitForTimeout(100);
  await phone.screenshot({ path: join(OUT, "15-phone-scrolled.png") });
  check((await phone.evaluate(() => window.T.merge.mode())) === "unified", "the draft review is unified on a phone");
  await phone.locator("#merge").scrollIntoViewIfNeeded();
  await phone.screenshot({ path: join(OUT, "16-phone-merge.png") });
  const csp2 = await phone.evaluate(() => window.__csp);
  check(csp2.length === 0, "no CSP violations on the phone");
} catch (err) {
  failures.push(String(err?.stack || err));
  console.log(`FAIL ${err?.stack || err}`);
} finally {
  await browser.close();
  server.close();
}
check(problems.length === 0, `no page or console errors${problems.length ? ": " + problems.join(" | ") : ""}`);
console.log(`\nscreenshots: ${OUT}`);
if (failures.length) {
  console.log(`${failures.length} failed`);
  process.exit(1);
}
console.log("all passed");

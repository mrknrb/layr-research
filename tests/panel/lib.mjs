// Helpers for the panel test scripts: start the harness (tests/panel/harness.py, a stand-in for layr's core),
// open a panel in Playwright's Chromium, call the plugin's tools, read what the panel asked layr to do, answer its
// menus and dialogs, take screenshots, and collect page errors. See README.md.
//
//   import { bench } from "./lib.mjs";
//   await bench("my test", async ({ h, open, shot, until, errors }) => { ... });
import { spawn } from "node:child_process";
import { mkdirSync, createWriteStream, readdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const PLUGIN = resolve(HERE, "..", "..");
export const SHOTS = resolve(process.env.SHOTS ?? join(HERE, "out"));
const SCRATCH = join(process.env.APPDATA ?? "", "layr/plugin-data/claude/work/w_a55w88blrq");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Playwright, from outside the plugin (PWTOOLS: a folder whose node_modules has it). */
export function playwright() {
  const req = createRequire(join(process.env.PWTOOLS ?? "C:/Users/mrknr/pwtools", "package.json"));
  return req("playwright");
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

// Every process this script started, by pid: ended at the very end even when the script fails or is interrupted.
const started = new Set();
function killStarted() {
  for (const pid of started) {
    try {
      process.kill(pid);
    } catch {}
  }
}
process.on("exit", killStarted);
for (const sig of ["SIGINT", "SIGTERM", "SIGBREAK"]) {
  process.on(sig, () => {
    killStarted();
    process.exit(130);
  });
}

/**
 * Starts the harness → a handle with its address and helpers. It resolves when the panel can be opened and the
 * backend answers (the first backend start can take minutes: timeoutMs, default 3 minutes).
 * opts: {args: extra harness flags, timeoutMs, log: where its stderr goes (default out/harness.log)}
 */
export async function startHarness({ args = [], timeoutMs = 180_000, log } = {}) {
  mkdirSync(SHOTS, { recursive: true });
  const env = {
    ...process.env,
    UV_PROJECT_ENVIRONMENT: process.env.UV_PROJECT_ENVIRONMENT ?? join(SCRATCH, "venv-research"),
    PYTHONDONTWRITEBYTECODE: "1",
  };
  const child = spawn("uv", ["run", "python", "tests/panel/harness.py", "--port", "0", ...args], {
    cwd: PLUGIN,
    env,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  started.add(child.pid);
  const errLog = createWriteStream(log ?? join(SHOTS, "harness.log"));
  const errTail = [];
  child.stderr.on("data", (d) => {
    errLog.write(d);
    errTail.push(String(d));
    if (errTail.length > 40) errTail.shift();
  });
  let exited = null;
  const exit = new Promise((r) => child.on("exit", (code) => r((exited = { code }))));
  const lines = [];
  let buf = "";
  child.stdout.on("data", (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      try {
        lines.push(JSON.parse(line));
      } catch {
        lines.push({ text: line });
      }
    }
  });
  const t0 = Date.now();
  let ready = null;
  while (!ready) {
    ready = lines.find((l) => l.port);
    const bad = lines.find((l) => l.error);
    if (bad || exited || Date.now() - t0 > timeoutMs) {
      child.kill();
      throw new Error(
        `The harness didn't start: ${bad?.error ?? (exited ? `it exited (${exited.code})` : "timed out")}\n` +
          errTail.join("").slice(-3000),
      );
    }
    if (!ready) await sleep(100);
  }
  started.add(ready.pid);
  const base = `http://127.0.0.1:${ready.port}`;

  async function req(path, body) {
    const r = await fetch(base + path, body === undefined ? {} : { method: "POST", body: JSON.stringify(body) });
    const text = await r.text();
    let value;
    try {
      value = JSON.parse(text);
    } catch {
      value = text;
    }
    if (!r.ok) throw new Error(`${path}: ${value?.error ?? text}`);
    return value;
  }

  const h = {
    ...ready,
    base,
    startedMs: Date.now() - t0,
    req,
    /** A backend tool's value (as layr.tools.run gives it), called as the user's panel. */
    tool: (name, args = {}, opts = {}) => req("/_test/tool", { name, args, ...opts }),
    /** What the panels asked layr for: [{seq, t, kind, ...}]; kinds: tool, event, notify, title, link, clipboard,
     *  problem, unknown, shellUnknown, menu, menuMiss, open, saveAs, panelOpen, fileRead, fileWrite, emit, key … */
    log: async (since = 0, kind) =>
      (await req(`/_test/log?since=${since}${kind ? `&kind=${kind}` : ""}`)).entries,
    state: () => req("/_test/state"),
    /** The answer to the next context menu (kind "menu": a label, "Parent > Child", {id}, or null), open dialog
     *  ({path} | {name, text} | {name, base64} | null) or save dialog (true | {name} | null). */
    answer: (kind, answer) => req("/_test/answer", { kind, answer }),
    /** Opens the panel again with new params (layr:panel.params, and layr.info.params from then on). */
    params: (params, panel = "experiments") => req("/_test/params", { params, panel }),
    /** The panel's saved state (layr.info.state on its next load); null clears it. */
    setPanelState: (state, panel = "experiments") => req("/_test/panel-state", { state, panel }),
    theme: (mode) => req("/_test/theme", { mode }),
    /** claude: "missing" (default) | "ungranted" | "usable"; answer: what its `ask` tool says. */
    connections: (claude, answer) => req("/_test/connections", { claude, answer }),
    emit: (topic, data) => req("/_test/emit", { topic, data }),
    /** A file "the user gave" the plugin → its ref (for params {file: ref}, as when a file opens the panel). */
    addFile: (spec) => req("/_test/file", spec),
    files: () => req("/_test/files"),
    /** The end of the backend's stderr (its log and tracebacks). */
    backendLog: (chars = 4000) => req(`/_test/backend-log?chars=${chars}`),
    /** Stops the harness and its backend; checks every pid it knew is gone (ends leftovers by pid). */
    async stop() {
      let pids = { pid: ready.pid, backendPid: ready.backendPid, children: [] };
      try {
        pids = await req("/_test/pids");
        await req("/_test/quit", {});
      } catch {}
      const t = Date.now();
      while (!exited && Date.now() - t < 30_000) await sleep(100);
      if (!exited) child.kill();
      await Promise.race([exit, sleep(3000)]);
      const all = [child.pid, pids.pid, pids.backendPid, ...pids.children].filter(Boolean);
      const t2 = Date.now();
      while (all.some(isAlive) && Date.now() - t2 < 5000) await sleep(200);
      const left = all.filter(isAlive);
      for (const pid of left) {
        try {
          process.kill(pid);
        } catch {}
      }
      started.delete(child.pid);
      started.delete(ready.pid);
      errLog.end();
      return { pids: all, left, report: lines.find((l) => l.stopped) ?? null };
    },
  };
  return h;
}

/** Polls fn (sync or async) until it gives something truthy → that value; throws after ms with what it waited for. */
export async function until(what, fn, ms = 15_000, every = 150) {
  const t = Date.now();
  let last;
  for (;;) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) {
      last = e;
    }
    if (Date.now() - t > ms) throw new Error(`Timed out after ${ms} ms waiting for ${what}${last ? `: ${last.message}` : ""}`);
    await sleep(every);
  }
}

/** A screenshot of the page into SHOTS (<name>.png) → its path. */
export async function shot(page, name) {
  const path = join(SHOTS, `${name}.png`);
  await page.screenshot({ path });
  return path;
}

/**
 * Collects what went wrong in the page: uncaught exceptions, console.error, failed requests and HTTP errors, and
 * from the harness: problems reported, unknown RPC and shell methods, error notifications. check(allow) throws when
 * something is left after the allow-list (strings: contained in the message; RegExps).
 */
export function collectErrors(page, h) {
  const seen = [];
  let since = 0;
  page.on("pageerror", (e) => seen.push(`uncaught: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && seen.push(`console.error: ${m.text()}`));
  page.on("requestfailed", (r) => seen.push(`request failed: ${r.url()} ${r.failure()?.errorText ?? ""}`));
  page.on("response", (r) => r.status() >= 400 && seen.push(`HTTP ${r.status()}: ${r.url()}`));
  async function harnessErrors() {
    const out = [];
    for (const e of await h.log(since)) {
      since = Math.max(since, e.seq);
      if (e.kind === "problem") out.push(`problem reported: ${e.title} ${e.details ?? ""}`);
      if (e.kind === "unknown" || e.kind === "shellUnknown") out.push(`unknown layr method: ${e.method}`);
      if (e.kind === "menuMiss") out.push(`context menu: ${e.error}`);
      if (e.kind === "notify" && e.level === "error") out.push(`error notification: ${e.text}`);
    }
    return out;
  }
  return {
    seen,
    async all() {
      seen.push(...(await harnessErrors()));
      return seen.slice();
    },
    async check(allow = []) {
      const left = (await this.all()).filter(
        (m) => !allow.some((a) => (a instanceof RegExp ? a.test(m) : m.includes(a))),
      );
      if (left.length) throw new Error(`The page had errors:\n  ${left.join("\n  ")}`);
    },
  };
}

/**
 * Opens a panel in a fresh browser context → {page, context, errors}. opts: {panel: "experiments" | "main",
 * viewport, query: extra URL query}. It waits until layr.js is connected.
 */
export async function openPanel(browser, h, { panel = "experiments", viewport = { width: 1360, height: 860 }, query = "" } = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: h.base });
  const page = await context.newPage();
  const errors = collectErrors(page, h);
  await page.goto(h.urls[panel] + query);
  await page.waitForFunction(() => window.layr && window.layr.connected, null, { timeout: 30_000 });
  return { page, context, errors };
}

/**
 * Runs a test script: starts the harness and Chromium, calls main(ctx), and always stops both (the process exit
 * code says pass or fail). ctx: {h, browser, open(opts) → {page, errors}, shot, until, step(name)}.
 */
export async function bench(name, main, { harnessArgs = [], headless = true } = {}) {
  const t0 = Date.now();
  const stamp = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;
  let h = null;
  let browser = null;
  let failed = null;
  const opened = [];
  mkdirSync(SHOTS, { recursive: true });
  for (const f of readdirSync(SHOTS)) if (/^FAIL-\d+\.png$/.test(f)) rmSync(join(SHOTS, f)); // from an earlier run
  try {
    h = await startHarness({ args: harnessArgs });
    console.log(`[${stamp()}] harness on ${h.base} (pid ${h.pid}, backend ${h.backendPid})`);
    browser = await playwright().chromium.launch({ headless });
    const ctx = {
      h,
      browser,
      shot,
      until,
      open: async (opts) => {
        const o = await openPanel(browser, h, opts);
        opened.push(o);
        return o;
      },
      step: (what) => console.log(`[${stamp()}] ${what}`),
    };
    await main(ctx);
  } catch (e) {
    failed = e;
    // what the pages looked like and what went wrong in them, to see why
    for (const [i, o] of opened.entries()) {
      const path = await shot(o.page, `FAIL-${i + 1}`).catch(() => null);
      const errs = await o.errors.all().catch(() => []);
      console.error(`page ${i + 1}: ${path ?? "no screenshot"}${errs.length ? `\n  ${errs.join("\n  ")}` : ""}`);
    }
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (h) {
      const { pids, left, report } = await h.stop();
      if (report) console.log(`[${stamp()}] the harness stopped in ${report.stopMs} ms${report.killed?.length ? `, ending ${report.killed.join(", ")}` : ""}`);
      console.log(`[${stamp()}] stopped; processes ${pids.join(", ")}: ${left.length ? `LEFT ${left.join(", ")}` : "all gone"}`);
      if (left.length && !failed) failed = new Error(`processes left behind: ${left.join(", ")}`);
    }
  }
  if (failed) {
    console.error(`FAIL ${name} after ${stamp()}:`, failed.stack || failed);
    process.exitCode = 1;
  } else {
    console.log(`PASS ${name} in ${stamp()}`);
  }
}

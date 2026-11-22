// Experiments: one workspace for a method and an experiment of it. The method is Python code drawn as a flow
// (flow.js) with its code under it (editor.js), explained from itself; the experiment brings the data that flows
// through it, its own pages made of cards (pages.js), its results, checks, corrections and settings. Everything goes
// through the plugin's own tools (layr.tools.run), the same ones AI agents use.
//
// Every place in the panel is reached through one function, show(target): the buttons use it, and links to "this
// experiment / method / tab / result / function / line" call it too.
import { createEditor, createMerge, highlight } from "./editor.js";
import { mountFlow } from "./flow.js";
import { createPages } from "./pages.js";
import { resizable } from "./resize.js";
import { cellKind, cellText, cellTitle, createRounds, roundWords } from "./rounds.js";
import { createSources, tableWord } from "./sources.js";

const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");
const actions = document.getElementById("actions");
const statusEl = document.getElementById("status");
const dialog = document.getElementById("dialog");

// The panel's state (layr.panel.setState keeps it for the next time the panel opens).
const S = {
  view: "home", // home | work (the workspace: a method, with an experiment of it or without data) | new | guide
  method: null, // the workspace's method
  exp: null, // the workspace's experiment (null: just the method, without data)
  tab: "flow", // flow ("Flow & code") | page:<n> | results | checks | corrections | settings
  run: null, // an earlier run of the experiment picked in the flow (null: its latest)
  code: false, // the Flow & code tab shows the code under the flow (false: folded away, the flow fills the tab)
  fn: null, // the function the code shows (where the cursor is)
  line: null, // a line of the code to show, once
  result: {}, // what the Results tab shows, per experiment: {experiment: ":numbers" | an output | "step:<function>"}
  target: null, // {exp, result, row, column, why}: a number (or a single value) the Results tab shows highlighted, once
  setting: null, // a setting to show, once, when the Settings tab opens
  review: {}, // each review card's item, region, filter and zoom (pages.js)
  focus: null, // {item, label} a review card opens at (a link from Checks or Corrections)
  newMethod: null, // the method a new experiment starts with
  flowFrac: null, // how much of the Flow & code tab's height the flow takes while the code shows (null: the usual)
  sizes: {}, // the sizes she dragged, in px, per place (resize.js): "work.outline", "flow.side", "results.list" …
  autoRecalc: true, // after a change of the code, a trial run on the experiment by itself
  seenComparison: null, // "method:version" whose comparison banner was closed
};
let live = null; // {exp, step, timer} while a run the panel started is in progress
let token = 0; // render generation: a slower, older render must not overwrite a newer one
let EXPS = []; // every experiment, as the experiments tool listed them last
let METHODS = []; // every method, as the methods tool listed them last

// ------------------------------------------------------------------------------------------------ helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// replaceChildren that skips null/false and flattens arrays (as h() does)
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
}

function tex(latex, display = false) {
  const el = h("span", { class: display ? "tex display" : "tex" });
  try {
    window.katex.render(latex || "", el, { throwOnError: false, displayMode: display, strict: "ignore" });
  } catch {
    el.textContent = latex;
  }
  return el;
}

const call = (tool, args = {}) => layr.tools.run(tool, args);

function fmt(v) {
  if (v == null || v === "") return "";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "";
    if (Number.isInteger(v)) return String(v);
    const a = Math.abs(v);
    return a !== 0 && (a < 1e-3 || a >= 1e6) ? v.toExponential(3) : String(+v.toPrecision(4));
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fail(err) {
  console.error(err);
  layr.ui.notify(err?.message || String(err), "error");
}

function setStatus(text) {
  statusEl.textContent = text || "";
}

function save() {
  const { line, setting, focus, newMethod, preset, target, ...keep } = S; // what only the next drawing uses isn't kept
  layr.panel.setState(keep).catch(() => {});
}

/** Where a size she drags at `place` is remembered (S.sizes, saved with the panel's state): resizable's `size`. */
function sizeOf(place) {
  return {
    get: () => S.sizes?.[place] ?? null,
    set: (px) => {
      S.sizes = { ...S.sizes };
      if (px == null) delete S.sizes[place];
      else S.sizes[place] = px;
      save();
    },
  };
}

/** Wide enough for boxes side by side (narrower, the CSS stacks them and nothing is resized sideways). */
const sideBySide = (px = 760) => innerWidth > px;

/** Changes the state and draws the panel again. Leaving the workspace with unsaved code asks first. */
function go(patch) {
  const leaving =
    S.view === "work" && ((patch.view && patch.view !== "work") || (patch.method && patch.method !== S.method));
  if (leaving && ME?.dirty) {
    openDialog({
      title: "Leave without saving?",
      body: "Your changes to the method's code aren't saved.",
      buttons: [
        { label: "Stay", id: false, kind: "ghost" },
        { label: "Leave", id: true, kind: "danger" },
      ],
    }).then((ok) => {
      if (ok) {
        ME.dirty = false;
        go(patch);
      }
    });
    return;
  }
  if (leaving) closeWork();
  if ("exp" in patch && patch.exp !== S.exp && !("run" in patch)) patch = { ...patch, run: null };
  Object.assign(S, patch);
  save();
  render();
}

/**
 * Goes to a place in the panel: the one function the panel's buttons and its links use. `target`:
 *   {method, exp, tab, result, fn, line, run, setting, focus, code}: the workspace. An experiment alone opens its
 *     method's; a method alone opens with its reference experiment (or the one run last), `exp: null` without data;
 *     neither (on the workspace) keeps the one open.
 *   {view: "home"}, {view: "new", newMethod}, {view: "guide"}
 * tab: "flow" (Flow & code), "page:<n>", "results", "checks", "corrections", "settings". result: what the Results
 * tab shows (":numbers", an output's name, "step:<function>"; a single value's name shows it among the numbers,
 * highlighted). cell: {row: {column: value, ...} picking one row, column}: that number of the result's table, scrolled
 * into view and highlighted (filters cleared); why: true also opens how it was calculated. fn / line: a function or a
 * line of the method's code (they show the code under the flow). code: true shows the code, false folds it away; else opening an experiment
 * starts with it folded, opening a method without data with it shown, and on the same workspace it stays as it is.
 */
async function show(target = {}) {
  const t = { view: "work", ...target };
  if (t.view !== "work") return go(t);
  if (!t.method && !("exp" in t) && S.view === "work") t.method = S.method;
  try {
    if ((t.exp && !t.method) || (t.method && t.exp === undefined && !(S.view === "work" && S.method === t.method))) {
      const [exps, methods] = await Promise.all([call("experiments"), METHODS.length ? METHODS : call("methods")]);
      EXPS = exps;
      METHODS = methods;
      if (t.exp && !t.method) t.method = exps.find((e) => e.experiment === t.exp)?.method;
      if (t.exp === undefined) t.exp = defaultExperiment(t.method);
    }
  } catch (err) {
    return fail(err);
  }
  if (!t.method) return go({ view: "home" });
  const same = S.view === "work" && S.method === t.method;
  const exp = t.exp === undefined ? (same ? S.exp : null) : t.exp || null;
  const patch = { view: "work", method: t.method, exp };
  patch.tab = t.tab || (t.result ? "results" : t.fn || t.line ? "flow" : same ? S.tab : "flow");
  patch.code = t.fn || t.line ? true : "code" in t ? !!t.code : same ? S.code : !exp;
  if (t.fn !== undefined || !same) patch.fn = t.fn || null;
  if (t.line) patch.line = t.line;
  if ("run" in t) patch.run = t.run || null;
  if (t.result && exp) patch.result = { ...S.result, [exp]: t.result };
  patch.target = t.result && exp && t.cell ? { exp, result: t.result, row: t.cell.row, column: t.cell.column, why: !!t.why } : null;
  if (t.setting) patch.setting = t.setting;
  if (t.focus) patch.focus = t.focus;
  go(patch);
}

/** The experiment a method opens with: its reference experiment, else the one run last, else none. */
function defaultExperiment(method) {
  const mine = EXPS.filter((e) => e.method === method);
  const ref = METHODS.find((m) => m.id === method)?.reference;
  if (ref && mine.some((e) => e.experiment === ref)) return ref;
  const at = (e) => String(e.last_run?.finished || e.last_run?.started || "");
  return mine.filter((e) => e.last_run).sort((a, b) => at(b).localeCompare(at(a)))[0]?.experiment || null;
}

function badge(text, kind) {
  return h("span", { class: `layr-badge ${kind || ""}` }, text);
}

function flagBadges(counts) {
  if (!counts) return [];
  const out = [];
  if (counts.error) out.push(badge(`${counts.error} errors`, "danger"));
  if (counts.warning) out.push(badge(`${counts.warning} warnings`, "warning"));
  if (counts.info) out.push(badge(`${counts.info} notes`, ""));
  return out;
}

/**
 * A modal dialog → the id (or `value()`) of the button pressed, null when closed. A button's `value()` may return a
 * promise (the button waits); undefined keeps the dialog open (invalid, or an error shown in it). `size`: "wide",
 * "medium" or nothing.
 */
function openDialog({ title, body, buttons, wide = false, size = wide ? "wide" : "" }) {
  return new Promise((resolve) => {
    dialog.classList.toggle("wide", size === "wide");
    dialog.classList.toggle("medium", size === "medium");
    put(
      dialog,
      h("h3", {}, title),
      h("div", { class: "dialog-body" }, body),
      h(
        "div",
        { class: "dialog-buttons" },
        buttons.map((b) =>
          h(
            "button",
            {
              class: `layr-button ${b.kind || ""}`,
              title: b.title || null,
              onclick: async (e) => {
                let v = b.value ? b.value() : b.id;
                if (v && typeof v.then === "function") {
                  const btn = e.currentTarget;
                  btn.disabled = true;
                  try {
                    v = await v;
                  } finally {
                    btn.disabled = false;
                  }
                }
                if (v === undefined) return; // invalid: keep open
                dialog.close();
                resolve(v);
              },
            },
            b.label,
          ),
        ),
      ),
    );
    dialog.onclose = () => resolve(null);
    // Enter in a field confirms (the primary button), as in any form; Escape closes (the dialog does that itself)
    dialog.onkeydown = (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing || !e.target.matches?.("input:not([type=checkbox])"))
        return;
      const primary = dialog.querySelector(".dialog-buttons .layr-button.primary");
      if (!primary) return;
      e.preventDefault();
      primary.click();
    };
    dialog.showModal();
  });
}

async function askReason(title, hint) {
  const ta = h("textarea", { class: "layr-textarea", rows: 3, placeholder: hint || "Why? A reviewer reads this." });
  const err = h(
    "div",
    { class: "layr-error", hidden: true },
    "A reason is needed: every correction is listed with it.",
  );
  setTimeout(() => ta.focus(), 30);
  return openDialog({
    title,
    body: [h("label", { class: "layr-label" }, "Reason"), ta, err],
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      {
        label: "Save",
        kind: "primary",
        value: () => (ta.value.trim() ? ta.value.trim() : ((err.hidden = false), undefined)),
      },
    ],
  });
}

/**
 * A table of row objects. onRow(row) / onCell(row, column): clicks (only numbers are clickable cells); onMenu(row,
 * column, event): a right-click on a cell that holds something. focus: {row (one of `rows`), column}: that cell is
 * marked (class cell-focus) and its row is among the rows shown however far down it is.
 */
function table(rows, columns, { onRow, onCell, onMenu, focus, limit = 200 } = {}) {
  if (!rows.length) return h("div", { class: "layr-muted pad" }, "No rows.");
  const cols = columns || Object.keys(rows[0]);
  const at = focus ? rows.indexOf(focus.row) : -1;
  if (at >= limit) limit = at + 20;
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "layr-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          cols.map((c) => h("th", {}, c)),
        ),
      ),
      h(
        "tbody",
        {},
        rows.slice(0, limit).map((r) =>
          h(
            "tr",
            { class: `${onRow ? "click" : ""}${r === focus?.row ? " row-focus" : ""}`, onclick: onRow && (() => onRow(r)) },
            cols.map((c) =>
              h(
                "td",
                {
                  class:
                    (typeof r[c] === "number" ? (onCell ? "num click" : "num") : "") +
                    (r === focus?.row && c === focus.column ? " cell-focus" : ""),
                  title:
                    typeof r[c] === "number" && onCell
                      ? `How was this number calculated?${onMenu ? " (Right-click: copy a link to it)" : ""}`
                      : null,
                  onclick: typeof r[c] === "number" && onCell ? () => onCell(r, c) : null,
                  oncontextmenu: onMenu && r[c] != null && r[c] !== "" ? (e) => onMenu(r, c, e) : null,
                },
                fmt(r[c]),
              ),
            ),
          ),
        ),
      ),
    ),
    rows.length > limit
      ? h("div", { class: "layr-muted pad" }, `${rows.length - limit} more rows (export to Excel for all).`)
      : null,
  );
}

/** Whether a row has these values (numbers as numbers, anything else as text: as the backend compares them). */
function rowHas(r, values) {
  return Object.entries(values || {}).every(([k, v]) =>
    typeof v === "number" && typeof r[k] === "number"
      ? Math.abs(r[k] - v) <= 1e-9 * Math.max(1, Math.abs(v))
      : String(r[k] ?? "") === String(v ?? ""),
  );
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToB64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// A file the user picks goes into the plugin's store: read by the backend through layr's file route, or sent in
// pieces when that isn't possible.
async function pickAndStore(types) {
  const ref = await layr.files.open({ types });
  if (!ref) return null;
  return storeFileRef(ref);
}

async function storeFileRef(ref) {
  setStatus(`Storing ${ref.name}…`);
  try {
    const url = new URL(layr.files.url(ref.id), location.href).href;
    return await call("file_fetch", { url, name: ref.name });
  } catch (err) {
    console.warn("file_fetch failed, sending in pieces", err);
    const bytes = new Uint8Array(await layr.files.read(ref.id));
    let upload = null;
    const step = 3 * 1024 * 1024;
    for (let i = 0; i < bytes.length; i += step) {
      setStatus(`Sending ${ref.name}: ${Math.round((100 * i) / bytes.length)} %`);
      upload = (await call("upload_chunk", { upload, data: bytesToB64(bytes.subarray(i, i + step)) })).upload;
    }
    return await call("upload_finish", { upload, name: ref.name });
  } finally {
    setStatus("");
  }
}

// ------------------------------------------------------------------------------------------------ Claude (optional)

// Explain and the Methods paragraph ask Claude through the claude plugin's `ask` tool, an optional connection
// (layr.json): the buttons show only while it is usable (or not decided yet: the first call asks the user).
const AI = { claude: false };

async function checkAi() {
  try {
    const { connections = [] } = await layr.connections.get();
    const c = connections.find((x) => x.plugin === "claude");
    AI.claude = Boolean(
      c?.usable ||
      (c?.installed &&
        c.state === "ungranted" &&
        !c.blocked &&
        (c.groups ?? []).every((g) => g.exists !== false && g.rule !== "deny")),
    );
  } catch {
    AI.claude = false;
  }
  return AI.claude;
}

async function askClaude(kind, args) {
  const p = await call("ai_prompt", { kind, ...args });
  let result;
  try {
    result = await layr.tools.run("ask", { prompt: p.prompt, system: p.system, model: "sonnet" }, { plugin: "claude" });
  } catch (err) {
    if (err?.code === layr.errors?.NOT_ALLOWED)
      throw new Error("Research isn't allowed to ask Claude: allow it in Settings › Tool access (Research).");
    if (err?.code === layr.errors?.NOT_FOUND)
      throw new Error("Claude Code isn't installed: install it from the store.");
    throw err;
  }
  const text = typeof result === "string" ? result : result?.text;
  if (!text?.trim()) throw new Error("Claude gave an empty answer: try again.");
  return text.trim();
}

function aiAnswer(text, label = "Written by AI (Claude): check it against the code.") {
  return h(
    "div",
    { class: "ai-answer" },
    h("div", { class: "ai-label" }, label),
    text.split(/\n{2,}/).map((p) => h("p", {}, p)),
  );
}

// ------------------------------------------------------------------------------------------------ links

// "Copy link" puts a reference to something in Research on the clipboard as a Markdown link (reference_make: its text
// is readable anywhere, its address is layr://research/open/<ref>); a Wiki page draws it as a card. Such a link opens
// this panel with the param `ref`, and openRef goes there.

/** Copies the Markdown link of a reference (reference_make's arguments) → true when it is on the clipboard. */
async function copyLink(args) {
  let r;
  try {
    const { all, ...make } = args;
    r = await call("reference_make", make);
  } catch (err) {
    // one number whose row the columns picked don't tell apart in the whole table: every column of the row then
    if (args.row && args.all && /give more columns/.test(String(err?.message || err)))
      return copyLink({ ...args, row: args.all, all: null });
    layr.ui.notify(cleanError(err), "error");
    return false;
  }
  try {
    await layr.clipboard.writeText(r.markdown);
  } catch (err) {
    fail(err);
    return false;
  }
  layr.ui.notify(
    S.run && args.experiment ? "Link copied: paste it in a Wiki page (it always shows the latest run)" : "Link copied: paste it in a Wiki page",
    "success",
  );
  return true;
}

/** A link to one value of a table: its row picked by the columns that tell it apart (rowKeys), more if needed. */
function copyCellLink(exp, tableName, row, column, rows) {
  const scalar = (v) => v !== null && v !== "" && typeof v !== "object";
  const keys = rowKeys(rows || [row], row, column).filter((k) => scalar(row[k]));
  let left = (rows || [row]).filter((r) => rowHas(r, Object.fromEntries(keys.map((k) => [k, row[k]]))));
  for (const k of Object.keys(row)) {
    if (left.length <= 1) break;
    if (k === column || keys.includes(k) || !scalar(row[k])) continue;
    const next = left.filter((r) => rowHas(r, { [k]: row[k] }));
    if (next.length < left.length) {
      keys.push(k);
      left = next;
    }
  }
  const pick = (ks) => Object.fromEntries(ks.map((k) => [k, row[k]]));
  const all = pick(Object.keys(row).filter((k) => k !== column && scalar(row[k])));
  return copyLink({ experiment: exp.id, result: tableName, row: pick(keys), column, all });
}

/** Waits until the workspace shows the experiment's flow, drawn (after a show()) → false when it doesn't come. */
async function flowReady(expId, ms = 30_000) {
  for (const t0 = Date.now(); Date.now() - t0 < ms; ) {
    if (S.view === "work" && ME?.exp === expId && ME.flow?.graph && !ME.els.flow.querySelector(".fl.loading"))
      return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
}

/**
 * Opens the panel at a reference (the `ref` of a layr://research/open/… link: experiment, method, result, row,
 * column, function, …). What no longer exists is gone round: it goes as far as it can and says what is missing in
 * one notice.
 */
async function openRef(text) {
  let ref = String(text || "").trim();
  if (ref.startsWith("layr://research/open/")) ref = decodeURIComponent(ref.slice("layr://research/open/".length));
  const p = Object.fromEntries(new URLSearchParams(ref.replace(/^\?/, "")));
  let row = null;
  let got;
  try {
    if (p.row) row = JSON.parse(p.row);
  } catch {
    layr.ui.notify("This link can't be opened: the row it names can't be read.", "warning");
    return;
  }
  try {
    got = await call("reference_get", { ref });
  } catch (err) {
    layr.ui.notify(`This link can't be opened: ${cleanError(err)}`, "warning");
    return;
  }
  const tell = (text = got.missing) => text && layr.ui.notify(text, "warning");
  try {
    [EXPS, METHODS] = await Promise.all([call("experiments"), call("methods")]);
  } catch (err) {
    return fail(err);
  }
  if (p.experiment) {
    const e = EXPS.find((x) => x.experiment === p.experiment);
    if (!e) {
      await show({ view: "home" });
      return tell(got.missing || "This experiment no longer exists.");
    }
    const at = { method: e.method, exp: e.experiment };
    if (p.function) {
      // a step: the flow, its box selected and looked inside (the code stays folded: the flow's side panel shows it)
      await show({ ...at, tab: "flow", code: false });
      if (got.ok && (await flowReady(e.experiment))) ME.flow.lookInside(p.function);
      return tell();
    }
    if (p.result) {
      const number = got.ok && row && p.column;
      await show({ ...at, result: p.result, ...(number ? { cell: { row, column: p.column } } : {}) });
      return tell();
    }
    await show(at);
    return tell();
  }
  if (p.method) {
    if (!METHODS.some((m) => m.id === p.method)) {
      await show({ view: "home" });
      return tell(got.missing || "This method no longer exists.");
    }
    await show(p.function && got.ok ? { method: p.method, fn: p.function, code: true } : { method: p.method });
    return tell();
  }
  tell("This link doesn't say what to open.");
}

// ------------------------------------------------------------------------------------------------ chrome

/** The path at the top: [{label, go: a show() target}] links, {label} plain, {node} an element (a picker). */
function setCrumbs(items) {
  put(
    crumbs,
    ...items.flatMap((it, i) => [
      i ? h("span", { class: "sep" }, "›") : null,
      it.node
        ? it.node
        : it.go
          ? h(
              "a",
              {
                href: "#",
                onclick: (e) => {
                  e.preventDefault();
                  show(it.go);
                },
              },
              it.label,
            )
          : h("b", {}, it.label),
    ]),
  );
}

function setActions(nodes) {
  put(actions, ...nodes.filter(Boolean));
}

// ------------------------------------------------------------------------------------------------ home

let EXAMPLES = null; // what "Try an example" offers (the examples tool)

/** What an experiment's last run says, in a word or two. */
function stateWord(e) {
  if (e.running) return "running";
  const st = e.last_run?.status;
  return !st ? "not run yet" : st === "done" ? "calculated" : st === "stopped" ? "stopped" : "failed";
}

/** The experiment was last calculated with an older version of its method (`version`: the method's newest). */
const outOfDate = (e, version) =>
  !!version && e.last_run?.status === "done" && e.last_run.method_version != null && e.last_run.method_version < version;

/** Why the experiment is out of date because of what it reads, one reason per experiment read ("Spring: …"). */
const sourceReasons = (e) => (e.outdated || []).map((o) => `${o.title}: ${o.reason}`);

function expCard(e) {
  const open = () => show({ method: e.method, exp: e.experiment });
  const menu = (ev) => {
    ev.stopPropagation();
    layr.ui.contextMenu(
      [
        { label: "Open", icon: "open_in_new", run: open },
        { label: "Copy link to this experiment", icon: "link", run: () => copyLink({ experiment: e.experiment }) },
      ],
      ev,
    );
  };
  // the experiments it reads and those that read it, each a link
  const titled = (ids) =>
    [...new Set(ids)].map((id, i) => [
      i ? ", " : "",
      h(
        "a",
        {
          href: "#",
          onclick: (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            show({ exp: id });
          },
        },
        EXPS.find((x) => x.experiment === id)?.title || "a deleted experiment",
      ),
    ]);
  const reads = (e.reads || []).map((r) => r.experiment);
  const readBy = (e.read_by || []).map((r) => r.experiment);
  const files = Object.values(e.files || {});
  return h(
    "div",
    { class: "layr-card exp-card click", onclick: open, oncontextmenu: menu },
    h(
      "div",
      { class: "exp-head" },
      h("div", { class: "exp-title" }, e.title),
      h(
        "button",
        { class: "layr-button small ghost icon exp-more", title: "More", "aria-label": `More for ${e.title}`, onclick: menu },
        "⋯",
      ),
    ),
    files.length || !reads.length ? h("div", { class: "layr-muted small" }, files.join(", ") || "no file yet") : null,
    reads.length ? h("div", { class: "layr-muted small exp-links" }, "Reads ", titled(reads)) : null,
    readBy.length ? h("div", { class: "layr-muted small exp-links" }, "Read by ", titled(readBy)) : null,
    h(
      "div",
      { class: "exp-meta" },
      e.running
        ? badge("running", "accent")
        : e.last_run
          ? [
              badge(stateWord(e), e.last_run.status === "done" ? "success" : "danger"),
              outOfDate(e, METHODS.find((m) => m.id === e.method)?.version) ? badge("older version of the method", "warning") : null,
              e.outdated?.length ? badge("out of date", "warning") : null,
              ...flagBadges(e.last_run.summary?.flags),
              h("span", { class: "layr-muted small" }, when(e.last_run.finished)),
            ]
          : badge("not run yet", ""),
      e.proposed_corrections ? badge(`${e.proposed_corrections} proposed corrections`, "warning") : null,
    ),
    e.outdated?.length && !e.running
      ? h("div", { class: "exp-why small", title: sourceReasons(e).join("\n") }, sourceReasons(e).join(" · "))
      : null,
  );
}

/** The experiments grouped by their method: a section per method (one nobody uses yet too), its experiments in it. */
async function renderHome(t) {
  const [exps, methods] = await Promise.all([call("experiments"), call("methods")]);
  if (t !== token) return;
  EXPS = exps;
  METHODS = methods;
  if (!EXAMPLES) call("examples").then((r) => (EXAMPLES = r.examples || []), () => {});
  setCrumbs([{ label: "Experiments" }]);
  layr.panel.setTitle("Experiments").catch(() => {});
  setActions([
    h("button", { class: "layr-button ghost", onclick: () => show({ view: "guide" }) }, "Python guide"),
    h("button", { class: "layr-button ghost", onclick: makeExample }, "Try an example ▾"),
    h("button", { class: "layr-button", onclick: newMethod }, "New method"),
    h("button", { class: "layr-button primary", onclick: () => show({ view: "new" }) }, "New experiment"),
  ]);
  const used = (m) => exps.some((e) => e.method === m.id);
  const section = (m) => {
    const mine = exps.filter((e) => e.method === m.id);
    return h(
      "section",
      { class: "method-section" },
      h(
        "div",
        { class: "method-head" },
        h("h2", {}, m.name),
        h(
          "span",
          { class: "layr-muted small method-state" },
          m.draft_only ? "new, proposed by Claude" : `version ${m.version}`,
          m.builtin ? " · built in" : "",
        ),
        m.draft && !m.draft_only ? badge("a change by Claude waits for you", "warning") : null,
        m.draft_only ? badge("waiting for you", "warning") : null,
        h("span", { class: "layr-spacer" }),
        h(
          "button",
          {
            class: "layr-button small ghost",
            title: "The method's flow and code (with the data of its reference experiment, or the one run last)",
            onclick: () => show({ method: m.id }),
          },
          "Open",
        ),
        m.draft_only
          ? null
          : h(
              "button",
              { class: "layr-button small", onclick: () => show({ view: "new", newMethod: m.id }) },
              "New experiment",
            ),
      ),
      m.description ? h("p", { class: "method-desc layr-muted" }, m.description) : null,
      mine.length
        ? h("div", { class: "cards" }, mine.map(expCard))
        : h("div", { class: "layr-muted small no-exp" }, "No experiment yet."),
    );
  };
  put(
    view,
    h(
      "div",
      { class: "page home" },
      h(
        "p",
        { class: "layr-muted home-intro" },
        "Your experiments, grouped by the method they use. A method is the recipe, written in plain Python; an " +
          "experiment runs it on your data, and every number it makes can be traced back to its line.",
      ),
      [...methods.filter(used), ...methods.filter((m) => !used(m))].map(section),
    ),
  );
}

/** A method from nothing: a name and a sentence give an empty recipe; the flow then says what to add first. */
async function newMethod() {
  const name = h("input", { class: "layr-input", placeholder: "e.g. Growth on plates" });
  const sentence = h("textarea", {
    class: "layr-textarea",
    rows: 2,
    placeholder: "e.g. Growth rates of cultures read on a plate reader.",
  });
  const err = h("div", { class: "layr-error", hidden: true }, "Give it a name.");
  setTimeout(() => name.focus(), 30);
  const ok = await openDialog({
    title: "New method",
    body: [
      h("label", { class: "layr-label" }, "Its name"),
      name,
      h("label", { class: "layr-label" }, "What it measures, in a sentence"),
      sentence,
      err,
    ],
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      {
        label: "Make it",
        kind: "primary",
        value: () => (name.value.trim() ? true : ((err.hidden = false), undefined)),
      },
    ],
  });
  if (!ok) return;
  try {
    const title = name.value.trim();
    const fresh = await call("flow_new", { name: title, description: sentence.value.trim() || undefined });
    const methods = await call("methods");
    const base =
      title
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "") || "method";
    let id = base;
    for (let i = 2; methods.some((m) => m.id === id); i++) id = `${base}-${i}`;
    const r = await call("method_create", {
      method: id,
      name: title,
      description: sentence.value.trim(),
      source: fresh.source,
    });
    if (r.saved === false)
      return layr.ui.notify((r.messages || []).map((m) => m.message).join("; ") || r.note, "error");
    show({ method: id, exp: null });
  } catch (e) {
    fail(e);
  }
}

/** An example experiment (the examples tool lists them): made, opened and run. */
async function makeExample(ev) {
  try {
    if (!EXAMPLES) EXAMPLES = (await call("examples")).examples || [];
  } catch (err) {
    return fail(err);
  }
  if (!EXAMPLES.length) return layr.ui.notify("No examples are installed.", "info");
  const picked = await layr.ui.contextMenu(
    EXAMPLES.map((x) => ({ label: `${x.title}: ${x.description}`, icon: "science", id: x.id })),
    ev,
  );
  if (!picked) return; // the menu was closed without a pick
  let e;
  try {
    setStatus("Making the example…");
    e = await call("lab_sample", { which: picked.id });
  } catch (err) {
    fail(err);
  } finally {
    setStatus("");
  }
  if (!e) return;
  await show({ method: e.method, exp: e.experiment, tab: "flow", code: false });
  // the run starts once the workspace has read what it shows (on Windows a run's files can't be replaced while they
  // are being read: see the backend's store), and after the status above is cleared: the status follows the run
  for (let i = 0; i < 60 && !ME?.flow?.graph; i++) await new Promise((r) => setTimeout(r, 100));
  await new Promise((r) => setTimeout(r, 400));
  startRun(e.experiment);
}

// ------------------------------------------------------------------------------------------------ new experiment

const extOf = (name) => (/\.[^./\\]+$/.exec(String(name || ""))?.[0] || "").toLowerCase();
const reads = (inp, ext) => (inp.types || []).some((x) => String(x).toLowerCase() === ext);

/**
 * A new experiment: a title, a method, a file for each of its file inputs and the experiments each of its result
 * inputs reads (`preset`: a file the panel was opened with).
 */
async function renderNew(t, preset) {
  const [methods, exps] = await Promise.all([call("methods"), call("experiments")]);
  if (t !== token) return;
  METHODS = methods;
  EXPS = exps;
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: "New experiment" }]);
  setActions([]);
  layr.panel.setTitle("New experiment").catch(() => {});
  const usable = methods.filter((m) => !m.draft_only);
  const ext = preset ? extOf(preset.name) : "";
  // the method to start with: the one asked for, else (for a file) the first that reads its kind of file
  const start =
    usable.find((m) => m.id === S.newMethod)?.id ||
    (ext && usable.find((m) => (m.inputs || []).some((i) => reads(i, ext)))?.id) ||
    usable[0]?.id;
  const files = {};
  const picks = {}; // result input → [{experiment, result}]
  const title = h("input", { class: "layr-input", placeholder: "e.g. Attitude survey, spring wave" });
  const methodSel = h(
    "select",
    { class: "layr-select" },
    usable.map((m) => h("option", { value: m.id, selected: m.id === start }, m.name)),
  );
  const about = h("div", { class: "layr-muted small" });
  const readsLabel = h("label", { class: "layr-label" }, "Raw data");
  const filesBox = h("div", {});
  const err = h("div", { class: "layr-error", hidden: true });
  const titleOf = (id) => EXPS.find((e) => e.experiment === id)?.title || id;
  const resultRow = (inp) => {
    const items = (picks[inp.id] || []).map((p) => ({ ...p, title: titleOf(p.experiment) }));
    return h(
      "div",
      { class: "file-row src-input" },
      h("span", {}, inp.label || inp.id),
      " ",
      SRC.chips(items, {
        onRemove: (i) => {
          picks[inp.id].splice(i, 1);
          drawFiles();
        },
      }),
      " ",
      h(
        "button",
        {
          class: "layr-button small",
          onclick: async () => {
            const got = await SRC.pick({ input: inp, picked: items, exps: EXPS, methods: METHODS });
            if (got) {
              picks[inp.id] = got;
              drawFiles();
            }
          },
        },
        items.length ? "Change…" : "Pick experiments…",
      ),
    );
  };
  const drawFiles = () => {
    const m = usable.find((x) => x.id === methodSel.value);
    const inputs = m?.inputs || [];
    err.hidden = true;
    put(about, m?.description || "");
    put(readsLabel, inputs.some((i) => i.kind === "result") ? "What it reads" : "Raw data");
    for (const k of Object.keys(picks)) if (!inputs.some((i) => i.id === k && i.kind === "result")) delete picks[k];
    // the file the panel was opened with goes to the input that reads its kind of file
    for (const k of Object.keys(files)) if (!inputs.some((i) => i.id === k) || files[k] === preset) delete files[k];
    const home = preset ? inputs.find((i) => reads(i, ext)) : null;
    if (home && !files[home.id]) files[home.id] = preset;
    put(
      filesBox,
      inputs.length ? null : h("div", { class: "layr-muted" }, "This method reads no file."),
      inputs.map((inp) =>
        inp.kind === "result" ? resultRow(inp) : h(
          "div",
          { class: "file-row" },
          h("span", {}, inp.label || inp.id),
          " ",
          files[inp.id] ? h("b", {}, files[inp.id].name) : h("span", { class: "layr-muted" }, "no file"),
          " ",
          h(
            "button",
            {
              class: "layr-button small",
              onclick: async () => {
                try {
                  const f = await pickAndStore(inp.types || []);
                  if (f) {
                    files[inp.id] = f;
                    drawFiles();
                  }
                } catch (err) {
                  fail(err);
                }
              },
            },
            files[inp.id] ? "Another file…" : "Choose file…",
          ),
        ),
      ),
      preset && !home
        ? h("div", { class: "banner" }, `This method can't read ${preset.name}: pick another method, or other files.`)
        : null,
    );
  };
  methodSel.onchange = () => {
    err.hidden = true;
    drawFiles();
  };
  drawFiles();
  const create = async () => {
    const m = usable.find((x) => x.id === methodSel.value);
    if (!m) return;
    const say = (text) => {
      put(err, text);
      err.hidden = false;
    };
    const missing = (m.inputs || []).filter((i) => !i.optional && (i.kind === "result" ? !picks[i.id]?.length : !files[i.id]));
    if (missing.length)
      return say(
        missing.map((i) => (i.kind === "result" ? `Pick the experiments for: ${i.label || i.id}` : `Choose the file for: ${i.label || i.id}`)).join(". "),
      );
    const firstPick = Object.values(picks).flat()[0];
    try {
      const e = await call("experiment_create", {
        title:
          title.value.trim() ||
          `${m.name}: ${Object.values(files)[0]?.name || (firstPick ? titleOf(firstPick.experiment) : "experiment")}`,
        method: m.id,
        files,
        ...(Object.keys(picks).length ? { sources: picks } : {}),
      });
      show({ method: m.id, exp: e.experiment, tab: "settings", code: false });
    } catch (e) {
      say(cleanError(e)); // e.g. a pick the backend refuses, said in plain words
    }
  };
  put(
    view,
    h(
      "div",
      { class: "page narrow" },
      h("h2", {}, "New experiment"),
      h("label", { class: "layr-label" }, "Title"),
      title,
      h("label", { class: "layr-label" }, "Method"),
      methodSel,
      about,
      readsLabel,
      filesBox,
      err,
      h(
        "div",
        { class: "row-end" },
        h("button", { class: "layr-button ghost", onclick: () => show({ view: "home" }) }, "Cancel"),
        h("span", { class: "layr-spacer" }),
        h("button", { class: "layr-button primary", onclick: create }, "Create"),
      ),
    ),
  );
}

// ------------------------------------------------------------------------------------------------ the workspace

// One screen for a method and an experiment of it (or the method without data): the experiment picker in the path at
// the top, the actions beside it, then the tabs. "Flow & code" is the flow with the method's code under it (folded
// away until asked for when an experiment opens); the other tabs are the experiment's: its pages, Results, Checks,
// Corrections, Settings, showing its latest run or an earlier one picked in the flow.

const TABS = [
  ["results", "Results"],
  ["checks", "Checks"],
  ["corrections", "Corrections"],
  ["settings", "Settings"],
];

// The experiment's own pages (pages.js), with the helpers they share with this file.
const PG = createPages({
  h,
  put,
  call,
  fmt,
  table,
  badge,
  openDialog,
  askReason,
  fail,
  go,
  S,
  save,
  startRun: (id) => startRun(id),
  figureOf: (a) => figureOf(a),
  human: (n) => human(n),
  kv: (k, v) => kv(k, v),
  filteredTable: (...a) => filteredTable(...a),
  token: () => token,
  sizeOf: (place) => sizeOf(place), // where a size she drags is remembered (resize.js)
});

// What an experiment reads of other experiments: the picker and its chips (sources.js); a chip opens that experiment.
const SRC = createSources({ h, put, openDialog, when, stateWord, onOpen: (id) => show({ exp: id }) });

/** Picks what one of the experiment's result inputs reads, and keeps it (the experiment isn't run by itself). */
async function pickSources(exp, inputId) {
  const inp = (exp.method_inputs || []).find((i) => i.id === inputId) || { id: inputId, label: human(inputId) };
  try {
    [EXPS, METHODS] = await Promise.all([call("experiments"), call("methods")]);
  } catch (err) {
    return fail(err);
  }
  const got = await SRC.pick({
    input: inp,
    picked: exp.sources?.[inputId] || [],
    exps: EXPS,
    methods: METHODS,
    self: exp.id,
    save: (list) => saveSources(exp, inputId, list),
  });
  if (!got) return;
  layr.ui.notify("Saved: press Run to calculate with what it reads now", "success");
  render();
}

/** Keeps what a result input reads → null, or the backend's sentence when it refuses (a loop, a missing table…). */
async function saveSources(exp, inputId, list) {
  try {
    await call("experiment_update", { experiment: exp.id, sources: { [inputId]: list } });
    return null;
  } catch (err) {
    return cleanError(err);
  }
}

/** The run the experiment's tabs show: an earlier one picked in the flow, else its latest (never a trial run). */
const runArgs = (exp) => (S.run ? { run: S.run } : exp.last_run?.id ? { run: exp.last_run.id } : { experiment: exp.id });

async function renderWork(t) {
  const same = ME && ME.method === S.method;
  const [m, all, exp, docs] = await Promise.all([
    same ? ME.m : call("method_get", { method: S.method }),
    call("experiments"),
    S.exp ? call("experiment_get", { experiment: S.exp }).catch(() => null) : null,
    toolboxList(),
  ]);
  if (t !== token) return;
  EXPS = all;
  if (S.exp && (!exp || exp.method !== m.id)) {
    // deleted meanwhile, or of another method: the method without data
    S.exp = null;
    S.run = null;
    save();
  }
  const mine = all.filter((e) => e.method === m.id);
  if (!ME || ME.method !== m.id) openWork(m, docs, mine, S.exp ? exp : null);
  ME.mine = mine;
  ME.expObj = S.exp ? exp : null;
  if (ME.exp !== S.exp) switchExperiment();
  layr.panel.setTitle(ME.expObj ? ME.expObj.title : m.name).catch(() => {});
  setCrumbs([
    { label: "Experiments", go: { view: "home" } },
    { label: m.draft_only ? `${m.name} (proposed)` : m.name },
    { node: expPicker() },
  ]);
  drawActions();
  drawStatus();
  await drawWork(t);
}

/** The experiment picker in the path: every experiment of the method, a new one, or none (just the method). */
function expPicker() {
  const sel = h(
    "select",
    { class: "layr-select crumb-pick", title: "The experiment whose data goes through the method", "aria-label": "Experiment" },
    ME.mine.map((e) =>
      h(
        "option",
        { value: e.experiment, selected: e.experiment === S.exp },
        `${e.title} · ${stateWord(e)}${outOfDate(e, ME.m.version) || e.outdated?.length ? " · out of date" : ""}`,
      ),
    ),
    ME.mine.length ? h("option", { disabled: true }, "──────────") : null,
    h("option", { value: ":new" }, "New experiment…"),
    h("option", { value: ":none", selected: !S.exp }, "No data: just the method"),
  );
  sel.addEventListener("change", () => {
    const v = sel.value;
    sel.value = S.exp || ":none"; // what is open stays shown until the change is made (or refused)
    if (v === ":new") show({ view: "new", newMethod: ME.method });
    else show({ method: ME.method, exp: v === ":none" ? null : v, ...(v === ":none" && !S.code ? { code: true } : {}) });
  });
  return sel;
}

/** One row of actions: More ▾, Export ▾, Save, Run all again (or Stop), Run. */
function drawActions() {
  if (!ME || S.view !== "work") return;
  const exp = ME.expObj;
  const running = !!exp && (exp.running || live?.exp === exp.id);
  setActions([
    h("button", { class: "layr-button ghost", onclick: (e) => moreMenu(e) }, "More ▾"),
    exp
      ? h("button", { class: "layr-button ghost", disabled: !exp.last_run, onclick: (e) => exportMenu(e, exp) }, "Export ▾")
      : null,
    h(
      "button",
      {
        class: `layr-button ${ME.dirty ? "primary" : "ghost"} save-btn`,
        disabled: !ME.dirty,
        title: ME.dirty ? "Save the code as a new version of the method (Ctrl+S)" : "No unsaved changes",
        onclick: () => saveMethod(),
      },
      "Save",
    ),
    running
      ? h("button", { class: "layr-button danger", onclick: () => call("run_stop", { experiment: exp.id }).catch(fail) }, "Stop")
      : exp?.last_run
        ? h(
            "button",
            {
              class: "layr-button ghost run-all",
              title: "Calculate every step again, reusing nothing from before",
              onclick: () => runNow({ fresh: true }),
            },
            "Run all again",
          )
        : null,
    h(
      "button",
      {
        class: `layr-button ${ME.dirty ? "" : "primary"} run-btn`,
        disabled: running || !exp,
        title: !exp
          ? "Pick an experiment first: a run calculates its data"
          : ME.dirty
            ? "Saves your change as a new version first, then runs it"
            : "Calculate the experiment (steps that didn't change are reused)",
        onclick: () => runNow(),
      },
      running ? "Running…" : "Run",
    ),
  ]);
}

/** Run (or Run all again): with unsaved code it is saved first (saving runs it). */
async function runNow({ fresh = false } = {}) {
  const exp = ME?.expObj;
  if (!exp) return;
  if (ME.dirty) return saveMethod();
  startRun(exp.id, { fresh });
}

function moreMenu(ev) {
  const exp = ME.expObj;
  const isRef = !!exp && ME.m.reference === exp.id;
  layr.ui.contextMenu(
    [
      { label: "Versions…", icon: "history", run: showVersions },
      { label: "Format the code (tidy layout)", icon: "format_align_left", run: formatCode },
      { label: "Extra Python packages…", icon: "extension", run: editPackages },
      exp
        ? {
            label: "Reference experiment: compare every change on it",
            icon: "compare_arrows",
            checked: isRef,
            run: () => setReference(isRef ? null : exp.id),
          }
        : null,
      "-",
      exp ? { label: "Copy link to this experiment", icon: "link", run: () => copyLink({ experiment: exp.id }) } : null,
      { label: "Copy link to the method", icon: "link", run: () => copyLink({ method: ME.method }) },
      "-",
      exp ? null : { label: "Folder that runs without layr (zip)", icon: "folder_zip", run: () => exportFolder({ method: ME.method }) },
      { label: "Duplicate the method…", icon: "content_copy", run: () => duplicate(ME.m) },
      { label: "Python guide", icon: "school", run: () => show({ view: "guide" }) },
    ].filter(Boolean),
    ev,
  );
}

async function setReference(experiment) {
  try {
    await call("method_reference", { method: ME.method, experiment });
    layr.ui.notify(
      experiment
        ? "Every new version of the method is compared on this experiment now"
        : "No reference experiment any more",
      "success",
    );
    await refreshMethodMeta();
  } catch (err) {
    fail(err);
  }
}

/** The tabs, the banners and the open tab's content. The Flow & code tab's flow and editor stay mounted. */
async function drawWork(t) {
  const els = ME.els;
  if (!view.contains(els.ws)) put(view, els.ws);
  const tab = drawTabs();
  put(els.wsBanners, expBanners(ME.expObj, tab));
  els.body.className = `tab-body${tab === "flow" ? " flow-tab" : ""}`;
  if (tab === "flow") {
    if (els.body.firstChild !== els.main) {
      put(els.body, els.main);
      ME.ed.view.requestMeasure();
    }
    if (els.split.classList.contains("folded") === !!S.code) codeShown(); // a link showed or folded the code
    revealTarget();
    return;
  }
  const exp = ME.expObj;
  const box = h("div", { class: "tab-in" });
  put(els.body, box);
  if (tab.startsWith("page:")) return PG.renderPage(box, exp, Number(tab.slice(5)), t, runArgs(exp));
  const fn = { results: tabResults, checks: tabChecks, corrections: tabCorrections, settings: tabSettings }[tab];
  await fn(box, exp, t);
}

/** The tab row (an experiment's tabs only with an experiment) → the tab shown. */
function drawTabs() {
  const exp = ME.expObj;
  const editing = PG.editing();
  if (editing && (!exp || editing.exp !== exp.id || S.tab !== `page:${editing.index}`)) PG.cancelEdit();
  const pageTabs = exp ? PG.tabs(exp) : [];
  if (PG.editing()?.isNew) pageTabs.push({ id: S.tab, label: PG.editing().page.title || "New page" });
  const known = ["flow", ...(exp ? [...pageTabs.map((p) => p.id), ...TABS.map(([id]) => id)] : [])];
  if (!known.includes(S.tab)) S.tab = "flow";
  const tab = S.tab;
  const flags = exp?.last_run?.summary?.flags || {};
  const proposed = (exp?.corrections || []).filter((c) => c.status === "proposed").length;
  const tabButton = (id, label, extra) =>
    h(
      "button",
      { class: `tab ${tab === id ? "active" : ""} tab-${id.replace(":", "-")}`, onclick: () => go({ tab: id }) },
      label,
      extra,
    );
  put(
    ME.els.tabs,
    tabButton("flow", "Flow & code", ME.dirty ? h("span", { class: "dirty-dot", title: "Unsaved changes to the code" }) : null),
    exp
      ? [
          pageTabs.map((p) => tabButton(p.id, p.label)),
          h(
            "button",
            {
              class: "tab tab-add",
              title: "A page of your own: numbers, tables, charts, texts, curves to review",
              onclick: () => PG.addPage(exp),
            },
            "+ Page",
          ),
          TABS.map(([id, label]) =>
            tabButton(
              id,
              label,
              id === "checks" && (flags.error || flags.warning)
                ? h("span", { class: "count" }, (flags.error || 0) + (flags.warning || 0))
                : id === "corrections" && proposed
                  ? h("span", { class: "count warn" }, proposed)
                  : null,
            ),
          ),
        ]
      : h("span", { class: "tabs-note layr-muted small" }, "Pick an experiment above to see its pages, results, checks and settings."),
  );
  return tab;
}

/** What the screen says above the open tab: a problem, a failed or missing run, unsaved code, an earlier run. */
function expBanners(exp, tab) {
  if (!exp) return [];
  const last = exp.last_run;
  const at = last?.error_at;
  const out = [];
  const link = (label, onclick) =>
    h(
      "a",
      {
        href: "#",
        onclick: (e) => {
          e.preventDefault();
          onclick();
        },
      },
      label,
    );
  if (exp.method_problem) out.push(h("div", { class: "banner danger" }, exp.method_problem));
  else if (last && last.status !== "done" && !S.run)
    out.push(
      h(
        "div",
        { class: "banner danger" },
        `The last run ${last.status === "stopped" ? "was stopped" : "failed"}: `,
        last.error || "",
        at?.line
          ? [" ", link(`line ${at.line} in ${at.function}`, () => show({ fn: at.function, line: at.line }))]
          : null,
      ),
    );
  else if (!last) out.push(h("div", { class: "banner" }, "Not calculated yet. Check the settings, then press Run."));
  else if (exp.outdated?.length && !S.run)
    // what it reads changed since its last run (it isn't run by itself: one click on Run)
    out.push(
      h(
        "div",
        { class: "banner outdated" },
        h("b", {}, "Out of date: "),
        "what it reads changed since its last run. Press Run to calculate it with what they hold now.",
        h(
          "ul",
          { class: "outdated-why" },
          exp.outdated.map((o) =>
            h(
              "li",
              {},
              EXPS.some((x) => x.experiment === o.experiment) ? link(o.title, () => show({ exp: o.experiment })) : h("b", {}, o.title),
              `: ${o.reason}`,
            ),
          ),
        ),
      ),
    );
  else if (exp.changed && exp.changed > (last.started || last.finished) && !S.run)
    out.push(h("div", { class: "banner" }, "Changed since the last run: press Run to recalculate."));
  else if (!S.run && !exp.method_version && outOfDate(exp, ME?.m.version))
    out.push(
      h(
        "div",
        { class: "banner" },
        `Calculated with version ${last.method_version} of the method; it is at version ${ME.m.version} now. ` +
          "Press Run to bring the results up to date.",
      ),
    );
  if (ME?.m.draft && !ME.m.draft_only && tab !== "flow")
    out.push(
      h(
        "div",
        { class: "banner quiet" },
        "Claude proposed a change to the method. ",
        link("Review it in Flow & code", () => go({ tab: "flow" })),
      ),
    );
  if (ME?.dirty && tab !== "flow")
    out.push(
      h(
        "div",
        { class: "banner quiet" },
        "Your change of the code isn't saved: this tab shows the last real run. ",
        link("Back to the code", () => go({ tab: "flow" })),
      ),
    );
  if (S.run) {
    const follows = tab === "results" || tab === "checks" || tab === "flow" || tab.startsWith("page:");
    out.push(
      h(
        "div",
        { class: "banner earlier" },
        h("b", {}, "You are looking at an earlier run. "),
        follows
          ? "The flow, the pages, Results and Checks show it (a curve to review always shows the latest run)."
          : "This tab shows the experiment as it is now (Results and Checks show the earlier run).",
        " ",
        link("Back to the latest run", () => {
          go({ run: null });
          ME?.flow.update({ run: null });
        }),
      ),
    );
  }
  return out;
}

/** A figure a run returned, as an image's src (the flow shows it in the result's card and inside its box). */
async function figureOf({ run, output }) {
  const res = await layr.tools.call("figure", { run, output });
  const img = res.content?.find((c) => c.type === "image");
  return img ? `data:${img.mimeType};base64,${img.data}` : null;
}

/** The links a box gets: the page of the experiment that shows its table, the checks, the corrections, the file. */
function boxActions(n) {
  const exp = ME?.expObj;
  if (!exp) return [];
  if (n.folded && n.quality)
    return [{ label: "See the checks", title: "Every check with what it found", run: () => go({ tab: "checks" }) }];
  if (n.input?.kind === "corrections") return [{ label: "Open the corrections", run: () => go({ tab: "corrections" }) }];
  if (n.input?.kind === "result")
    return [
      {
        label: (n.input.sources || []).length ? "Change what it reads" : "Pick experiments",
        title: "The experiments this experiment reads, and the table of each",
        run: () => pickSources(exp, n.input.id),
      },
    ];
  if (n.input)
    return [
      {
        label: n.input.value ? "Use another file" : "Choose the file",
        title: "In the experiment's settings",
        run: () => go({ tab: "settings" }),
      },
    ];
  const page = PG.pageShowing(exp, [...(n.outs || []), n.fn ? `step:${n.fn}` : null].filter(Boolean));
  if (page)
    return [{ label: `Open ${page.title}`, title: "The page of this experiment that shows it", run: () => go({ tab: page.tab }) }];
  return [];
}

/**
 * A setting changed in a box: the experiment keeps it. It is calculated again by itself: for real, or (with unsaved
 * code) as a trial run of that code.
 */
async function settingChanged(id, value) {
  const exp = ME?.expObj;
  if (!exp) return layr.ui.notify("Pick an experiment above first: settings belong to it.", "info");
  try {
    await call("experiment_update", { experiment: exp.id, params: { [id]: value } });
    if (ME.dirty) {
      ME.trialSource = null;
      recalc(ME.ed.getValue());
    } else startRun(exp.id);
  } catch (err) {
    fail(err);
    render();
  }
}

/** Another experiment's data (or none) goes through the method: unsaved code stays and is calculated on it. */
function switchExperiment() {
  ME.exp = S.exp;
  ME.trialRun = null;
  ME.trialSource = ME.dirty ? null : ME.saved;
  ME.trialNote = null;
  ME.inside = new Map();
  ME.tickets.trial++;
  const me = ME;
  ME.flow.update({ experiment: S.exp, run: S.run || null }).then(() => {
    // its latest run is of an older version of the code: the code as it is now is tried on it (steps reused), so
    // every box has its numbers
    if (ME === me && !ME.dirty && ME.flow.graph?.stale && S.autoRecalc !== false && ME.exp && !S.run && !ME.busy)
      recalc(ME.ed.getValue());
  });
  drawValues();
  drawInspector();
  if (ME.dirty && S.autoRecalc !== false && S.exp) settle();
}

/** The Methods paragraph for a paper, drafted by Claude from the method and settings and kept with the experiment. */
async function methodsParagraph(exp) {
  const ai = await checkAi();
  const kept = exp.methods_text || "";
  const choice = await openDialog({
    title: "Methods paragraph",
    body: kept
      ? [h("p", { class: "pre-line" }, kept), h("div", { class: "ai-label" }, "Drafted by Claude, kept by you.")]
      : h(
          "p",
          { class: "layr-muted" },
          ai
            ? "Claude can draft the Methods paragraph for a paper from this method's code and settings."
            : "Install the Claude Code plugin to have the Methods paragraph drafted from the code.",
        ),
    buttons: [
      { label: "Close", id: null, kind: "ghost" },
      kept ? { label: "Copy", id: "copy" } : null,
      ai ? { label: kept ? "Draft again with Claude…" : "Draft with Claude…", id: "draft", kind: "primary" } : null,
    ].filter(Boolean),
  });
  if (choice === "copy") navigator.clipboard.writeText(kept).then(() => layr.ui.notify("Copied", "success"), fail);
  if (choice === "draft") draftMethods(exp);
}

function exportMenu(ev, exp) {
  const doExport = async (format, ext) => {
    try {
      setStatus("Writing the report…");
      const out = await call("export", { experiment: exp.id, format, inline: true });
      const ref = await layr.files.saveAs({ name: out.name, types: [ext] });
      if (ref) {
        await layr.files.write(ref.id, b64ToBytes(out.data));
        layr.ui.notify(`Saved ${ref.name}`, "success");
      }
    } catch (err) {
      fail(err);
    } finally {
      setStatus("");
    }
  };
  layr.ui.contextMenu(
    [
      { label: "Report (web page, prints to PDF)", icon: "description", run: () => doExport("html", ".html") },
      { label: "Word document", icon: "article", run: () => doExport("docx", ".docx") },
      { label: "Excel workbook (every table)", icon: "table_chart", run: () => doExport("xlsx", ".xlsx") },
      { label: "Methods paragraph for a paper…", icon: "edit_note", run: () => methodsParagraph(exp) },
      "-",
      {
        label: "Folder that runs without layr (zip)",
        icon: "folder_zip",
        run: () => exportFolder({ experiment: exp.id }),
      },
      { label: "Open this run in a notebook (JupyterLab)", icon: "data_object", run: () => openNotebook(exp) },
    ],
    ev,
  );
}

async function exportFolder(args) {
  try {
    setStatus("Writing the folder…");
    const out = await call("method_export", { ...args, inline: true });
    const ref = await layr.files.saveAs({ name: out.name, types: [".zip"] });
    if (ref) {
      await layr.files.write(ref.id, b64ToBytes(out.data));
      layr.ui.notify(`Saved ${ref.name}: unzip it and run python run.py`, "success");
    }
  } catch (err) {
    fail(err);
  } finally {
    setStatus("");
  }
}

async function openNotebook(exp) {
  try {
    setStatus("Writing the notebook…");
    const nb = await call("notebook", { experiment: exp.id });
    if (nb.jupyter) {
      try {
        await layr.panels.open("lab", {}, { plugin: "jupyter", where: "right" });
        layr.ui.notify(`In JupyterLab, open ${nb.in_jupyter}`, "success");
        return;
      } catch (err) {
        console.warn("JupyterLab didn't open", err);
      }
    }
    layr.ui.notify(`The notebook is in ${nb.folder} (install the JupyterLab plugin to open it in layr).`, "info");
  } catch (err) {
    fail(err);
  } finally {
    setStatus("");
  }
}

/**
 * Calculates the experiment for real (`fresh`: every step again, nothing reused). The flow lights up the step being
 * calculated; when it is done the screen shows the new numbers and how many steps were reused.
 */
function startRun(expId, { fresh = false } = {}) {
  const mine = (live = { exp: expId, step: null });
  S.run = null;
  setStatus("Calculating…");
  const flowOf = () => (ME && ME.exp === expId ? ME.flow : null);
  // The run answers when it is done (a remembered run in well under a second); its steps light up from its events.
  // A long one answers "running" after the wait: then it is asked again every half second.
  call("run", { experiment: expId, wait_s: 30, ...(fresh ? { fresh: true } : {}) }).then(
    (r) => live === mine && (r.status === "running" || r.status === "waiting" ? poll() : done(r)),
    (err) => {
      if (live !== mine) return;
      live = null;
      flowOf()?.setRunning(null);
      setStatus("");
      fail(err);
      render();
    },
  );
  const done = (r) => {
    live = null;
    const flow = flowOf();
    if (flow) {
      flow.setRunning(null);
      // the new numbers go into the flow at once (unless unsaved code is being tried: its trial run stays shown)
      if (r.run && !S.run && r.status !== "stopped" && !ME.dirty) {
        ME.trialRun = null;
        ME.trialSource = ME.saved;
        flow.update({ run: null, refresh: true });
        valuesChanged();
        drawInspector();
      }
    }
    const hits = r.cache?.hits || 0;
    setStatus(r.status === "done" ? "" : r.status === "stopped" ? "The run was stopped" : "The run failed");
    if (r.status === "done")
      layr.ui.notify(hits ? `Calculated · ${hits} step${hits === 1 ? "" : "s"} reused from before` : "Calculated",
        "success");
    render();
  };
  const poll = async () => {
    if (live !== mine) return;
    try {
      const r = await call("run_get", { experiment: expId });
      if (r.status === "running") {
        setStatus(`Calculating: ${human(r.step) || r.message || "…"}`);
        if (r.step && r.step !== live.step) {
          live.step = r.step;
          flowOf()?.setRunning({ step: r.step });
        }
        live.timer = setTimeout(poll, 500);
        return;
      }
      done(r);
    } catch (err) {
      live = null;
      flowOf()?.setRunning(null);
      setStatus("");
      fail(err);
    }
  };
  render();
}

function human(name) {
  const s = String(name || "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function kindBadge(kind) {
  return h("span", { class: `kind ${kind || ""}` }, kind || "");
}

// The step that made a saved table, from its file name (steps/03_scale_scores.csv).
function stepOfFile(file) {
  const m = /steps\/\d+_(.+?)(_in_.+|_\d+)?\.csv$/.exec(file || "");
  return m ? m[1] : null;
}

function tableChips(inputs, output, fnName, exp) {
  const open = (step) => exp && step && show({ exp: exp.id, method: ME?.method, result: `step:${step}` });
  const chips = [];
  for (const [k, v] of Object.entries(inputs || {}))
    if (v && v.kind === "table")
      chips.push(
        h(
          "button",
          {
            class: "layr-button small",
            title: (v.columns || []).join(", "),
            disabled: !exp || !stepOfFile(v.file),
            onclick: () => open(stepOfFile(v.file)),
          },
          `${k}: ${v.rows} rows × ${(v.columns || []).length}`,
        ),
      );
  const takes = chips.length ? [h("h4", {}, "Takes"), h("div", { class: "gives" }, chips)] : [];
  const gives =
    output && output.kind === "table"
      ? [
          h("h4", {}, "Gives"),
          h(
            "div",
            { class: "gives" },
            h(
              "button",
              { class: "layr-button small", title: (output.columns || []).join(", "), onclick: () => open(fnName) },
              `a table: ${output.rows} rows × ${(output.columns || []).length} columns`,
            ),
          ),
        ]
      : [];
  return [...takes, ...gives];
}

function codeBlock(code) {
  const pre = highlight(code || "");
  pre.classList.add("code-block");
  return pre;
}

async function draftMethods(exp) {
  try {
    setStatus("Claude is writing the Methods paragraph…");
    const text = await askClaude("methods", { experiment: exp.id });
    setStatus("");
    const ta = h("textarea", { class: "layr-textarea", rows: 9 }, text);
    const keep = await openDialog({
      title: "Methods paragraph (draft by Claude)",
      body: [
        h("div", { class: "ai-label" }, "Drafted by AI from the method's code and settings: check every statement."),
        ta,
      ],
      buttons: [
        { label: "Close", id: false, kind: "ghost" },
        { label: "Keep with the experiment", kind: "primary", value: () => ta.value },
      ],
    });
    if (keep) {
      await call("experiment_update", { experiment: exp.id, methods_text: keep });
      render();
    }
  } catch (err) {
    setStatus("");
    fail(err);
  }
}

function kv(k, v) {
  return h("div", { class: "kv-row" }, h("span", { class: "layr-muted" }, k), h("span", {}, v));
}

// ---------- Results tab: a list of everything the run returned; only the one picked is shown beside it

/**
 * The entries of the Results list for a run: [{id, group, kind, label, sub}]. The single values all together
 * (":numbers"), then each text, each table, each figure, then each step's table ("step:<function>").
 */
function resultEntries(run) {
  const outs = run.outputs || {};
  const names = Object.keys(outs);
  const of = (kind) => names.filter((n) => outs[n].kind === kind);
  const label = (n) => outs[n].label || human(n);
  const size = (rows, cols) => `${rows ?? "?"} row${rows === 1 ? "" : "s"} × ${cols?.length ?? 0} column${cols?.length === 1 ? "" : "s"}`;
  const out = [];
  const values = of("value");
  if (values.length)
    out.push({ id: ":numbers", group: "", kind: "numbers", label: "Numbers", sub: `${values.length} value${values.length === 1 ? "" : "s"}` });
  for (const n of of("text")) out.push({ id: n, group: "Texts", kind: "text", label: label(n), sub: `${outs[n].chars} characters` });
  for (const n of of("table")) out.push({ id: n, group: "Tables", kind: "table", label: label(n), sub: size(outs[n].rows, outs[n].columns) });
  for (const n of of("figure")) out.push({ id: n, group: "Figures", kind: "figure", label: label(n), sub: "figure" });
  const seen = new Set();
  for (const s of run.steps || [])
    if (s.table && s.function && !seen.has(s.function)) {
      seen.add(s.function);
      out.push({ id: `step:${s.function}`, group: "Each step's table", kind: "step", label: human(s.function), sub: size(s.table.rows, s.table.columns) });
    }
  return out;
}

async function tabResults(body, exp, t) {
  if (!exp.last_run || (exp.last_run.status !== "done" && !S.run)) {
    put(body, h("div", { class: "layr-empty" }, "No results yet: run the experiment."));
    return;
  }
  const run = await call("run_get", runArgs(exp));
  if (t !== token) return;
  const outs = run.outputs || {};
  const entries = resultEntries(run);
  if (!entries.length) {
    put(body, h("div", { class: "layr-empty" }, "This run returned nothing to show."));
    return;
  }
  let pick = S.result?.[exp.id];
  // a number to show highlighted (a link to it, "Open it there"); a single value is shown among the numbers
  const target = S.target?.exp === exp.id ? S.target : null;
  S.target = null;
  let value = null;
  if (outs[pick]?.kind === "value") {
    value = pick;
    pick = ":numbers";
  }
  if (!entries.some((e) => e.id === pick))
    pick = (entries.find((e) => e.kind === "numbers") || entries.find((e) => e.kind === "table") || entries[0]).id;
  const shown = h("div", { class: "result-shown" });
  const list = h("div", { class: "result-list", role: "listbox", "aria-label": "What the run returned" });
  const choose = (id, focus = {}) => {
    S.result = { ...S.result, [exp.id]: id };
    save();
    for (const el of list.querySelectorAll(".result-item")) {
      el.classList.toggle("active", el.dataset.id === id);
      el.setAttribute("aria-selected", el.dataset.id === id ? "true" : "false");
    }
    drawResult(shown, exp, run, entries.find((e) => e.id === id), outs, t, focus);
  };
  let group = null;
  for (const e of entries) {
    if (e.group && e.group !== group) list.append(h("div", { class: "result-group" }, e.group));
    group = e.group;
    list.append(
      h(
        "button",
        {
          class: `result-item kind-${e.kind}`,
          role: "option",
          "data-id": e.id,
          title: e.label,
          onclick: () => choose(e.id),
        },
        h("span", { class: "result-label" }, e.label),
        h("span", { class: "result-sub" }, e.sub),
      ),
    );
  }
  // the list's right edge is dragged to make it wider (the list scrolls inside a frame that holds the strip)
  const side = h("div", { class: "result-side" }, list);
  const grid = h("div", { class: "results" }, side, shown);
  put(body, grid);
  resizable(side, {
    edge: "right",
    min: 150,
    max: () => Math.max(150, grid.clientWidth - 320),
    enabled: () => sideBySide(),
    size: sizeOf("results.list"),
    apply: (px) => (px == null ? grid.style.removeProperty("--list-w") : grid.style.setProperty("--list-w", `${px}px`)),
    title: "Drag to make the list wider or narrower · double-click for the usual width",
  });
  choose(pick, { target: target && target.result === pick ? target : null, value });
}

/** A short-lived highlight on an element (a linked number), scrolled into the middle of the view. */
function spotlight(el) {
  if (!el) return;
  el.classList.add("cell-focus");
  requestAnimationFrame(() => el.scrollIntoView({ block: "center", inline: "center" }));
  setTimeout(() => el.classList.remove("cell-focus"), 6000);
}

/**
 * The picked entry of the Results list, drawn into `box`. focus: {target: {row, column, why}} a number of the table
 * to show highlighted (a link to it), {value: name} a single value to highlight among the numbers.
 */
async function drawResult(box, exp, run, entry, outs, t, focus = {}) {
  const mine = (box.dataset.ticket = String(Number(box.dataset.ticket || 0) + 1));
  const stale = () => t !== token || box.dataset.ticket !== mine;
  const linkButton = (args, what) =>
    h(
      "button",
      {
        class: "layr-button small ghost result-link",
        title: `Copy a link to ${what} for a Wiki page (pasted there, it shows it and stays up to date)`,
        onclick: () => copyLink(args),
      },
      "Copy link",
    );
  const head = (title, sub, link = true) =>
    h(
      "div",
      { class: "result-head" },
      h("h3", {}, title),
      sub ? h("span", { class: "layr-muted small" }, sub) : null,
      link ? [h("span", { class: "layr-spacer" }), linkButton({ experiment: exp.id, result: entry.id }, "it")] : null,
    );
  put(box, head(entry.label, entry.sub, false), h("span", { class: "layr-spinner" }));
  try {
    if (entry.kind === "numbers") {
      const values = Object.keys(outs).filter((n) => outs[n].kind === "value");
      const cards = values.map((n) =>
        h(
          "div",
          {
            class: "layr-card value-card",
            "data-name": n,
            oncontextmenu: (e) =>
              layr.ui.contextMenu(
                [{ label: "Copy link to this number", icon: "link", run: () => copyLink({ experiment: exp.id, result: n }) }],
                e,
              ),
          },
          h("div", { class: "value-top" }, h("div", { class: "layr-muted small" }, outs[n].label || human(n)), linkButton({ experiment: exp.id, result: n }, "this number")),
          h("div", { class: "big-number" }, PG.valueText(outs[n].value)),
        ),
      );
      put(box, head("Numbers", entry.sub, false), h("div", { class: "values" }, cards));
      if (focus.value) spotlight(cards[values.indexOf(focus.value)]);
    } else if (entry.kind === "text") {
      const got = await call("result_value", { run: run.run, output: entry.id });
      if (stale()) return;
      put(box, head(entry.label, entry.sub), h("pre", { class: "text-result" }, got.text ?? ""));
    } else if (entry.kind === "figure") {
      const src = await figureOf({ run: run.run, output: entry.id });
      if (stale()) return;
      put(
        box,
        head(entry.label),
        src ? h("img", { src, class: "fig", alt: entry.label }) : h("div", { class: "layr-muted" }, "This figure can't be shown."),
      );
    } else {
      let data = await call("result_table", { run: run.run, table: entry.id, limit: 2000 });
      if (stale()) return;
      // a linked number: its row, even past the rows read at first
      const target = focus.target;
      let cell = null;
      if (target?.row) {
        let r = data.rows.find((x) => rowHas(x, target.row));
        if (!r && data.total > data.rows.length) {
          const more = await call("result_table", { run: run.run, table: entry.id, where: target.row, limit: 2 }).catch(() => null);
          if (stale()) return;
          if (more?.rows?.length === 1) {
            r = more.rows[0];
            data = { ...data, rows: [...data.rows, r] };
          }
        }
        if (r) cell = { row: r, column: target.column, why: target.why };
        else
          layr.ui.notify(
            `No row of ${entry.label} has ${Object.entries(target.row).map(([k, v]) => `${k} ${v}`).join(", ")} any more.`,
            "warning",
          );
      }
      const tbox = h("div", {});
      put(
        box,
        head(entry.label, entry.kind === "step" ? "the table this step gave" : null),
        h(
          "div",
          { class: "layr-muted small" },
          "Click a number to see how it was calculated; right-click a cell to copy a link to it (for a Wiki page).",
        ),
        tbox,
      );
      filteredTable(tbox, exp, entry.id, data, null, cell);
    }
  } catch (err) {
    if (!stale()) put(box, head(entry.label, null, false), h("div", { class: "layr-error" }, cleanError(err)));
  }
}

/** A table cell's right-click menu: how its number was calculated, a link to it. */
function cellMenu(exp, tableName, row, column, rows, ev) {
  const number = typeof row[column] === "number";
  layr.ui.contextMenu(
    [
      number ? { label: "How was this number calculated?", icon: "functions", run: () => showProvenance(exp, tableName, row, column, rows) } : null,
      {
        label: number ? "Copy link to this number" : "Copy link to this value",
        icon: "link",
        run: () => copyCellLink(exp, tableName, row, column, rows),
      },
    ].filter(Boolean),
    ev,
  );
}

/**
 * A table with a filter for each column of a few repeated texts, every number clickable (how was it calculated), a
 * right-click on a cell for its menu. focus: {row (one of data.rows), column, why}: that cell is scrolled into view and
 * highlighted for a few seconds, with no filter on (why: its "how was it calculated" opens too).
 */
function filteredTable(box, exp, tableName, data, only, focus = null) {
  const filterCols = (
    only ||
    data.columns.filter((c) => {
      const vals = new Set(data.rows.map((r) => r[c]));
      return vals.size >= 2 && vals.size <= 30 && vals.size < data.rows.length && data.rows.some((r) => typeof r[c] === "string");
    })
  ).slice(0, 6);
  const filt = {};
  const draw = () => {
    const rows = data.rows.filter((r) => Object.entries(filt).every(([c, v]) => !v || String(r[c]) === v));
    put(
      box,
      h(
        "div",
        { class: "filters" },
        filterCols.map((c) =>
          h(
            "select",
            {
              class: "layr-select small",
              onchange: (e) => {
                filt[c] = e.target.value;
                draw();
              },
            },
            h("option", { value: "" }, `All ${c}`),
            [...new Set(data.rows.map((r) => String(r[c] ?? "")))]
              .sort()
              .map((v) => h("option", { value: v, selected: filt[c] === v }, v || "(empty)")),
          ),
        ),
        h("span", { class: "layr-muted small" }, `${rows.length} of ${data.total} rows`),
      ),
      table(rows, data.columns, {
        onCell: (r, c) => showProvenance(exp, tableName, r, c, data.rows),
        onMenu: (r, c, e) => cellMenu(exp, tableName, r, c, data.rows, e),
        focus: first ? focus : null,
      }),
    );
  };
  let first = !!focus;
  draw();
  if (!first) return;
  first = false;
  spotlight(box.querySelector(".cell-focus"));
  if (focus.why) showProvenance(exp, tableName, focus.row, focus.column, data.rows);
}

/** The columns that pick out one row, to ask how one of its numbers was calculated: texts first, then whole numbers,
 * each kept only when it narrows the rows down. */
function rowKeys(rows, row, column) {
  const cand = Object.keys(row).filter(
    (k) => k !== column && row[k] !== null && row[k] !== "" && (typeof row[k] === "string" || Number.isInteger(row[k])),
  );
  cand.sort((a, b) => (typeof row[b] === "string") - (typeof row[a] === "string"));
  const keys = [];
  let left = rows;
  for (const k of cand) {
    if (left.length <= 1) break;
    const next = left.filter((r) => String(r[k]) === String(row[k]));
    if (next.length < left.length) {
      keys.push(k);
      left = next;
    }
  }
  return keys.length ? keys : cand.slice(0, 3);
}

async function showProvenance(exp, tableName, row, column, rows) {
  const keys = rowKeys(rows || [row], row, column);
  const where = Object.fromEntries(keys.map((k) => [k, row[k]]));
  try {
    const p = await call("provenance", { ...runArgs(exp), table: tableName, where, column });
    const chain = p.chain || [];
    // the chain's last step can be a number that came in from another experiment: open it there, and how it came
    const source = (c, i) =>
      h(
        "div",
        { class: "prov-step prov-source" },
        h(
          "div",
          {},
          h("span", { class: "stepnum" }, i + 1),
          h("b", {}, "From the experiment ", c.title || c.experiment),
          " ",
          kindBadge("source"),
          " = ",
          h("b", {}, fmt(c.value)),
        ),
        h(
          "div",
          { class: "layr-muted small" },
          c.sentence ||
            [`its table ${tableWord(c.result)}`, ...Object.entries(c.where || {}).map(([k, v]) => `${k} ${v}`), c.column]
              .filter(Boolean)
              .join(" · "),
        ),
        h(
          "button",
          {
            class: "layr-button small prov-open",
            title: "Open that experiment's table at this number, and how it was calculated there",
            onclick: () => {
              dialog.close();
              show({
                exp: c.experiment,
                result: c.result,
                ...(c.where && Object.keys(c.where).length && c.column ? { cell: { row: c.where, column: c.column }, why: true } : {}),
              });
            },
          },
          "Open it there",
        ),
      );
    const body = chain.length
      ? chain.map((c, i) =>
          c.kind === "source" ? source(c, i) : h(
            "div",
            { class: `prov-step level-${Math.min(c.level || 0, 4)}` },
            h(
              "div",
              {},
              h("span", { class: "stepnum" }, i + 1),
              h("b", {}, human(c.function)),
              " ",
              kindBadge(c.kind),
              c.field ? h("span", { class: "layr-muted small" }, ` (its ${c.field})`) : null,
              c.row != null ? h("span", { class: "layr-muted small" }, ` (row ${c.row + 1} of a column)`) : null,
              " = ",
              h("b", {}, fmt(c.value)),
            ),
            c.sentence ? h("div", { class: "layr-muted small" }, c.sentence) : null,
            (c.latex_numbers || []).length
              ? h(
                  "div",
                  { class: "example" },
                  c.latex_numbers.map((l) => tex(l, true)),
                )
              : (c.formulas || []).map((f) => h("div", { class: "formula" }, tex(f.latex, true))),
            h(
              "div",
              { class: "prov-inputs small" },
              Object.entries(c.inputs || {})
                .filter(([, v]) => v !== null && typeof v !== "object")
                .map(([k, v]) => h("span", {}, h("code", {}, k), " ", fmt(v))),
            ),
          ),
        )
      : h("p", { class: "layr-muted" }, p.note || "No recorded formula returned this number.");
    await openDialog({
      title: `How ${column} = ${fmt(row[column])} was calculated`,
      body: [h("div", { class: "layr-muted small" }, keys.map((k) => `${k} ${row[k]}`).join(" · ")), body],
      buttons: [
        {
          label: "Copy link to this number",
          kind: "ghost",
          title: "For a Wiki page: pasted there, it shows this number and says when it changed",
          value: () => {
            copyCellLink(exp, tableName, row, column, rows);
            return undefined; // the dialog stays open
          },
        },
        { label: "Close", id: null },
      ],
    });
  } catch (err) {
    fail(err);
  }
}

// ---------- Checks tab

const FLAG_CORE = ["level", "category", "message"];

async function tabChecks(body, exp, t) {
  if (!exp.last_run || exp.last_run.status !== "done") {
    put(body, h("div", { class: "layr-empty" }, "No checks yet: run the experiment."));
    return;
  }
  const fl = await call("result_table", { ...runArgs(exp), table: "flags", limit: 2000 }).catch(() => null);
  if (t !== token) return;
  if (!fl) {
    put(
      body,
      h(
        "div",
        { class: "layr-empty" },
        "This method has no quality checks. A method makes them by returning a table called flags (toolbox: flag, " +
          "flags_table).",
      ),
    );
    return;
  }
  const groups = {};
  for (const f of fl.rows) (groups[f.category] ||= []).push(f);
  const order = { error: 0, warning: 1, info: 2 };
  const about = (f) => Object.keys(f).filter((k) => !FLAG_CORE.includes(k) && f[k] !== null && f[k] !== "");
  const review = PG.reviewFor(exp, fl.columns);
  const reviewTitle = review ? exp.method_pages[Number(review.tab.slice(5))]?.title : null;
  const open = (f) => {
    const item = review ? f[review.card.item] : null;
    if (item == null || item === "") return null;
    const label = review.card.label ? f[review.card.label] || null : null;
    return () => go({ tab: review.tab, focus: { item, label } });
  };
  put(
    body,
    h(
      "div",
      { class: "page wide" },
      h(
        "p",
        { class: "layr-muted" },
        "Nothing is changed by these checks: they say what to look at.",
        review ? ` Click one to open it on the page ${reviewTitle}.` : "",
      ),
      fl.rows.length ? null : h("div", { class: "layr-empty" }, "Every check passed."),
      Object.entries(groups)
        .sort((a, b) => order[a[1][0].level] - order[b[1][0].level])
        .map(([cat, rows]) =>
          h(
            "div",
            { class: "check-group" },
            h("h4", {}, cat, " ", h("span", { class: "layr-muted" }, `(${rows.length})`)),
            h(
              "ul",
              { class: "checks" },
              rows.map((f) =>
                h(
                  "li",
                  { class: `${f.level} ${open(f) ? "click" : ""}`, onclick: open(f) },
                  badge(f.level, f.level === "error" ? "danger" : f.level === "warning" ? "warning" : ""),
                  " ",
                  about(f).map((k) => [h("b", {}, `${human(k)} ${f[k]}`), " · "]),
                  f.message,
                ),
              ),
            ),
          ),
        ),
    ),
  );
}

// ---------- Corrections tab

async function tabCorrections(body, exp, t) {
  const corr = (exp.corrections || []).slice().reverse();
  const kinds = exp.method_corrections || {};
  const decide = async (c, decision) => {
    try {
      await call("correction_decide", { experiment: exp.id, correction: c.id, decision });
      startRun(exp.id);
    } catch (err) {
      fail(err);
    }
  };
  const statusKind = { approved: "success", proposed: "warning", rejected: "", removed: "", replaced: "" };
  const show = (c) => {
    const review = PG.reviewFor(exp, Object.keys(c));
    if (!review || c[review.card.item] == null) return null;
    return () =>
      go({ tab: review.tab, focus: { item: c[review.card.item], label: review.card.label ? c[review.card.label] || null : null } });
  };
  put(
    body,
    h(
      "div",
      { class: "page wide" },
      h(
        "p",
        { class: "layr-muted" },
        "Every change to the automatic result is here, with its reason; the raw data is never changed. " +
          "An AI agent can only propose corrections: they apply once you approve them.",
      ),
      !exp.method_takes_corrections && Object.keys(kinds).length
        ? h(
            "div",
            { class: "banner" },
            "The method's recipe doesn't take corrections yet, so they would change nothing: give its recipe an input " +
              "called corrections (in the method's code) and use them in a step.",
          )
        : null,
      h(
        "div",
        { class: "section-head" },
        h(
          "button",
          { class: "layr-button primary", disabled: !Object.keys(kinds).length, onclick: () => addCorrection(exp) },
          "Add a correction…",
        ),
        h("button", { class: "layr-button ghost", onclick: () => editKinds(exp) }, "Kinds of correction…"),
        h("span", { class: "layr-muted small" }, Object.values(kinds).map((k) => k.label).join(" · ") || "No kinds yet."),
      ),
      corr.length
        ? h(
            "div",
            { class: "table-wrap" },
            h(
              "table",
              { class: "layr-table" },
              h("thead", {}, h("tr", {}, ["What", "Reason", "By", "Status", ""].map((c) => h("th", {}, c)))),
              h(
                "tbody",
                {},
                corr.map((c) =>
                  h(
                    "tr",
                    { class: c.status === "approved" || c.status === "proposed" ? "" : "dim" },
                    h("td", {}, c.what || c.kind),
                    h("td", {}, c.reason),
                    h("td", { class: "layr-muted small" }, c.by, h("br"), when(c.at)),
                    h("td", {}, badge(c.status, statusKind[c.status])),
                    h(
                      "td",
                      { class: "nowrap" },
                      c.status === "proposed"
                        ? [
                            h("button", { class: "layr-button small primary", onclick: () => decide(c, "approve") }, "Approve"),
                            h("button", { class: "layr-button small ghost", onclick: () => decide(c, "reject") }, "Reject"),
                          ]
                        : null,
                      c.status === "approved"
                        ? h("button", { class: "layr-button small ghost", onclick: () => decide(c, "remove") }, "Undo")
                        : null,
                      show(c) ? h("button", { class: "layr-button small ghost", onclick: show(c) }, "Show") : null,
                    ),
                  ),
                ),
              ),
            ),
          )
        : h("div", { class: "layr-empty" }, "No corrections: every number comes from the automatic method."),
    ),
  );
}

/** A correction typed in by hand: its kind, its fields, a reason. */
async function addCorrection(exp) {
  const kinds = exp.method_corrections || {};
  const st = { kind: Object.keys(kinds)[0], fields: {} };
  const box = h("div", {});
  const reason = h("textarea", { class: "layr-textarea", rows: 3, placeholder: "Why? A reviewer reads this." });
  const draw = () => {
    const k = kinds[st.kind];
    put(
      box,
      h("label", { class: "layr-label" }, "Kind"),
      h(
        "select",
        {
          class: "layr-select",
          onchange: (e) => {
            st.kind = e.target.value;
            st.fields = {};
            draw();
          },
        },
        Object.entries(kinds).map(([id, kk]) => h("option", { value: id, selected: id === st.kind }, kk.label)),
      ),
      k.help ? h("div", { class: "layr-muted small" }, k.help) : null,
      Object.entries(k.fields).map(([name, f]) => [
        h("label", { class: "layr-label" }, `${human(name)}${f.optional ? " (optional)" : ""}`),
        f.type === "boolean"
          ? h("input", { type: "checkbox", onchange: (e) => (st.fields[name] = e.target.checked) })
          : h("input", {
              class: "layr-input",
              type: f.type === "text" ? "text" : "number",
              step: f.type === "integer" ? "1" : "any",
              value: st.fields[name] ?? "",
              oninput: (e) => (st.fields[name] = e.target.value),
            }),
      ]),
      h("label", { class: "layr-label" }, "Reason"),
      reason,
    );
  };
  draw();
  const ok = await openDialog({
    title: "Add a correction",
    body: box,
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      { label: "Save", kind: "primary", value: () => (reason.value.trim() ? true : (layr.ui.notify("A reason is needed.", "error"), undefined)) },
    ],
  });
  if (!ok) return;
  try {
    await call("correction_propose", { experiment: exp.id, kind: st.kind, fields: st.fields, reason: reason.value.trim() });
    startRun(exp.id);
  } catch (err) {
    fail(err);
  }
}

/** The kinds of correction the method declares (CORRECTIONS), edited: each with its label, help and fields. */
async function editKinds(exp) {
  const kinds = structuredClone(exp.method_corrections || {});
  const list = Object.entries(kinds).map(([id, k]) => ({
    id,
    label: k.label,
    help: k.help,
    fields: Object.entries(k.fields).map(([name, f]) => ({ name, ...f })),
    key: k.key,
  }));
  const box = h("div", { class: "kinds-editor" });
  const draw = () => {
    put(
      box,
      h(
        "p",
        { class: "layr-muted small" },
        "A kind says what a correction holds (a respondent's number, a sample's name, a start and an end…). The " +
          "method's code decides what it does with the approved ones: they reach its recipe's corrections input.",
      ),
      list.map((k, i) =>
        h(
          "div",
          { class: "layr-card kind-card" },
          h(
            "div",
            { class: "kind-head" },
            h("input", { class: "layr-input", value: k.label || "", placeholder: "What it does, e.g. Leave out a respondent", oninput: (e) => (k.label = e.target.value) }),
            h("input", {
              class: "layr-input id-input",
              value: k.id,
              placeholder: "id, e.g. drop_respondent",
              title: "Its id in the code: correction[\"kind\"] == \"…\"",
              oninput: (e) => (k.id = e.target.value.trim()),
            }),
            h(
              "button",
              {
                class: "layr-button small ghost danger",
                onclick: () => {
                  list.splice(i, 1);
                  draw();
                },
              },
              "Remove",
            ),
          ),
          h("input", { class: "layr-input", value: k.help || "", placeholder: "Help (optional)", oninput: (e) => (k.help = e.target.value) }),
          k.fields.map((f, j) =>
            h(
              "div",
              { class: "field-row" },
              h("input", { class: "layr-input", value: f.name, placeholder: "field, e.g. respondent", oninput: (e) => (f.name = e.target.value.trim()) }),
              h(
                "select",
                { class: "layr-select", onchange: (e) => (f.type = e.target.value) },
                [["text", "text"], ["integer", "whole number"], ["number", "number"], ["boolean", "yes / no"]].map(([v, l]) =>
                  h("option", { value: v, selected: f.type === v }, l),
                ),
              ),
              h(
                "label",
                { class: "check" },
                h("input", { type: "checkbox", checked: !!f.optional, onchange: (e) => (f.optional = e.target.checked) }),
                " optional",
              ),
              h(
                "button",
                {
                  class: "layr-button small ghost",
                  onclick: () => {
                    k.fields.splice(j, 1);
                    draw();
                  },
                },
                "✕",
              ),
            ),
          ),
          h(
            "button",
            {
              class: "layr-button small ghost",
              onclick: () => {
                k.fields.push({ name: "", type: "text", optional: false });
                draw();
              },
            },
            "+ Field",
          ),
        ),
      ),
      h(
        "button",
        {
          class: "layr-button small",
          onclick: () => {
            list.push({ id: "", label: "", help: "", fields: [{ name: "", type: "text", optional: false }] });
            draw();
          },
        },
        "+ Kind of correction",
      ),
    );
  };
  draw();
  const value = () => {
    const out = {};
    for (const k of list) {
      if (!/^[a-z][a-z0-9_]{0,40}$/.test(k.id || "")) {
        layr.ui.notify(`"${k.id || k.label || "a kind"}": the id needs lowercase letters, digits and _ (e.g. drop_respondent).`, "error");
        return undefined;
      }
      const fields = {};
      for (const f of k.fields) if (f.name) fields[f.name] = `${f.optional ? "optional " : ""}${f.type}`;
      const item = { label: k.label || k.id, ...(k.help ? { help: k.help } : {}), fields };
      const defaultKey = Object.keys(fields).filter((n) => !fields[n].endsWith("number"));
      const key = (k.key || []).filter((n) => n in fields);
      if (key.length && key.join() !== defaultKey.join()) item.key = key;
      out[k.id] = item;
    }
    return out;
  };
  const got = await openDialog({
    title: "Kinds of correction",
    wide: true,
    body: box,
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      { label: "Save in the method", kind: "primary", value },
    ],
  });
  if (!got) return;
  try {
    const r = await call("method_declare", { method: exp.method, what: "corrections", value: got, change: "Changed the kinds of correction" });
    if (r.saved === false && !r.draft) return layr.ui.notify(r.note || "Not saved.", "error");
    layr.ui.notify(`Saved as version ${r.version} of the method`, "success");
    await refreshMethodMeta();
    render();
  } catch (err) {
    fail(err);
  }
}

// ---------- Settings tab

async function tabSettings(body, exp) {
  const params = structuredClone(exp.resolved_params || {});
  const defs = exp.method_params || [];
  const title = h("input", { class: "layr-input", value: exp.title });
  const notes = h(
    "textarea",
    { class: "layr-textarea", rows: 2, placeholder: "Notes: strain, conditions, anything a reader needs" },
    exp.notes || "",
  );
  const groups = {};
  for (const p of defs) (groups[p.group || "Settings"] ||= []).push(p);
  const fields = Object.entries(groups).map(([g, ps]) =>
    h(
      "fieldset",
      { class: "layr-card group" },
      h("legend", {}, g),
      ps.map((p) =>
        h(
          "div",
          { class: `field${p.id === S.setting ? " focus" : ""}`, id: `set-${p.id}` },
          h("label", { class: "layr-label", title: p.help || "" }, p.label, p.unit ? ` (${p.unit})` : ""),
          editor(p, params),
          p.help ? h("div", { class: "layr-muted small" }, p.help) : null,
        ),
      ),
    ),
  );
  // a result of other experiments: the experiments picked as chips (each opens it, × stops reading it), the picker
  const resultRow = (inp) => {
    // a deleted experiment is named by the title its last reading knew (the backend only has its id)
    const items = (exp.sources?.[inp.id] || []).map((s) => {
      const known = s.missing && (exp.outdated || []).find((o) => o.experiment === s.experiment)?.title;
      return known ? { ...s, title: `${known} (deleted)` } : s;
    });
    const err = h("div", { class: "layr-error small", hidden: true });
    return h(
      "div",
      { class: "file-row src-input", "data-input": inp.id },
      h("span", {}, inp.label || inp.id, ": "),
      SRC.chips(items, {
        onRemove: async (i) => {
          const left = items.filter((_, k) => k !== i).map(({ experiment, result }) => ({ experiment, result }));
          const problem = await saveSources(exp, inp.id, left);
          if (!problem) return render();
          put(err, problem);
          err.hidden = false;
        },
      }),
      h("button", { class: "layr-button small", onclick: () => pickSources(exp, inp.id) }, items.length ? "Change…" : "Pick experiments…"),
      err,
    );
  };
  const files = (exp.method_inputs || []).map((inp) => {
    if (inp.kind === "result") return resultRow(inp);
    const f = exp.inputs?.[inp.id];
    return h(
      "div",
      { class: "file-row" },
      h("span", {}, inp.label || inp.id, ": "),
      f
        ? [
            h("b", {}, f.name),
            h(
              "span",
              { class: "layr-muted small" },
              ` SHA-256 ${String(f.sha256).slice(0, 16)}… · ${(f.size / 1e6).toFixed(1)} MB`,
            ),
          ]
        : h("span", { class: "layr-muted" }, "none"),
      h(
        "button",
        {
          class: "layr-button small",
          onclick: async () => {
            try {
              const nf = await pickAndStore(inp.types || []);
              if (nf) {
                await call("experiment_update", { experiment: exp.id, files: { [inp.id]: nf } });
                render();
              }
            } catch (err) {
              fail(err);
            }
          },
        },
        f ? "Replace…" : "Choose…",
      ),
    );
  });
  const saveAll = async () => {
    try {
      const changed = {};
      for (const p of defs)
        if (JSON.stringify(params[p.id]) !== JSON.stringify(exp.resolved_params[p.id])) changed[p.id] = params[p.id];
      await call("experiment_update", { experiment: exp.id, title: title.value, notes: notes.value, params: changed });
      if (Object.keys(changed).length) startRun(exp.id); // calculated again with the new settings
      else {
        layr.ui.notify("Saved", "success");
        render();
      }
    } catch (err) {
      fail(err);
    }
  };
  put(
    body,
    h(
      "div",
      { class: "page narrow settings" },
      h("label", { class: "layr-label" }, "Title"),
      title,
      h("label", { class: "layr-label" }, "Notes"),
      notes,
      h("h4", {}, (exp.method_inputs || []).some((i) => i.kind === "result") ? "What it reads" : "Raw data"),
      files,
      h(
        "p",
        { class: "layr-muted small" },
        `Method: ${exp.method}${exp.method_version ? ` (pinned to version ${exp.method_version})` : " (latest version)"}`,
      ),
      fields,
      h(
        "div",
        { class: "row-end sticky" },
        h(
          "button",
          { class: "layr-button danger ghost", onclick: () => deleteExperiment(exp) },
          "Delete experiment",
        ),
        h("span", { class: "layr-spacer" }),
        h(
          "button",
          { class: "layr-button primary", onclick: saveAll, title: "Save, then calculate the experiment with the new settings" },
          "Save and run",
        ),
      ),
    ),
  );
  if (S.setting) {
    document.getElementById(`set-${S.setting}`)?.scrollIntoView({ block: "center" });
    S.setting = null;
  }
}

/**
 * Deletes the experiment after asking. When other experiments read its results the backend refuses, naming them:
 * that sentence is shown, with "Delete anyway" (they then show it as missing).
 */
async function deleteExperiment(exp) {
  const ok = await openDialog({
    title: "Delete this experiment?",
    body: "Its runs go too. Raw files stay in the store.",
    buttons: [
      { label: "Cancel", id: false, kind: "ghost" },
      { label: "Delete", id: true, kind: "danger" },
    ],
  });
  if (!ok) return;
  const gone = () => go({ view: "home", exp: null });
  try {
    await call("experiment_delete", { experiment: exp.id });
    return gone();
  } catch (err) {
    const refusal = cleanError(err);
    if (!/read the results of/.test(refusal)) return fail(err);
    const anyway = await openDialog({
      title: "Other experiments read it",
      body: h("p", {}, refusal.replace(/ \(force\)/, "").replace(/ \(e[0-9a-f]{8}\)/g, "")),
      buttons: [
        { label: "Keep it", id: false, kind: "ghost" },
        { label: "Delete anyway", id: true, kind: "danger" },
      ],
    });
    if (!anyway) return;
    try {
      await call("experiment_delete", { experiment: exp.id, force: true });
      gone();
    } catch (err2) {
      fail(err2);
    }
  }
}

function editor(p, params) {
  const v = params[p.id];
  const set = (x) => {
    params[p.id] = x;
  };
  if (p.type === "boolean")
    return h("input", { type: "checkbox", checked: !!v, onchange: (e) => set(e.target.checked) });
  if (p.type === "number" || p.type === "integer") {
    return h("input", {
      class: "layr-input num",
      type: "number",
      step: p.type === "integer" ? "1" : "any",
      value: v ?? "",
      placeholder: "empty: none",
      onchange: (e) => set(e.target.value === "" ? null : Number(e.target.value)),
    });
  }
  if (p.type === "list") {
    return h("input", {
      class: "layr-input",
      value: (v || []).join(", "),
      placeholder: "comma separated",
      onchange: (e) =>
        set(
          e.target.value
            .split(",")
            .map((x) => x.trim())
            .filter(Boolean)
            .map((x) => (isNaN(+x) ? x : +x)),
        ),
    });
  }
  if (p.type === "rows") {
    // a table typed in by hand, with the columns the method declares; empty optional cells are left out
    if (p.problem) return h("div", { class: "layr-error small" }, p.problem);
    const cols = p.columns || [];
    const clean = (rows) =>
      rows
        .map((r) =>
          Object.fromEntries(
            cols.filter((c) => r[c.id] !== undefined && r[c.id] !== null && r[c.id] !== "").map((c) => [c.id, r[c.id]]),
          ),
        )
        .filter((r) => Object.keys(r).length);
    return rowsEditor(
      (Array.isArray(v) ? v : []).map((r) => ({ ...r })),
      cols.map((c) => ({
        id: c.id,
        label: `${c.label || human(c.id)}${c.unit ? ` (${c.unit})` : ""}${c.optional ? " (optional)" : ""}`,
        type: c.type,
      })),
      (rows) => set(clean(rows)),
    );
  }
  if (p.type === "object") {
    return h(
      "textarea",
      {
        class: "layr-textarea mono",
        rows: 3,
        onchange: (e) => {
          try {
            set(JSON.parse(e.target.value || "null"));
          } catch {
            layr.ui.notify(`${p.label}: not valid JSON`, "error");
          }
        },
      },
      JSON.stringify(v ?? null, null, 1),
    );
  }
  return h("input", { class: "layr-input", value: v ?? "", onchange: (e) => set(e.target.value || null) });
}

/** Rows typed in by hand: cols [{id, label, type: text | number | integer | boolean}]; onChange(rows) after each edit. */
function rowsEditor(rows, cols, onChange) {
  const wrap = h("div", { class: "rows-editor" });
  const cellInput = (r, c) => {
    if (c.type === "boolean")
      return h("input", {
        type: "checkbox",
        checked: !!r[c.id],
        onchange: (e) => {
          r[c.id] = e.target.checked;
          onChange(rows);
        },
      });
    const numeric = c.type === "number" || c.type === "integer";
    return h("input", {
      class: `layr-input ${numeric ? "num" : ""}`,
      type: numeric ? "number" : "text",
      step: c.type === "integer" ? "1" : "any",
      value: r[c.id] ?? "",
      "aria-label": c.label,
      onchange: (e) => {
        r[c.id] = numeric ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value;
        onChange(rows);
      },
    });
  };
  const draw = () => {
    put(
      wrap,
      h(
        "table",
        {},
        h(
          "tr",
          {},
          cols.map((c) => h("th", {}, c.label)),
          h("th"),
        ),
        rows.map((r, i) =>
          h(
            "tr",
            {},
            cols.map((c) => h("td", {}, cellInput(r, c))),
            h(
              "td",
              {},
              h(
                "button",
                {
                  class: "layr-button small ghost icon",
                  title: "Remove this row",
                  onclick: () => {
                    rows.splice(i, 1);
                    onChange(rows);
                    draw();
                  },
                },
                "×",
              ),
            ),
          ),
        ),
      ),
      h(
        "button",
        {
          class: "layr-button small ghost",
          onclick: () => {
            rows.push({});
            draw();
          },
        },
        "+ Add a row",
      ),
    );
  };
  draw();
  return wrap;
}

// ------------------------------------------------------------------------------------------------ flow & code

// The Flow & code tab: the flow on top (editable), a bar to fold the code away or bring it back (and to drag), and
// under it the outline on the left, the code in the middle (CodeMirror 6 through editor.js) and on the right the
// function at the cursor explained (its sentence, its formula drawn from the code, the shown run's numbers) and the
// style checker's messages. Help while typing comes from the backend (Jedi); the shown run's values sit at the ends
// of the lines of the function at the cursor, its checks are folded. A change (typed, or made in the flow) is tried
// on the open experiment by itself (a trial run); Save makes a new version of the method, then the experiment runs
// for real. A change Claude proposed waits in a banner and is reviewed chunk by chunk.
//
// `ME` lives as long as the workspace shows the same method: switching experiments or tabs keeps the editor, the
// flow and unsaved code. Its parts (banners, flow, bar, outline, editor with its status line, inspector) are drawn by
// their own functions into `ME.els`.
let ME = null;
let toolboxCache = null;

async function toolboxList() {
  if (!toolboxCache)
    toolboxCache = await call("toolbox_docs").then(
      (r) => r.functions || [],
      () => [],
    );
  return toolboxCache;
}

/** Ends the workspace: the flow, the editor, its hover card and a draft review are taken off the page. */
function closeWork() {
  if (!ME) return;
  ME.review?.merge?.destroy();
  ME.flow?.destroy();
  ME.ed?.destroy();
  ME.rounds?.destroy();
  for (const r of [ME.els?.flowRz, ME.els?.outlineRz, ME.els?.inspectorRz]) r?.destroy();
  clearTimeout(ME.checkTimer);
  clearTimeout(ME.cursorTimer);
  clearTimeout(ME.metaTimer);
  ME.tickets.trial++;
  ME = null;
}

// For the end-to-end tests and for poking at it in the developer tools.
window.__lab = {
  editor: () => ME?.ed || null,
  merge: () => ME?.review?.merge || null,
  flow: () => ME?.flow || null,
  rounds: () => ME?.rounds || null,
  format: () => formatCode(),
  claudeStep: (o) => claudeStep(o),
  show: (target) => show(target),
  panel: () => ({ ...S }),
  state: () =>
    ME && {
      dirty: ME.dirty,
      trialRun: ME.trialRun,
      trialNote: ME.trialNote?.text || "",
      busy: !!ME.busy || ME.inspecting > 0,
      exp: ME.exp,
      fn: ME.fn,
      code: S.code,
      version: ME.m?.version,
    },
};

function comparisonBlock(c, lead = null) {
  if (!c) return null;
  if (c.status === "running")
    return h(
      "div",
      { class: "layr-muted small" },
      h("span", { class: "layr-spinner" }),
      lead ? ` ${lead}calculating it again…` : " Rerunning the reference experiment…",
    );
  if (c.status === "failed") return h("div", { class: "layr-error small" }, `The comparison failed: ${c.error || ""}`);
  const moved = (c.tables || []).filter((t) => t.moved);
  return h(
    "div",
    { class: "comparison" },
    h(
      "div",
      {},
      lead ? h("span", { class: "layr-muted" }, lead) : null,
      h("b", {}, c.summary || ""),
      c.experiment_title ? h("span", { class: "layr-muted" }, ` on ${c.experiment_title}`) : null,
    ),
    // folded to one line until she asks: the workspace keeps its room for the flow and the code
    moved.length
      ? h("details", { class: "cmp-tables" }, h("summary", {}, "Which numbers"), moved.map((t) =>
      h(
        "details",
        {},
        h(
          "summary",
          {},
          h("b", {}, t.table),
          ` · ${t.cells_changed || 0} numbers` +
            (t.rows_added ? ` · ${t.rows_added} rows added` : "") +
            (t.rows_removed ? ` · ${t.rows_removed} rows removed` : "") +
            (t.added ? " · new" : "") +
            (t.removed ? " · gone" : ""),
        ),
        h(
          "table",
          { class: "layr-table small" },
          (t.changes || []).map((ch) =>
            h(
              "tr",
              {},
              h("td", {}, Object.values(ch.key || {}).join(" · ")),
              h("td", {}, ch.column || ch.row || ""),
              h("td", { class: "num" }, fmt(ch.before)),
              h("td", {}, "→"),
              h("td", { class: "num" }, fmt(ch.after)),
              h(
                "td",
                { class: "num layr-muted" },
                ch.change_pct != null ? `${ch.change_pct > 0 ? "+" : ""}${fmt(ch.change_pct)} %` : "",
              ),
            ),
          ),
        ),
      ),
    ))
      : null,
  );
}

/** The workspace's method is opened: the flow and the editor are mounted, the code's parts drawn. */
function openWork(m, docs, mine, expObj) {
  closeWork();
  ME = {
    method: m.id,
    m,
    saved: m.source,
    dirty: false,
    messages: m.check || [],
    summary: m.summary,
    fn: null, // set by revealTarget from S.fn
    toolbox: docs,
    mine, // the experiments of this method
    expObj, // the open experiment (experiment_get)
    exp: S.exp, // the experiment whose data the flow and the code show
    trialRun: null, // the newest trial run of the code as it is in the editor
    trialRunSource: null, // the code that trial run calculated
    inspecting: 0, // explanations being fetched
    trialSource: m.source, // the code the shown numbers belong to (a trial run is made when it differs)
    trialNote: null,
    flowSource: m.source, // the code the flow was last drawn from
    flowSel: null,
    aiStep: null, // {fn}: a step Claude wrote, not saved yet
    ai: null,
    inside: new Map(), // "run or experiment|function" → {promise, value} of function_inside
    shownInside: null, // function_inside of the function whose values the code shows
    rounds: null, // the rounds tables of its loops (rounds.js), in roundsBox beside the code
    roundsKey: null,
    roundsBox: h("div", { class: "rounds-box" }),
    tickets: { hover: 0, values: 0, check: 0, trial: 0 },
    review: null,
    busy: false,
  };
  ME.els = workParts();
  layoutCode();
  ME.ed = mountMethodEditor(ME.els.editor, m.source);
  ME.flow = mountMethodFlow(ME.els.flow, m.source);
  drawBanners();
  drawOutline();
  drawLint();
  drawStatus();
  drawInspector();
  drawValues();
  lateCheck(m);
}

/**
 * A comparison on the reference experiment is running: its "comparison" event says when it is done. One late look
 * stays as a safety net (an event can be missed while the panel reconnects); it is not a poll.
 */
function lateCheck(m) {
  clearTimeout(ME.metaTimer);
  const latest = (m.versions || [])[m.versions.length - 1];
  if (m.draft?.comparison?.status === "running" || latest?.comparison?.status === "running")
    ME.metaTimer = setTimeout(() => refreshMethodMeta({ late: true }), 45000);
}

/** The workspace's parts, empty: each is filled by its own draw function. */
function workParts() {
  const els = {
    ws: h("div", { class: "ws" }),
    tabs: h("div", { class: "tabs", role: "tablist" }),
    wsBanners: h("div", { class: "ws-banners" }),
    body: h("div", { class: "tab-body" }),
    main: h("div", { class: "code-tab" }),
    banners: h("div", { class: "mv-banners" }),
    flow: h("div", { class: "mv-flow" }),
    bar: h("div", { class: "code-bar" }),
    outline: h("div", { class: "mv-outline" }),
    editor: h("div", { class: "mv-editor" }),
    status: h("div", { class: "mv-status small" }),
    inspector: h("div", { class: "mv-inspector" }),
  };
  els.ws.append(els.tabs, els.wsBanners, els.body);
  els.middle = h("div", { class: "mv-middle" }, els.editor, els.status);
  // the outline and the explanation scroll inside a frame that holds their resize strip
  els.outlineCol = h("div", { class: "mv-col" }, els.outline);
  els.inspectorCol = h("div", { class: "mv-col" }, els.inspector);
  els.grid = h("div", { class: "mv-grid" }, els.outlineCol, els.middle, els.inspectorCol);
  els.split = h("div", { class: "mv-split" }, els.flow, els.bar, els.grid);
  // their widths: the outline's right edge and the explanation's left edge are dragged; the code takes the rest
  const codeRoom = 320;
  const col = (el, edge, place, other, min) =>
    resizable(el, {
      edge,
      min,
      max: () => els.grid.clientWidth - other().offsetWidth - codeRoom,
      enabled: () => S.code && sideBySide(),
      size: sizeOf(place),
      apply: (px) =>
        px == null ? els.grid.style.removeProperty(`--${place.slice(5)}-w`) : els.grid.style.setProperty(`--${place.slice(5)}-w`, `${px}px`),
      title: "Drag to make it wider or narrower · double-click for the usual width",
    });
  els.outlineRz = col(els.outlineCol, "right", "work.outline", () => els.inspectorCol, 130);
  els.inspectorRz = col(els.inspectorCol, "left", "work.inspector", () => els.outlineCol, 220);
  // the bar between the flow and the code: its button folds the code away or brings it back; dragged (while the code
  // shows) it gives the flow or the code more room (remembered as a share of the tab), a double click puts it back
  const splitH = () => Math.max(1, els.split.clientHeight);
  els.flowRz = resizable(els.flow, {
    edge: "bottom",
    handle: els.bar,
    ignore: (e) => !!e.target.closest("button"),
    enabled: () => S.code && sideBySide(),
    min: 120,
    max: () => splitH() - els.bar.offsetHeight - 160,
    size: {
      get: () => (S.flowFrac == null ? null : S.flowFrac * splitH()),
      set: (px) => {
        S.flowFrac = px == null ? null : +(px / splitH()).toFixed(4);
        save();
      },
    },
    measure: () => els.flow.offsetHeight,
    apply: (px) => {
      if (!S.code) return;
      const f = px == null ? 0.42 : px / splitH();
      els.split.style.gridTemplateRows = `minmax(120px, ${(f * 100).toFixed(1)}%) auto minmax(160px, 1fr)`;
    },
    title: "Drag to give the flow or the code more room · double-click to put it back in the middle",
  });
  return els;
}

/** The flow, the bar and the code back in the tab (after a review took their place). */
function layoutCode() {
  put(ME.els.main, ME.els.banners, ME.els.split);
  applyFold();
}

/** The code shown under the flow, or folded away: then the flow fills the tab and looks inside a box in its own
 * side panel (unfolded, a box shows its function in the code). */
function applyFold() {
  const els = ME.els;
  els.split.classList.toggle("folded", !S.code);
  els.split.style.gridTemplateRows = S.code ? "minmax(120px, 42%) auto minmax(160px, 1fr)" : "minmax(0, 1fr) auto";
  els.grid.hidden = !S.code;
  if (S.code) for (const r of [els.flowRz, els.outlineRz, els.inspectorRz]) r.refresh(); // the sizes she gave them
  put(
    els.bar,
    h(
      "button",
      {
        class: "layr-button small code-toggle",
        "aria-expanded": S.code ? "true" : "false",
        title: S.code ? "Fold the code away: the flow fills the tab" : "Show the method's code under the flow",
        onclick: () => setCode(!S.code),
      },
      S.code ? "▾ Hide the code" : "▴ Show the code",
    ),
    S.code
      ? h("span", { class: "code-grip", title: "Drag to give the flow or the code more room" })
      : h("span", { class: "layr-muted small code-hint" }, "the outline, the code and what each function does"),
    !S.code && ME.dirty ? h("span", { class: "dirty small" }, "Unsaved changes") : null,
  );
  ME.flow?.setInside(S.code ? "host" : "panel");
}

function setCode(on) {
  if (!ME || S.code === !!on) return;
  S.code = !!on;
  save();
  codeShown();
}

/** The code is shown or folded as S.code says (after setCode, or a link that changed S.code: see drawWork). */
function codeShown() {
  applyFold();
  if (S.code) {
    ME.ed.view.requestMeasure();
    // what was picked in the flow while the code was folded
    drawOutline();
    drawInspector();
    drawValues();
    const line = ME.pendingLine || (ME.summary?.outline || []).find((x) => x.name === ME.fn)?.line;
    ME.pendingLine = null;
    if (line) setTimeout(() => ME?.ed.reveal(line, { select: false }), 30);
  }
}

/** The function or line a link asked for (S.fn, S.line) is shown in the code, once. */
function revealTarget() {
  if (!ME) return;
  const changed = S.fn && S.fn !== ME.fn;
  if (changed) {
    ME.fn = S.fn;
    drawOutline();
    drawInspector();
    drawValues();
  }
  const line = S.line || (changed ? (ME.summary?.outline || []).find((x) => x.name === S.fn)?.line : null);
  S.line = null;
  if (!line) return;
  if (S.code) setTimeout(() => ME?.ed.reveal(line), 30);
  else ME.pendingLine = line;
}

// ---------- the flow above the code

/**
 * Where the values shown come from: a trial run of the code in the editor, an earlier run, or the latest run; and the
 * code they belong to (`source`), so that numbers of other code are not passed off as this code's.
 */
function dataRef() {
  if (!ME?.exp) return null;
  if (ME.trialRun) return { key: ME.trialRun, args: { run: ME.trialRun }, source: ME.trialRunSource };
  if (S.run) return { key: S.run, args: { run: S.run }, source: null };
  return { key: ME.exp, args: { experiment: ME.exp }, source: ME.saved };
}

/** The code in the editor is being calculated (or will be after the pause): the numbers shown are of other code. */
function numbersComing() {
  const ref = dataRef();
  return !!ref && S.autoRecalc !== false && (ME.busy || ME.trialGoing) && ref.source !== ME.ed.getValue() && !S.run;
}

function mountMethodFlow(host, source) {
  // Once the flow has drawn for the first time, the box of the function at the cursor is selected.
  // The latest run is of an older version of the code: it is calculated as it is now (a trial run, steps reused), so
  // every box has its numbers without her pressing anything.
  const sync = (tries = 0) => {
    if (!ME || (ME.flow && ME.flow !== flow)) return;
    if (flow.graph && ME.ed) {
      cursorIn(ME.ed.cursor().line);
      if (flow.graph.stale && S.autoRecalc !== false && ME.exp && !ME.trialRun && !S.run && !ME.busy)
        recalc(ME.ed.getValue());
    } else if (tries < 80) setTimeout(() => sync(tries + 1), 250);
  };
  setTimeout(sync, 250);
  const flow = mountFlow(host, {
    call,
    tex,
    method: ME.method,
    source,
    experiment: ME.exp,
    run: S.run || null,
    editable: true,
    inside: S.code ? "host" : "panel",
    picker: "runs", // the experiment is picked in the path at the top; the flow offers its earlier runs
    claude: AI.claude,
    runNote: false, // the screen's banner says when the last run failed, with a link to the line
    figure: figureOf,
    sizes: { get: (place) => sizeOf(`flow.${place}`).get(), set: (place, px) => sizeOf(`flow.${place}`).set(px) },
    onSource: (src, info) => flowEdited(src, info),
    onSetting: (id, value) => settingChanged(id, value),
    onPick: (p) => flowPicked(p),
    onSelect: (s) => flowSelected(s),
    onOpen: (o) => flowOpened(o),
    boxActions: (n) => boxActions(n),
    copyLink: ({ fn }) => copyFunctionLink(fn),
  });
  return flow;
}

/** A change made in the flow: one step of the editor's undo, then checked and calculated like typing. */
function flowEdited(source, info) {
  if (!ME) return;
  ME.flowSource = source;
  replaceCode(source, { now: true });
  if (info?.added?.length) ME.flowSel = info.added[info.added.length - 1];
}

/** An earlier run (or the latest again) picked in the flow: the code's numbers and the other tabs follow it. */
function flowPicked({ run }) {
  if (!ME) return;
  if (run && run === ME.trialRun) return; // the trial run shown already
  S.run = run || null;
  ME.trialRun = null;
  ME.trialSource = ME.dirty ? null : ME.saved;
  ME.trialNote = null;
  save();
  valuesChanged();
  drawInspector();
  drawStatus();
  if (ME.expObj) put(ME.els.wsBanners, expBanners(ME.expObj, S.tab));
  if (ME.dirty && S.autoRecalc !== false && !run) settle();
}

/** A box was selected: its function (or its recipe line) is shown in the code (once the code is shown). */
function flowSelected({ node, fn, line }) {
  if (!ME) return;
  if (ME.selFromEditor === node) return; // the editor's cursor selected it: nothing to move
  ME.flowSel = node;
  const own = fn && (ME.summary?.outline || []).find((f) => f.name === fn && f.kind !== "recipe");
  const toolbox = !own && fn && (ME.toolbox || []).some((t) => t.name === fn);
  if (!S.code) {
    // folded: the flow's own side panel shows it; the code follows when it is shown
    if (own || toolbox) {
      ME.fn = fn;
      S.fn = fn;
      save();
    }
    ME.pendingLine = own ? own.line : line || null;
    return;
  }
  if (own) jumpTo(own.line, own.name, { focus: false });
  else if (toolbox) {
    // a ready-made step: its line in the recipe is shown, and the step itself is explained (with its calls)
    if (line) ME.ed.reveal(line, { select: false });
    ME.fn = fn;
    S.fn = fn;
    save();
    drawOutline();
    drawInspector();
    drawValues();
  } else if (line) {
    ME.ed.reveal(line);
    cursorIn(line, { fromFlow: true });
  }
}

function flowOpened(o) {
  if (!ME) return;
  if (o.what === "function") {
    setCode(true);
    if (o.created && o.fn) return cursorInFunction(o.fn);
    const own = o.fn && (ME.summary?.outline || []).find((f) => f.name === o.fn);
    const line = own?.line || (o.fn ? defLine(o.fn) : null) || o.line;
    if (line) jumpTo(line, own?.name || o.fn || null);
    return;
  }
  if (o.what === "claude") return claudeStep(o);
  if (o.what === "new-experiment") return show({ view: "new", newMethod: ME.method });
  if (o.what === "experiment") return show({ exp: o.experiment });
  if (o.what === "sources") {
    if (!ME.expObj) return layr.ui.notify("Pick an experiment above first: what it reads belongs to it.", "info");
    return pickSources(ME.expObj, o.input);
  }
  const tab = { results: "results", settings: "settings", corrections: "corrections", checks: "checks" }[o.what];
  if (!tab) return;
  if (!ME.expObj) return layr.ui.notify("Pick an experiment above first: results and settings belong to it.", "info");
  show({ tab, result: o.table || undefined, setting: o.setting || undefined });
}

/**
 * "Copy link" to a function of the method: with an experiment open, the step with that experiment's numbers;
 * without one (or `codeOnly`), its code in the method.
 */
function copyFunctionLink(fn, { codeOnly = false } = {}) {
  return copyLink(ME.expObj && !codeOnly ? { experiment: ME.expObj.id, function: fn } : { method: ME.method, function: fn });
}

/** The line of `def name(` in the editor, or null. */
function defLine(name) {
  const lines = ME.ed.getValue().split("\n");
  const i = lines.findIndex((l) => l.startsWith(`def ${name}(`));
  return i < 0 ? null : i + 1;
}

/** A new function was made (a step of her own): the cursor goes to the end of its body, ready to write. */
function cursorInFunction(name) {
  const lines = ME.ed.getValue().split("\n");
  const start = lines.findIndex((l) => l.startsWith(`def ${name}(`));
  if (start < 0) return;
  let end = start + 1;
  while (end < lines.length && (lines[end] === "" || /^\s/.test(lines[end]))) end++;
  while (end > start + 1 && !lines[end - 1].trim()) end--;
  ME.ed.reveal(end);
  const v = ME.ed.view;
  v.dispatch({ selection: { anchor: v.state.doc.line(end).to } });
  ME.ed.focus();
  ME.fn = name;
  S.fn = name;
  save();
  drawOutline();
  drawInspector();
}

const cleanError = (e) =>
  String(e?.message || e || "")
    .replace(/^Error executing tool \w+:\s*/, "")
    .replace(/^LabError:\s*/, "");

/** A short name for a new function from her sentence ("Divide each mean by…" → divide_each_mean), not yet used. */
function nameFrom(sentence) {
  const words = String(sentence || "")
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .split(/\s+/)
    .filter((w) => w && !["the", "a", "an", "of", "each", "by", "to", "and", "in", "its", "for"].includes(w));
  let base = words.slice(0, 3).join("_") || "new_step";
  if (/^\d/.test(base)) base = "step_" + base;
  const taken = new Set((ME.summary?.outline || []).map((f) => f.name));
  let name = base;
  for (let i = 2; taken.has(name) || defLine(name); i++) name = `${base}_${i}`;
  return name;
}

/**
 * "Describe it, Claude writes it": Claude writes the step from her sentence; she reviews it as a change of the code
 * (the code before, the code with the new function and its step after) and keeps what she accepts. It is in the
 * editor like any edit (not saved until she saves) and marked as written by AI until then.
 */
async function claudeStep({ wire, from, columns, sentence }) {
  const before = ME.ed.getValue();
  const name = nameFrom(sentence);
  const param = /^[A-Za-z_]\w*$/.test(wire) ? wire : "table";
  try {
    setStatus("Claude is writing the step…");
    const skeleton = await call("flow_edit", {
      source: before,
      ops: [
        { op: "new_function", name, params: [param], sentence },
        { op: "add_step", function: name, args: { [param]: from?.startsWith("in:") ? { input: wire } : { wire } } },
      ],
    });
    const p = await call("ai_prompt", {
      kind: "step",
      source: skeleton.source,
      function: name,
      inputs: { [param]: columns?.length ? columns : null },
      request: sentence,
    });
    const text = await askClaudePrompt(p);
    setStatus("");
    const code = /```(?:python)?\s*\n([\s\S]*?)```/.exec(text)?.[1]?.replace(/\s+$/, "") || "";
    if (!new RegExp(`^def ${name}\\(`, "m").test(code))
      throw new Error("Claude's answer has no function to put in: try again with another sentence.");
    const after = replaceFunction(skeleton.source, name, code);
    const note = text.replace(/```[\s\S]*?```/g, "").trim();
    reviewAiStep({ before, after, name, note, sentence });
  } catch (err) {
    setStatus("");
    fail(err);
  }
}

async function askClaudePrompt(p) {
  try {
    const result = await layr.tools.run("ask", { prompt: p.prompt, system: p.system, model: "sonnet" },
      { plugin: "claude" });
    const text = typeof result === "string" ? result : result?.text;
    if (!text?.trim()) throw new Error("Claude gave an empty answer: try again.");
    return text;
  } catch (err) {
    if (err?.code === layr.errors?.NOT_ALLOWED)
      throw new Error("Research isn't allowed to ask Claude: allow it in Settings › Tool access (Research).");
    throw err;
  }
}

/** `source` with the top-level function `name` (its def line to the line before the next top-level line) replaced. */
function replaceFunction(source, name, code) {
  const lines = source.split("\n");
  const start = lines.findIndex((l) => l.startsWith(`def ${name}(`));
  if (start < 0) return source;
  let end = start + 1;
  while (end < lines.length && (lines[end] === "" || /^\s/.test(lines[end]))) end++;
  while (end > start + 1 && !lines[end - 1].trim()) end--;
  return [...lines.slice(0, start), ...code.split("\n"), ...lines.slice(end)].join("\n");
}

/** Claude's step as a change to review, in place of the flow and the code until she decides. */
function reviewAiStep({ before, after, name, note, sentence }) {
  const box = h("div", { class: "code-review-merge" });
  const counts = h("span", { class: "layr-muted small" });
  ME.review = { merge: null, ai: true };
  const close = () => endReview();
  put(
    ME.els.main,
    h(
      "div",
      { class: "code-review" },
      h(
        "div",
        { class: "code-review-head" },
        h("div", {}, h("b", {}, `A new step written by Claude: ${name}`), h("div", { class: "layr-muted" }, sentence)),
        note ? aiAnswer(note) : null,
        h(
          "div",
          { class: "row-gap" },
          h(
            "span",
            { class: "small" },
            "Left: your code now. Right: with Claude's step. ✓ keeps a change, ✕ drops it.",
          ),
          h("span", { class: "layr-spacer" }),
          counts,
        ),
      ),
      box,
      h(
        "div",
        { class: "row-gap code-review-buttons" },
        h("button", { class: "layr-button ghost", onclick: close }, "Don't take it"),
        h("span", { class: "layr-spacer" }),
        h(
          "button",
          {
            class: "layr-button primary",
            onclick: () => {
              const merged = ME.review.merge.getResult();
              close();
              if (merged === before) return;
              ME.aiStep = { fn: name };
              replaceCode(merged, { now: true });
              drawBanners();
              const line = defLine(name);
              if (line) jumpTo(line, name);
            },
          },
          "Put it in the code",
        ),
      ),
    ),
  );
  drawBanners();
  ME.review.merge = createMerge(box, {
    before,
    after,
    onChange: (c) => (counts.textContent = `${c.accepted} kept, ${c.rejected} dropped, ${c.left} to decide`),
  });
}

// ---------- recalculating while she edits

/**
 * After a pause in typing (or at once after a change in the flow): the checker; with no errors the flow is drawn
 * from the code as it is, and (when "Recalculate by itself" is on) a trial run calculates what changed.
 */
async function settle() {
  if (!ME) return;
  const source = ME.ed.getValue();
  const fresh = await recheck();
  if (!ME || !fresh || ME.ed.getValue() !== source) return;
  const error = ME.messages.find((x) => x.level === "error");
  ME.flow?.setProblem(error ? { line: error.line, message: error.message } : null);
  if (error) {
    setTrialNote("not calculated: fix the error in the code first", "bad");
    ME.busy = false;
    return;
  }
  const trial = S.autoRecalc !== false && ME.exp && source !== ME.trialSource;
  // the boxes are drawn from the new code at once when the recipe itself changed (a step added, a wire moved);
  // a change inside a step is drawn with the trial run's numbers, a moment later, in one go
  if (source !== ME.flowSource && (!trial || recipeOf(source) !== recipeOf(ME.flowSource))) {
    ME.flowSource = source;
    ME.flow?.update({ source });
  }
  if (trial) await recalc(source);
  else if (ME.ed.getValue() === source) ME.busy = false;
}

/** The recipe function's text in `source` (its def line to the next top-level line), to see whether it changed. */
function recipeOf(source) {
  const name = ME.flow?.graph?.method?.recipe;
  const lines = String(source || "").split("\n");
  const start = name ? lines.findIndex((l) => l.startsWith(`def ${name}(`)) : -1;
  if (start < 0) return null;
  let end = start + 1;
  while (end < lines.length && (lines[end] === "" || /^\s/.test(lines[end]))) end++;
  return lines.slice(start, end).join("\n");
}

/** A trial run of `source` on the picked experiment; only the newest matters. */
async function recalc(source) {
  if (!ME?.exp) return;
  const me = ME;
  me.trialSource = source;
  me.busy = true;
  if (me.trialGoing) {
    // One trial at a time: the one going is let go (its numbers are for older code) and the newest code is
    // calculated as soon as it is back, however many pauses she made meanwhile.
    me.trialNext = source;
    me.tickets.trial++;
    setTrialNote("calculating your newest change…", "run");
    return;
  }
  me.trialGoing = true;
  try {
    await trialRun(source);
  } finally {
    me.trialGoing = false;
  }
  const next = me.trialNext;
  me.trialNext = null;
  if (ME === me && next != null && next === me.ed.getValue()) await recalc(next);
}

async function trialRun(source) {
  const exp = ME.exp;
  const n = ++ME.tickets.trial;
  setTrialNote("calculating…", "run");
  let r;
  try {
    r = await call("run", { experiment: exp, source, wait_s: 50 });
    const going = () => ME && ME.tickets.trial === n && (r.status === "waiting" || r.status === "running");
    for (let i = 0; going() && i < 200; i++) {
      if (r.status === "waiting") r = await call("run", { experiment: exp, source, wait_s: 50 });
      else {
        await new Promise((res) => setTimeout(res, 700));
        r = await call("run_get", { run: r.run }).catch(() => r);
      }
    }
  } catch (err) {
    if (ME && ME.tickets.trial === n) {
      setTrialNote(`not calculated: ${cleanError(err)}`, "bad");
      ME.busy = false;
      if (ME.flowSource !== source) {
        ME.flowSource = source;
        ME.flow?.update({ source });
      }
    }
    return;
  }
  if (!ME || ME.tickets.trial !== n || r.status === "replaced") return;
  ME.flow?.setRunning(null);
  if (r.run) {
    ME.trialRun = r.run;
    ME.trialRunSource = source;
    if (S.run) {
      // a change of the code is tried on the experiment as it is now, not on the earlier run that was picked
      S.run = null;
      save();
      if (ME.expObj) put(ME.els.wsBanners, expBanners(ME.expObj, S.tab));
    }
    ME.flowSource = source;
    // the flow, the values in the code and the explanation are fetched at once (not one after the other)
    ME.inside = new Map();
    await Promise.all([ME.flow?.update({ source, run: r.run }), drawValues(), drawInspector()]);
    if (!ME || ME.tickets.trial !== n) return;
  }
  const total = (r.cache?.hits || 0) + (r.cache?.misses || 0);
  if (r.status === "done")
    setTrialNote(
      `calculated${r.cache?.hits ? ` · ${r.cache.hits} of ${total} reused` : ""}`,
      "ok",
    );
  else {
    const at = r.summary?.failed_step;
    setTrialNote(`stopped${at ? ` at ${human(at)}` : ""}: ${cleanError(r.error?.message || r.error || r.status)}`,
      "bad");
  }
  if (ME.ed.getValue() === source) ME.busy = false;
}

function setTrialNote(text, kind) {
  if (!ME) return;
  ME.trialNote = text ? { text, kind } : null;
  drawStatus();
}

// ---------- the editor and the help while typing

/** The columns of the tables the function at the cursor held in the shown run, by name (for completion and hover). */
function knownColumns() {
  const ref = dataRef();
  const got = ME?.fn && ref ? ME.inside.get(`${ref.key}|${ME.fn}`)?.value : null;
  const out = {};
  if (!got) return out;
  const add = (name, v) => {
    if (v && typeof v === "object" && v.kind === "table" && Array.isArray(v.columns) && !out[name])
      out[name] = v.columns.map(String);
  };
  for (const [k, v] of Object.entries(got.call?.args || {})) add(k, v);
  for (const st of got.statements || [])
    for (const r of st.rounds || []) for (const [k, v] of Object.entries(r.values || {})) add(k, v);
  return out;
}

function hoverCard(r) {
  if (!r || (!r.signature && !r.sentence)) return null;
  const where =
    r.kind === "column"
      ? `a column of the table ${r.table}`
      : r.line
        ? `in this method, line ${r.line}`
        : r.module && r.module !== "this method"
          ? `from ${r.module}`
          : r.function_kind || "";
  return h(
    "div",
    { class: "code-hover" },
    r.signature ? highlight(r.signature) : null,
    r.sentence ? h("div", { class: "code-hover-sentence" }, r.sentence) : null,
    where ? h("div", { class: "layr-muted small" }, where) : null,
  );
}

function mountMethodEditor(parent, doc) {
  const latest = (kind) => ++ME.tickets[kind];
  const isLatest = (kind, n) => ME && ME.tickets[kind] === n;
  const ed = createEditor(parent, {
    doc,
    onChange: () => onMethodEdit(),
    onSave: () => saveMethod(),
    onCursor: ({ line }) => {
      clearTimeout(ME.cursorTimer);
      ME.cursorTimer = setTimeout(() => cursorIn(line), 250);
    },
    complete: async ({ source, line, column }) => {
      // An answer that comes after a newer request is dropped by the editor itself (CodeMirror aborts the old one).
      const r = await call("code_complete", { source, line, column, columns: knownColumns() });
      if (!ME) return null;
      const rank = { column: 9, own: 6, toolbox: 5, keyword: 2, builtin: 1, library: 0 };
      return (r.items || []).map((x) => ({
        label: x.label,
        kind: x.kind,
        detail: x.detail || "",
        doc: x.info || undefined,
        boost: rank[x.group] ?? 0,
        apply: x.insert && x.insert !== x.label ? x.insert : undefined,
      }));
    },
    hover: async ({ source, line, column }) => {
      const n = latest("hover");
      const r = await call("code_hover", { source, line, column, columns: knownColumns() });
      return isLatest("hover", n) ? hoverCard(r) : null;
    },
    signature: async ({ source, line, column }) => {
      const r = await call("code_signature", { source, line, column });
      const s = (r.signatures || [])[0];
      return s ? { label: s.label, active: s.index, doc: s.sentence || "" } : null;
    },
    onDefinition: (at) => goToDefinition(at),
    onRename: (at) => renameAt(at),
  });
  return ed;
}

/** The cursor is on `line`: its function is explained, and its box (a recipe line's box, or the step) is selected. */
function cursorIn(line, { fromFlow = false } = {}) {
  if (!ME) return;
  const f = (ME.summary?.outline || []).find((x) => x.line <= line && line <= x.end_line);
  if (f && f.name !== ME.fn) {
    ME.fn = f.name;
    S.fn = f.name;
    save();
    drawOutline();
    drawInspector();
    drawValues();
  }
  const g = ME.flow?.graph;
  if (fromFlow || !g || !f) return;
  const recipe = f.kind === "recipe";
  const target = recipe
    ? (g.nodes || []).find((n) => n.line <= line && line <= n.end_line)?.id
    : (g.nodes || []).find((n) => n.fn === f.name)?.id;
  if (target && target !== ME.flowSel) {
    ME.flowSel = target;
    ME.selFromEditor = target;
    ME.flow.select(target);
    ME.selFromEditor = null;
  }
}

const PAUSE_MS = 450;

/** The code became changed or saved: the actions, the tabs (their dot), the banners and the code bar say so. */
function dirtyChanged() {
  if (!ME) return;
  drawActions();
  drawBanners();
  applyFold();
  if (S.view === "work" && view.contains(ME.els.ws)) {
    drawTabs();
    if (ME.expObj) put(ME.els.wsBanners, expBanners(ME.expObj, S.tab));
  }
}

function onMethodEdit({ now = false } = {}) {
  if (!ME) return;
  const wasDirty = ME.dirty;
  ME.dirty = ME.ed.getValue() !== ME.saved;
  if (wasDirty !== ME.dirty) dirtyChanged();
  ME.busy = true;
  drawStatus();
  clearTimeout(ME.checkTimer);
  // the pause that counts as "done typing": short, since a trial run of remembered steps takes well under a second
  ME.checkTimer = setTimeout(settle, now ? 0 : PAUSE_MS);
}

/** Puts new text in the editor as one step that Ctrl+Z undoes (Format, rename, a flow edit, a copied function). */
function replaceCode(source, { now = false } = {}) {
  ME.ed.setValue(source, { keepHistory: true });
  onMethodEdit({ now });
}

async function goToDefinition(at) {
  try {
    const r = await call("code_definition", at);
    if (!ME) return;
    const d = (r.definitions || [])[0];
    if (!d) return layr.ui.notify("No definition found for this name.", "info");
    if (d.in_file && d.line) {
      ME.ed.reveal(d.line);
      ME.ed.focus();
    } else if (d.toolbox) {
      // A toolbox function: shown read only in the inspector, with "Copy into my method".
      ME.fn = d.toolbox;
      S.fn = d.toolbox;
      save();
      drawOutline();
      drawInspector();
      drawValues();
    } else layr.ui.notify(`${d.name} comes from ${d.module || "Python"}: it isn't part of this method.`, "info");
  } catch (err) {
    fail(err);
  }
}

async function renameAt(at) {
  const text = at.source.split("\n")[at.line - 1] || "";
  const left = /[A-Za-z_]\w*$/.exec(text.slice(0, at.column))?.[0] || "";
  const old = left + (/^\w*/.exec(text.slice(at.column))?.[0] || "");
  if (!old) return layr.ui.notify("Put the cursor on the name to rename.", "info");
  const input = h("input", { class: "layr-input", value: old });
  setTimeout(() => input.select(), 30);
  const name = await openDialog({
    title: `Rename ${old}`,
    body: [h("label", { class: "layr-label" }, "New name (changed everywhere in the method)"), input],
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      { label: "Rename", kind: "primary", value: () => input.value.trim() || undefined },
    ],
  });
  if (!name || name === old) return;
  try {
    const r = await call("code_rename", { ...at, new_name: name });
    if (!ME || ME.ed.getValue() !== at.source) return layr.ui.notify("The code changed meanwhile: try again.", "info");
    if (r.warning) {
      const ok = await openDialog({
        title: "Rename a setting?",
        body: r.warning,
        buttons: [
          { label: "Cancel", id: false, kind: "ghost" },
          { label: "Rename anyway", id: true, kind: "danger" },
        ],
      });
      if (!ok || ME.ed.getValue() !== at.source) return;
    }
    replaceCode(r.source);
    layr.ui.notify(`Renamed on ${(r.lines || []).length} lines: save to keep it`, "success");
  } catch (err) {
    fail(err);
  }
}

async function formatCode() {
  if (!ME) return;
  const source = ME.ed.getValue();
  try {
    const r = await call("code_format", { source });
    if (r.error) return layr.ui.notify(r.error, "error");
    if (!r.changed) return layr.ui.notify("Already tidy: nothing to change.", "info");
    if (!ME || ME.ed.getValue() !== source) return;
    replaceCode(r.source);
    layr.ui.notify("Tidied up (Ctrl+Z undoes it): save to keep it", "success");
  } catch (err) {
    fail(err);
  }
}

// ---------- this run's values in the code

/** One value summary as the short text of a chip, its dot's colour, and its hover card. */
function valueChip(name, v, runId, extra) {
  const chip = { name, type: "none", text: "", title: "" };
  const k = v && typeof v === "object" && !Array.isArray(v) ? v.kind : null;
  if (v == null) chip.text = "None";
  else if (typeof v === "number") chip.text = fmt(v);
  else if (typeof v === "string") chip.text = JSON.stringify(v.length > 40 ? v.slice(0, 40) + "…" : v);
  else if (typeof v === "boolean") chip.text = v ? "True" : "False";
  else if (k === "table") {
    chip.type = "table";
    chip.text = `table ${v.rows} × ${(v.columns || []).length}`;
    chip.title = `${name}: ${v.rows} rows`;
    chip.card = () => tableCard(v, runId);
  } else if (k === "number") chip.text = v.text ?? fmt(v.value);
  else if (k === "column") {
    chip.type = "data";
    chip.text = `column of ${v.length}`;
    chip.card = () => listCard((v.values || []).slice(0, 12), v.length);
  } else if (k === "array") {
    chip.type = "data";
    chip.text = `array ${(v.shape || []).join(" × ")}`;
    chip.card = () => listCard((v.values || []).slice(0, 12), (v.shape || []).reduce((a, b) => a * b, 1));
  } else if (k === "list" || k === "dict") {
    chip.type = "data";
    const word = k === "list" ? ["item", "items"] : ["entry", "entries"];
    chip.text = `${v.length} ${word[v.length === 1 ? 0 : 1]}`;
    chip.card = () => jsonCard(v.items);
  } else if (k === "record") {
    chip.type = "data";
    chip.text = v.type || "record";
    chip.card = () => jsonCard(v.fields);
  } else if (k === "figure") {
    chip.type = "figure";
    chip.text = "figure";
    if (v.file) chip.card = () => figureCard(v, runId);
  } else if (k === "function") chip.text = `function ${v.name || ""}`;
  else chip.text = v.text || k || "…";
  if (extra) chip.text += extra;
  return chip;
}

function rowsOf(r) {
  // value_get gives rows as records or as lists matching `columns`.
  const cols = r.columns || [];
  return (r.rows || []).map((row) => (Array.isArray(row) ? Object.fromEntries(cols.map((c, i) => [c, row[i]])) : row));
}

async function tableCard(v, runId) {
  if (!v.file || !runId) return h("div", { class: "small" }, `Columns: ${(v.columns || []).join(", ")}`);
  const r = await call("value_get", { run: runId, file: v.file, limit: 8 });
  return h(
    "div",
    { class: "value-card" },
    table(rowsOf(r), r.columns, { limit: 8 }),
    h(
      "div",
      { class: "layr-muted small" },
      `First ${Math.min(8, r.total ?? v.rows)} of ${r.total ?? v.rows} rows` +
        (r.stored === "sampled" ? " (this run kept only part of this table)" : ""),
    ),
  );
}

function listCard(values, total) {
  return h(
    "div",
    { class: "value-card small" },
    h("div", { class: "mono" }, values.map((x) => fmt(x)).join(", ") + (total > values.length ? ", …" : "")),
    h("div", { class: "layr-muted" }, `${total} values`),
  );
}

function jsonCard(x) {
  const text = JSON.stringify(x ?? null, null, 1) || "";
  return h("pre", { class: "value-card json" }, text.length > 1500 ? text.slice(0, 1500) + "\n…" : text);
}

async function figureCard(v, runId) {
  const r = await call("value_get", { run: runId, file: v.file });
  const data = r.png || r.data || r.image;
  return data ? h("img", { class: "value-fig", src: `data:image/png;base64,${data}` }) : "A figure";
}

/** function_inside for the run whose values are shown (see dataRef), fetched once per function and run. */
function insideOf(ref, fn) {
  const key = `${ref.key}|${fn}`;
  let got = ME.inside.get(key);
  if (!got) {
    got = { promise: null, value: null };
    got.promise = call("function_inside", { ...ref.args, function: fn }).then(
      (r) => (got.value = r),
      () => null,
    );
    ME.inside.set(key, got);
  }
  return got.promise;
}

/** Forgets the values (another experiment was picked, or it ran again) and draws them anew. */
function valuesChanged() {
  if (!ME) return;
  ME.inside = new Map();
  drawValues();
}

/**
 * The function at the cursor gets the shown run's values at the ends of its lines, and its checks (an `if` that only
 * stops with a message) folded. Lines are matched by their text, so values stay right after an edit above them.
 */
async function drawValues() {
  if (!ME) return;
  const n = ++ME.tickets.values;
  const fn = ME.fn;
  const f = (ME.summary?.outline || []).find((x) => x.name === fn);
  const ref = dataRef();
  if (!ref || !f) {
    ME.ed.setValues({});
    ME.ed.setFolds([]);
    ME.folds = 0;
    ME.shownInside = null;
    setRounds(null, null);
    drawStatus();
    return;
  }
  const got = await insideOf(ref, fn);
  if (!ME || ME.tickets.values !== n) return;
  ME.shownInside = got;
  setRounds(got, fn);
  paintValues();
}

/**
 * The rounds tables of the shown function's loops, in the explanation beside the code (rounds.js); a row picked there
 * is the round the code shows. Kept while the same call of the same function in the same run is shown.
 */
function setRounds(got, fn) {
  const key = got?.run ? `${got.run}|${fn}|${got.call?.id ?? ""}` : null;
  if (ME.roundsKey === key) return;
  ME.rounds?.destroy();
  ME.roundsKey = key;
  ME.rounds = key
    ? createRounds({ call, args: { run: got.run }, f: got, fmt, height: sizeOf("work.rounds"), onChange: () => paintValues() })
    : null;
  put(
    ME.roundsBox,
    ME.rounds ? [h("h4", {}, ME.rounds.count > 1 ? "Its loops, round by round" : "Its loop, round by round"), ME.rounds.el] : null,
  );
}

/** The values of the function at the cursor at the ends of its lines (see drawValues), for the rounds picked. */
function paintValues() {
  if (!ME) return;
  const got = ME.shownInside;
  const fn = ME.fn;
  const f = (ME.summary?.outline || []).find((x) => x.name === fn);
  if (!f) return;
  const doc = ME.ed.getValue().split("\n");
  const ranLines = (got?.code || "").split("\n");
  // The function's place in the editor now (from the checker's outline) against its place in the run's copy.
  const now = (ME.summary?.outline || []).find((x) => x.name === fn) || f;
  const delta = now.line - (got?.line ?? now.line);
  const ranText = (line) => ranLines[line - (got?.line ?? 0)];
  const place = (line) => {
    const want = ranText(line);
    if (want == null) return null;
    if (doc[line + delta - 1] === want) return line + delta;
    for (let i = now.line; i <= now.end_line; i++) if (doc[i - 1] === want) return i;
    return null;
  };
  const values = {};
  for (const st of got?.statements || []) {
    const at = place(st.line);
    if (!at) continue;
    const picked = ME.rounds?.at(st);
    if (picked) {
      // a line of a `for` loop: the round picked in its table beside the code
      const list = [];
      const words = roundWords(picked);
      const of = words ? ` · ${words.title}` : "";
      if (st.loop && words)
        list.push({
          name: "",
          text: words.chip,
          type: "none",
          title: `The code shows ${words.title}: pick another round in the table beside the code`,
          onClick: () => ME?.rounds?.reveal(st.line),
        });
      if (picked.kind === "full") {
        for (const name of st.names)
          if (name in (picked.round.values || {})) {
            const chip = valueChip(name, picked.round.values[name], got.run);
            list.push({ ...chip, title: (chip.title || name) + of });
          }
        if (picked.round.failed || picked.round.error)
          list.push({ name: "", text: "stopped here", type: "none", title: cleanError(picked.round.error || got.error || "The step stopped at this line.") });
      }
      if (picked.kind === "light")
        for (const [name, v] of Object.entries(picked.values)) {
          const more = cellTitle(v, fmt);
          list.push({
            name,
            text: cellText(v, fmt),
            type: cellKind(v),
            title: name + of + (more ? ` · ${more}` : "") + (more && v.preview == null ? " (whole values are kept for the first and last rounds)" : ""),
          });
        }
      if (picked.kind === "inner")
        list.push({
          name: "",
          text: `${picked.rounds} round${picked.rounds === 1 ? "" : "s"} in round ${picked.outer}`,
          type: "none",
          title: "This loop's rounds in that round: click to see them in its table",
          onClick: picked.open,
        });
      if (list.length) values[at] = list;
      continue;
    }
    const first = (st.rounds || []).find((r) => !r.failed) || (st.rounds || [])[0];
    if (!first) continue;
    const extra = st.loop || st.n > 1 ? ` · round 1 of ${st.n}` : "";
    values[at] = st.names
      .filter((name) => name in (first.values || {}))
      .map((name, i) => valueChip(name, first.values[name], got.run, i === 0 ? extra : ""));
    if (!values[at].length) delete values[at];
  }
  if (got && "returned" in got) {
    for (let i = now.end_line; i >= now.line; i--)
      if (/^\s*return\b/.test(doc[i - 1] || "")) {
        (values[i] ||= []).push({ ...valueChip("return", got.returned, got.run), name: "return" });
        break;
      }
  }
  ME.ed.setValues(values);
  const folds = [];
  for (const g of got?.guards || []) {
    const a = place(g.line);
    if (!a) continue;
    folds.push({ from: a, to: a + (g.end_line - g.line), label: `stops with a message if ${g.condition}` });
  }
  ME.ed.setFolds(folds);
  ME.folds = folds.length;
  ME.valuesNote = got ? got.note || got.values_note || "" : "";
  drawStatus();
}

// ---------- packages, the status line, the checker's marks

/** The experiments of this method other than the open one: they share its code. */
const otherExperiments = () => (ME?.mine || []).filter((e) => e.experiment !== ME.exp);

/** "Shared with 2 other experiments" in the status line: their names on hover, a click lists them to open one. */
function sharedNote() {
  const others = otherExperiments();
  if (!others.length) return null;
  return h(
    "button",
    {
      class: "linklike shared-note",
      title: `The method is shared: ${others.map((e) => e.title).join(", ")}. A saved change is theirs too (they ` +
        "show their old results until they are run again).",
      onclick: (e) =>
        layr.ui.contextMenu(
          others.map((x) => ({ label: `${x.title} · ${stateWord(x)}`, icon: "science", run: () => show({ exp: x.experiment, method: ME.method }) })),
          e,
        ),
    },
    ME.exp
      ? `Shared with ${others.length} other experiment${others.length === 1 ? "" : "s"}`
      : `Used by ${others.length} experiment${others.length === 1 ? "" : "s"}`,
  );
}

/** The method's extra Python packages (REQUIREMENTS): installed for it before its first run. */
async function editPackages() {
  if (!ME) return;
  if (ME.dirty) return layr.ui.notify("Save your changes to the code first.", "error");
  const current = ME.m?.summary?.requirements || [];
  const ta = h(
    "textarea",
    { class: "layr-textarea", rows: 5, placeholder: "One per line, e.g.\npingouin\nlifelines>=0.27" },
    current.join("\n"),
  );
  setTimeout(() => ta.focus(), 30);
  const got = await openDialog({
    title: "Extra Python packages",
    body: [
      h(
        "p",
        { class: "layr-muted" },
        "Packages from the internet (PyPI) this method imports, one per line, optionally with a version. They are " +
          "installed for this method once, before its next run, next to the plugin's own (pandas, numpy, SciPy, " +
          "statsmodels, scikit-learn, matplotlib, pyreadstat), which need no line here.",
      ),
      ta,
    ],
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      { label: "Save in the method", kind: "primary", value: () => ta.value },
    ],
  });
  if (got == null) return;
  const list = got
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter(Boolean);
  try {
    const r = await call("method_declare", { method: ME.method, what: "requirements", value: list, change: "Changed the packages" });
    if (r.saved === false && !r.draft && r.note !== "Nothing changed.") return layr.ui.notify(r.note || "Not saved.", "error");
    if (r.saved) layr.ui.notify(`Saved as version ${r.version}: the packages are installed in the background`, "success");
    await refreshMethodMeta();
  } catch (err) {
    fail(err);
  }
}

function drawStatus() {
  if (!ME) return;
  const errors = ME.messages.filter((x) => x.level === "error").length;
  const warnings = ME.messages.length - errors;
  put(
    ME.els.status,
    h(
      "span",
      { class: ME.dirty ? "dirty" : "" },
      ME.dirty
        ? "Unsaved changes"
        : ME.m.draft_only
          ? "Proposed by Claude: save to accept it"
          : `Saved · version ${ME.m.version}`,
    ),
    sharedNote(),
    ME.folds
      ? h(
          "button",
          {
            class: "linklike folds-toggle",
            title: "Checks that only stop with a message are folded to one line: click to show or hide them",
            onclick: () => ME.ed.toggleFolds(),
          },
          `${ME.folds} check${ME.folds > 1 ? "s" : ""} folded`,
        )
      : null,
    ME.valuesNote ? h("span", { class: "layr-muted", title: ME.valuesNote }, "no values inside") : null,
    h("span", { class: "layr-spacer" }),
    ME.trialNote
      ? h("span", { class: `trial-note ${ME.trialNote.kind || ""}`, title: ME.trialNote.text }, ME.trialNote.text)
      : null,
    h(
      "label",
      { class: "recalc-toggle", title: "After a change, try it on the open experiment by itself (nothing is saved)" },
      h("input", {
        type: "checkbox",
        checked: S.autoRecalc !== false,
        onchange: (e) => {
          S.autoRecalc = e.target.checked;
          save();
          if (S.autoRecalc && ME.dirty) settle();
        },
      }),
      " Recalculate by itself",
    ),
    errors ? h("span", { class: "lint-count error" }, `${errors} error${errors > 1 ? "s" : ""}`) : null,
    warnings ? h("span", { class: "lint-count warning" }, `${warnings} style note${warnings > 1 ? "s" : ""}`) : null,
    !errors && !warnings
      ? h("span", { class: "lint-count ok" }, "Style checker: all good")
      : null,
  );
}

function drawLint() {
  ME.ed.setDiagnostics(
    ME.messages.filter((x) => x.line).map((x) => ({ line: x.line, level: x.level, message: x.message })),
  );
}

/** The style checker on the code as it is; true when its answer is for the code still in the editor. */
async function recheck() {
  if (!ME) return false;
  const source = ME.ed.getValue();
  const n = ++ME.tickets.check;
  try {
    const r = await call("method_check", { source });
    if (!ME || ME.tickets.check !== n || ME.ed.getValue() !== source) return false;
    ME.messages = r.messages;
    if (r.summary) ME.summary = r.summary;
    drawLint();
    drawStatus();
    drawOutline();
    drawInspector();
    drawValues();
    return true;
  } catch (err) {
    console.warn(err);
    return false;
  }
}

function jumpTo(line, fnName, { focus = true } = {}) {
  if (fnName && fnName !== ME.fn) {
    ME.fn = fnName;
    S.fn = fnName;
    save();
    drawValues();
  }
  ME.ed.reveal(line);
  if (focus) ME.ed.focus();
  drawOutline();
  drawInspector();
}

function drawOutline() {
  const outline = ME.summary?.outline || [];
  const groups = [
    ["recipe", "Recipe"],
    ["step", "Steps"],
    ["formula", "Formulas"],
    ["helper", "Helpers"],
  ];
  const count = (name) => ME.messages.filter((x) => x.function === name).length;
  // a function's menu (right-click, or its ⋯): links to it for a Wiki page
  const menu = (e, f) => {
    e.preventDefault();
    e.stopPropagation();
    layr.ui.contextMenu(
      [
        ME.expObj
          ? { label: "Copy link to this step (with this experiment's numbers)", icon: "link", run: () => copyFunctionLink(f.name) }
          : null,
        { label: "Copy link to its code", icon: "link", run: () => copyFunctionLink(f.name, { codeOnly: true }) },
      ].filter(Boolean),
      e,
    );
  };
  put(
    ME.els.outline,
    groups.map(([kind, label]) => {
      const fns = outline.filter((f) => f.kind === kind);
      if (!fns.length) return null;
      return h(
        "div",
        { class: "ol-group" },
        h("div", { class: "ol-head" }, label, h("span", { class: "layr-muted" }, ` ${fns.length}`)),
        fns.map((f) =>
          h(
            "div",
            {
              class: `ol-item ${f.name === ME.fn ? "active" : ""}`,
              title: f.sentence || "",
              onclick: () => jumpTo(f.line, f.name),
              oncontextmenu: (e) => menu(e, f),
            },
            h("span", { class: "ol-name" }, f.name),
            count(f.name)
              ? h("span", { class: "lint-dot", title: `${count(f.name)} style notes` }, count(f.name))
              : null,
            h(
              "button",
              { class: "ol-more", title: "Copy a link to it…", "aria-label": `Links to ${f.name}`, onclick: (e) => menu(e, f) },
              "⋯",
            ),
          ),
        ),
      );
    }),
    h(
      "details",
      { class: "ol-group", open: ME.fn && !outline.some((f) => f.name === ME.fn) ? true : null },
      h("summary", { class: "ol-head" }, "Toolbox", h("span", { class: "layr-muted" }, " read only")),
      (ME.toolbox || []).map((t) =>
        h(
          "div",
          {
            class: `ol-item toolbox ${t.name === ME.fn ? "active" : ""}`,
            title: t.sentence || "",
            onclick: () => {
              ME.fn = t.name;
              S.fn = t.name;
              save();
              drawOutline();
              drawInspector();
              drawValues();
            },
          },
          h("span", { class: "ol-name" }, t.name),
        ),
      ),
    ),
  );
}

// ---------- drafts, saving, versions

function drawBanners() {
  const m = ME.m;
  const out = [];
  const d = m.draft;
  if (d && !m.draft_only && !ME.review) {
    out.push(
      h(
        "div",
        { class: "banner draft" },
        h(
          "div",
          {},
          h("b", {}, "Claude proposed a change"),
          ` (${d.by || "an AI agent"}, ${when(d.at)}): `,
          d.change || "",
          d.messages?.length ? h("span", { class: "layr-muted" }, ` · ${d.messages.length} style notes`) : null,
        ),
        comparisonBlock(d.comparison),
        h(
          "div",
          { class: "row-gap" },
          h("button", { class: "layr-button small primary", onclick: showDraft }, "Review the changes"),
          h("button", { class: "layr-button small", onclick: () => decideDraft("accept") }, "Accept all"),
          h("button", { class: "layr-button small ghost", onclick: () => decideDraft("reject") }, "Reject"),
        ),
      ),
    );
  }
  if (ME.aiStep && ME.dirty && !ME.review) {
    out.push(
      h(
        "div",
        { class: "banner draft ai-step" },
        h(
          "div",
          {},
          h("b", {}, "Written by AI (Claude): "),
          `the step ${ME.aiStep.fn} is in the code, not saved yet. Read it and check its numbers before you save.`,
        ),
        h(
          "div",
          { class: "row-gap" },
          h(
            "button",
            {
              class: "layr-button small",
              onclick: () => {
                const line = defLine(ME.aiStep.fn);
                if (line) jumpTo(line, ME.aiStep.fn);
              },
            },
            "Show it",
          ),
        ),
      ),
    );
  }
  if (m.draft_only) {
    out.push(
      h(
        "div",
        { class: "banner draft" },
        h("b", {}, "A new method Claude proposed. "),
        "Read it; accept it to use it, or change it and save.",
        h(
          "div",
          { class: "row-gap" },
          h("button", { class: "layr-button small primary", onclick: () => decideDraft("accept") }, "Accept"),
          h("button", { class: "layr-button small ghost", onclick: () => decideDraft("reject") }, "Reject"),
        ),
      ),
    );
  }
  const latest = (m.versions || [])[m.versions.length - 1];
  const seen = `${m.id}:${latest?.version}`;
  if (latest?.comparison && latest.version === m.version && !ME.review && S.seenComparison !== seen) {
    out.push(
      h(
        "div",
        { class: "banner closable" },
        h(
          "button",
          {
            class: "layr-button small ghost icon banner-close",
            title: "Close (the versions keep it)",
            onclick: () => {
              S.seenComparison = seen;
              save();
              drawBanners();
            },
          },
          "✕",
        ),
        comparisonBlock(latest.comparison, `Version ${latest.version} on the reference experiment: `),
      ),
    );
  }
  put(ME.els.banners, out);
}

async function confirmReplace() {
  if (!ME.dirty) return true;
  return openDialog({
    title: "Accept Claude's change?",
    body: "Your unsaved changes in the editor are replaced by the accepted version.",
    buttons: [
      { label: "Cancel", id: false, kind: "ghost" },
      { label: "Accept", id: true, kind: "primary" },
    ],
  });
}

async function decideDraft(decision, source) {
  try {
    if (decision === "accept" && !(await confirmReplace())) return;
    await call("draft_decide", { method: ME.method, decision, ...(source != null ? { source } : {}) });
    ME.dirty = false;
    layr.ui.notify(
      decision === "accept"
        ? source != null
          ? "Saved what you kept: it is the method's newest version now"
          : "Accepted: it is the method's newest version now"
        : "Rejected",
      "success",
    );
    if (decision === "reject" && ME.m.draft_only) {
      closeWork();
      return go({ view: "home", method: null, exp: null });
    }
    closeReview();
    await refreshMethodMeta();
    endReview();
    if (decision === "accept") newVersionSaved();
    else render();
  } catch (err) {
    fail(err);
  }
}

/**
 * Reviewing Claude's draft: the changed chunks of the code side by side (or one above the other on a narrow panel),
 * each accepted or rejected, then "Accept" saves what stands. Takes the place of the outline, editor and inspector
 * until it is closed; the editor keeps its text meanwhile.
 */
async function showDraft() {
  try {
    const d = (await call("draft_get", { method: ME.method })).draft;
    if (!d) return render();
    const before = ME.saved;
    const box = h("div", { class: "code-review-merge" });
    const counts = h("span", { class: "layr-muted small" });
    const done = h("button", { class: "layr-button primary" }, "Accept");
    ME.review = { draft: d, merge: null };
    const drawCounts = (c) => {
      const all = c.accepted + c.rejected + c.left;
      counts.textContent =
        `${all} change${all === 1 ? "" : "s"}: ${c.accepted} accepted, ${c.rejected} rejected` +
        (c.left ? `, ${c.left} not decided (count as accepted)` : "");
      done.textContent = c.rejected ? "Save what stands" : "Accept";
    };
    done.onclick = () => {
      const merged = ME.review.merge.getResult();
      decideDraft("accept", merged === d.source ? null : merged);
    };
    put(
      ME.els.main,
      h(
        "div",
        { class: "code-review" },
        h(
          "div",
          { class: "code-review-head" },
          h(
            "div",
            {},
            h("b", {}, "Claude's proposed change"),
            h("span", { class: "layr-muted" }, ` · ${d.by || "an AI agent"} · ${when(d.at)}`),
            h("div", {}, d.change || ""),
          ),
          comparisonBlock(d.comparison),
          (d.messages || []).length
            ? h(
                "ul",
                { class: "lint-list small" },
                d.messages.map((x) => h("li", { class: x.level }, `line ${x.line}: ${x.message}`)),
              )
            : null,
          h(
            "div",
            { class: "row-gap" },
            h("span", { class: "small" }, "Left: your version now. Right: Claude's. ✓ keeps a change, ✕ drops it."),
            h("span", { class: "layr-spacer" }),
            counts,
          ),
        ),
        box,
        h(
          "div",
          { class: "row-gap code-review-buttons" },
          h("button", { class: "layr-button ghost", onclick: () => endReview() }, "Back to the code"),
          h("span", { class: "layr-spacer" }),
          h("button", { class: "layr-button ghost", onclick: () => decideDraft("reject") }, "Reject it all"),
          done,
        ),
      ),
    );
    drawBanners();
    ME.review.merge = createMerge(box, { before, after: d.source, onChange: drawCounts });
    drawCounts(ME.review.merge.counts());
  } catch (err) {
    fail(err);
  }
}

function closeReview() {
  if (!ME?.review) return;
  ME.review.merge?.destroy();
  ME.review = null;
}

/** A review is closed: the flow, the bar and the code are back in the tab. */
function endReview() {
  if (!ME) return;
  closeReview();
  layoutCode();
  drawBanners();
}

/**
 * Saves the code as a new version (she says what changed); true when it was saved. The method is shared by every
 * experiment of it: the others say so until they are run again. The open experiment is then run for real by itself
 * (its steps are remembered from the trial run, so it is quick).
 */
async function saveMethod() {
  if (!ME || !ME.dirty) return false;
  const source = ME.ed.getValue();
  const others = otherExperiments();
  const note = h("input", {
    class: "layr-input",
    placeholder: "e.g. Scores from the mean instead of the sum",
    value: ME.aiStep ? `Added ${ME.aiStep.fn} (written by Claude, checked by me)` : ME.fn ? `Changed ${ME.fn}` : "",
  });
  setTimeout(() => note.select(), 30);
  const change = await openDialog({
    title: "Save a new version",
    body: [
      h("label", { class: "layr-label" }, "What changed, and why? (for the version history)"),
      note,
      others.length
        ? h(
            "p",
            { class: "layr-muted small" },
            `The method is shared with ${others.length} other experiment${others.length === 1 ? "" : "s"} (` +
              `${others.map((e) => e.title).join(", ")}): they show their old results until they are run again.`,
          )
        : null,
      ME.expObj ? h("p", { class: "layr-muted small" }, `${ME.expObj.title} is run with the new version right after.`) : null,
    ],
    buttons: [
      { label: "Cancel", id: null, kind: "ghost" },
      { label: "Save", kind: "primary", value: () => note.value.trim() || "Changed in the editor" },
    ],
  });
  if (!change) return false;
  try {
    const r = await call("method_write", { method: ME.method, source, change });
    if (!r.saved) {
      ME.messages = r.messages || ME.messages;
      drawLint();
      drawStatus();
      layr.ui.notify(r.note || "Not saved: fix the errors (marked in red next to the lines) first.", "error");
      return false;
    }
    ME.saved = source;
    ME.dirty = ME.ed.getValue() !== source;
    ME.aiStep = null;
    layr.ui.notify(
      `Saved as version ${r.version}` +
        (others.length
          ? ` · ${others.length} other experiment${others.length === 1 ? " is" : "s are"} out of date until run again`
          : ""),
      "success",
    );
    await refreshMethodMeta();
    dirtyChanged();
    newVersionSaved();
    return true;
  } catch (err) {
    fail(err);
    return false;
  }
}

/** A new version of the method was saved here (Save, an accepted draft, a version gone back to): the open
 * experiment is calculated with it, for real (its steps are remembered from the trial runs, so it is quick). */
function newVersionSaved() {
  if (ME?.expObj && !ME.dirty) startRun(ME.expObj.id);
  else render();
}

// After a save or an event: new versions, drafts and comparisons. Unsaved code stays; else the editor and the flow
// follow the saved code (an accepted draft, a version gone back to, a change made by an agent).
async function refreshMethodMeta({ late = false } = {}) {
  if (!ME) return;
  try {
    const m = await call("method_get", { method: ME.method });
    if (!ME || ME.method !== m.id) return;
    ME.m = m;
    if (!ME.dirty && m.source !== ME.ed.getValue()) {
      ME.ed.setValue(m.source, { keepHistory: true });
      ME.messages = m.check || [];
      ME.summary = m.summary;
      ME.trialSource = m.source;
      ME.flowSource = m.source;
      ME.flow.update({ source: m.source });
      drawLint();
      drawOutline();
      valuesChanged();
      drawInspector();
    }
    ME.saved = ME.dirty ? ME.saved : m.source;
    drawActions();
    drawBanners();
    drawStatus();
    if (!late) lateCheck(m);
  } catch (err) {
    console.warn(err);
  }
}

async function showVersions() {
  try {
    const versions = (await call("method_versions", { method: ME.method })).slice().reverse();
    const box = h("div", {});
    let merge = null;
    const compare = async (from, to) => {
      try {
        const [a, b] = await Promise.all([
          call("method_get", { method: ME.method, version: from }),
          call("method_get", { method: ME.method, version: to }),
        ]);
        merge?.destroy();
        const holder = h("div", { class: "versions-merge" });
        put(box, h("b", {}, `Changes in version ${to} (left: version ${from})`), holder);
        merge = createMerge(holder, { before: a.source, after: b.source, readOnly: true });
      } catch (err) {
        fail(err);
      }
    };
    const list = h(
      "ul",
      { class: "layr-list lined versions" },
      versions.map((v) =>
        h(
          "li",
          {},
          h("b", {}, `Version ${v.version}`),
          " ",
          h("span", { class: "layr-muted small" }, `${v.by || ""} ${when(v.at)}`),
          h("div", { class: "small" }, v.summary),
          v.comparison ? comparisonBlock(v.comparison) : null,
          h(
            "div",
            {},
            v.version > 1
              ? h(
                  "button",
                  {
                    class: "layr-button small ghost",
                    onclick: () => compare(versions.find((x) => x.version < v.version)?.version ?? 1, v.version),
                  },
                  "Changed lines",
                )
              : null,
            v.version !== ME.m.version
              ? h(
                  "button",
                  {
                    class: "layr-button small ghost",
                    onclick: async () => {
                      try {
                        const r = await call("method_revert", { method: ME.method, version: v.version });
                        dialog.close();
                        if (r.saved)
                          layr.ui.notify(`Back to version ${v.version}: saved as version ${r.version}`, "success");
                        ME.dirty = false;
                        await refreshMethodMeta();
                        dirtyChanged();
                        newVersionSaved();
                      } catch (err) {
                        fail(err);
                      }
                    },
                  },
                  "Go back to this",
                )
              : null,
          ),
        ),
      ),
    );
    await openDialog({ title: "Versions", body: [list, box], buttons: [{ label: "Close", id: null }], wide: true });
    merge?.destroy();
  } catch (err) {
    fail(err);
  }
}

async function drawInspector() {
  if (!ME) return;
  const box = ME.els.inspector;
  const name = ME.fn;
  const msgs = ME.messages;
  const allMessages = msgs.length
    ? [
        h("h4", {}, `Style checker (${msgs.length})`),
        h(
          "ul",
          { class: "lint-list" },
          msgs.map((x) =>
            h(
              "li",
              { class: `${x.level} click`, onclick: () => x.line && jumpTo(x.line, x.function) },
              h("b", {}, x.line ? `line ${x.line}: ` : ""),
              x.message,
            ),
          ),
        ),
      ]
    : [];
  // where the numbers come from (the experiment is picked in the path at the top)
  const shownExp = (ME.mine || []).find((e) => e.experiment === ME.exp);
  const picker = ME.exp
    ? h(
        "div",
        { class: "numbers-from small" },
        "Numbers from ",
        h("b", {}, shownExp?.title || "the open experiment"),
        h(
          "span",
          { class: "layr-muted" },
          ME.trialRun ? " (your change, tried just now)" : S.run ? " (an earlier run)" : " (its latest run)",
        ),
      )
    : h(
        "div",
        { class: "layr-muted small numbers-from" },
        (ME.mine || []).length
          ? "No data: pick an experiment at the top to see its numbers here and in the code."
          : "No experiment uses this method yet: make one (New experiment… at the top) to see numbers here.",
      );
  if (!name) {
    ME.inspectorFn = null;
    const s = ME.summary || {};
    put(
      box,
      h(
        "div",
        { class: "sheet" },
        h("h3", {}, ME.m.name),
        h("p", { class: "sentence" }, ME.m.description || ""),
        h(
          "p",
          { class: "layr-muted small" },
          `${(s.outline || []).length} functions · ${(s.settings || []).length} settings · inputs: ` +
            ((s.inputs || []).map((i) => i.id).join(", ") || "none"),
        ),
        h(
          "p",
          { class: "layr-muted" },
          "Select a box in the flow, put the cursor in a function, or pick one on the left: it is explained here.",
        ),
        picker,
        allMessages,
      ),
    );
    return;
  }
  if (ME.inspectorFn !== name)
    put(box, h("div", { class: "sheet" }, h("h3", {}, name), h("span", { class: "layr-spinner" })));
  const n = (ME.inspectorTicket = (ME.inspectorTicket || 0) + 1);
  const me = ME;
  let v;
  const coming = numbersComing();
  try {
    const ref = dataRef();
    me.inspecting++;
    v = await call("explain_function", { function: name, source: ME.ed.getValue(), ...(ref ? ref.args : {}) });
  } catch (err) {
    if (ME?.fn === name && ME.inspectorTicket === n)
      put(
        box,
        h(
          "div",
          { class: "sheet" },
          h("h3", {}, name),
          h("div", { class: "layr-error" }, String(err.message || err).replace(/^Error executing tool \w+: /, "")),
          picker,
          allMessages,
        ),
      );
    return;
  } finally {
    me.inspecting--;
  }
  if (!ME || ME.fn !== name || ME.inspectorTicket !== n) return;
  const mine = msgs.filter((x) => x.function === name);
  const parts = [
    h("h3", {}, human(name), " ", kindBadge(v.kind)),
    h(
      "div",
      { class: "layr-muted small" },
      h("code", {}, v.signature || name),
      v.line ? ` · line ${v.line}` : v.module ? ` · ${v.module}` : "",
    ),
    h("p", { class: "sentence" }, v.sentence || h("span", { class: "layr-muted" }, "(no docstring yet)")),
  ];
  if (v.text) parts.push(h("p", { class: "layr-muted small pre-line" }, v.text));
  if (v.formulas?.length) {
    parts.push(h("h4", {}, "Formula"));
    for (const f of v.formulas) parts.push(h("div", { class: "formula", title: f.code || "" }, tex(f.latex, true)));
  }
  parts.push(picker);
  if (v.step)
    parts.push(
      ...tableChips(
        v.step.inputs,
        v.step.output,
        name,
        ME.exp ? { id: ME.exp } : null,
      ),
    );
  if (coming) {
    // the numbers at hand are of other code (the saved version, or the change before): no "0 calls" for a step that
    // is new in her change; the trial run's numbers come in a moment
    parts.push(
      h("div", { class: "layr-muted small" }, h("span", { class: "layr-spinner" }), " Calculating your change…"),
    );
  } else if (v.count != null) {
    parts.push(
      h(
        "h4",
        {},
        `In the last run: ${v.count} call${v.count === 1 ? "" : "s"}${v.kept < v.count ? ` (${v.kept} kept)` : ""}`,
      ),
    );
    if (v.example?.latex?.length)
      parts.push(
        h(
          "div",
          { class: "example" },
          h(
            "div",
            { class: "flabel" },
            v.example.row != null ? `row ${v.example.row + 1} of ${v.example.column_length}` : "first call",
          ),
          v.example.latex.map((l) => tex(l, true)),
        ),
      );
    const sample = (v.sample || []).filter((c) => !c.error);
    if (sample.length && v.kind !== "step" && v.kind !== "recipe") {
      const names = Object.keys(sample[0].inputs || {});
      parts.push(
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "layr-table small calls" },
            h(
              "tr",
              {},
              names.map((k) => h("th", {}, k)),
              h("th", {}, "→ result"),
            ),
            sample.map((c) =>
              h(
                "tr",
                {},
                names.map((k) =>
                  h(
                    "td",
                    { class: typeof c.inputs[k] === "number" ? "num" : "" },
                    typeof c.inputs[k] === "object" ? "…" : fmt(c.inputs[k]),
                  ),
                ),
                h(
                  "td",
                  { class: "num" },
                  typeof c.result === "object" ? JSON.stringify(c.result).slice(0, 60) : fmt(c.result),
                ),
              ),
            ),
          ),
        ),
      );
    }
    const err = (v.sample || []).find((c) => c.error);
    if (err) parts.push(h("div", { class: "banner danger" }, err.error));
  } else if (ME.exp) {
    parts.push(h("div", { class: "layr-muted small" }, "Not called in the last run (or it hasn't run yet)."));
  }
  parts.push(ME.roundsBox); // its loops' rounds (drawValues fills it)
  if (mine.length)
    parts.push(
      h("h4", {}, "Style notes"),
      h(
        "ul",
        { class: "lint-list" },
        mine.map((x) =>
          h(
            "li",
            { class: `${x.level} click`, onclick: () => x.line && jumpTo(x.line) },
            h("b", {}, `line ${x.line}: `),
            x.message,
          ),
        ),
      ),
    );
  if (v.kind === "toolbox") {
    parts.push(
      h("h4", {}, "The code (toolbox, read only)"),
      codeBlock(v.code),
      v.is_function
        ? h(
            "button",
            {
              class: "layr-button small",
              onclick: async () => {
                try {
                  const r = await call("toolbox_copy", { name, source: ME.ed.getValue() });
                  replaceCode(r.source);
                  layr.ui.notify(`${name} is copied into your method: change it there, then save`, "success");
                  recheck().then(() => {
                    const line = (ME?.summary?.outline || []).find((x) => x.name === name)?.line;
                    if (line) jumpTo(line, name);
                  });
                } catch (err) {
                  fail(err);
                }
              },
            },
            "Copy into my method to change it",
          )
        : null,
    );
  }
  if (AI.claude) {
    const aiBox = h("div", {});
    const ask = async () => {
      const selection = ME.ed.selection();
      put(
        aiBox,
        h("div", { class: "layr-muted small" }, h("span", { class: "layr-spinner" }), " Claude is reading the code…"),
      );
      try {
        const text = await askClaude(
          "explain",
          selection.trim()
            ? { source: ME.ed.getValue(), code: selection }
            : v.kind === "toolbox"
              ? { source: ME.ed.getValue(), code: v.code }
              : { source: ME.ed.getValue(), function: name },
        );
        ME.ai = { fn: name, text };
        put(aiBox, aiAnswer(text));
      } catch (err) {
        put(aiBox, h("div", { class: "layr-error small" }, err.message));
      }
    };
    parts.push(
      h(
        "div",
        { class: "row-gap" },
        h(
          "button",
          { class: "layr-button small", onclick: ask, title: "Select lines first to explain only those" },
          "Explain in plain words (AI)",
        ),
      ),
      aiBox,
    );
    if (ME.ai?.fn === name) put(aiBox, aiAnswer(ME.ai.text));
  }
  parts.push(...allMessages);
  put(box, h("div", { class: "sheet" }, parts));
  ME.inspectorFn = name;
}

async function duplicate(method) {
  const id = h("input", { class: "layr-input", value: `${method.id}-copy` });
  const name = h("input", { class: "layr-input", value: `${method.name} (copy)` });
  const ok = await openDialog({
    title: "Duplicate the method",
    body: [
      h("label", { class: "layr-label" }, "Id (letters, digits, dashes)"),
      id,
      h("label", { class: "layr-label" }, "Name"),
      name,
    ],
    buttons: [
      { label: "Cancel", id: false, kind: "ghost" },
      { label: "Duplicate", id: true, kind: "primary" },
    ],
  });
  if (!ok) return;
  try {
    const r = await call("method_create", { method: id.value.trim(), name: name.value.trim(), copy_of: method.id });
    if (!r.saved) return layr.ui.notify(r.messages.map((p) => p.message).join("; ") || r.note, "error");
    show({ method: id.value.trim(), exp: null });
  } catch (err) {
    fail(err);
  }
}

// ------------------------------------------------------------------------------------------------ guide

async function renderGuide(t) {
  const { GUIDE } = await import("./guide.js");
  if (t !== token) return;
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: "Python for methods" }]);
  setActions([]);
  layr.panel.setTitle("Python for methods").catch(() => {});
  put(
    view,
    h(
      "div",
      { class: "page narrow guide" },
      h("h2", {}, "Python for methods"),
      h(
        "p",
        { class: "layr-muted" },
        "The few Python and pandas patterns the methods use, each with a short example. Open a method to see them " +
          "in real code; the Explain button there asks Claude about any function or selected lines.",
      ),
      GUIDE.map((g, i) =>
        h(
          "section",
          {},
          h("h3", {}, `${i + 1}. ${g.title}`),
          g.text.map((p) => h("p", {}, p)),
          codeBlock(g.code),
        ),
      ),
    ),
  );
}

// ------------------------------------------------------------------------------------------------ main

async function render() {
  const t = ++token;
  try {
    if (S.view === "work" && S.method) await renderWork(t);
    else if (S.view === "new") await renderNew(t, S.preset);
    else if (S.view === "guide") await renderGuide(t);
    else {
      if (S.view !== "home") S.view = "home";
      await renderHome(t);
    }
  } catch (err) {
    if (t !== token) return;
    if (/No experiment|No method|no method/.test(err?.message || "")) {
      closeWork();
      go({ view: "home", exp: null, method: null });
      return;
    }
    put(
      view,
      h(
        "div",
        { class: "layr-empty" },
        h("div", { class: "layr-error" }, cleanError(err)),
        h("button", { class: "layr-button", onclick: () => show({ view: "home" }) }, "Back to the experiments"),
      ),
    );
  }
}

async function waitForBackend() {
  const hint = setTimeout(() => {
    const el = document.getElementById("startingHint");
    if (el) el.hidden = false;
  }, 4000);
  // The first start installs Python and its science packages: on a slow line that takes minutes.
  for (let i = 0; ; i++) {
    try {
      await call("methods");
      clearTimeout(hint);
      return;
    } catch (err) {
      if (i > 400) throw err;
      await new Promise((r) => setTimeout(r, 1500));
    }
  }
}

await layr.ready;
Object.assign(S, layr.info.state || {});
S.preset = null;
try {
  await waitForBackend();
} catch (err) {
  put(
    view,
    h(
      "div",
      { class: "layr-empty" },
      h("div", { class: "layr-error" }, "The analysis engine didn't start. ", cleanError(err)),
      h("button", { class: "layr-button", onclick: () => location.reload() }, "Try again"),
    ),
  );
  throw err;
}
/** A file the panel was opened with: stored, and a new experiment started from it (once per file). */
async function openedWith(file) {
  if (!file?.id || file.id === S.openedFile) return false;
  try {
    S.preset = await storeFileRef(file);
    S.openedFile = file.id;
    S.newMethod = null;
    return true;
  } catch (err) {
    fail(err);
    return false;
  }
}
if (await openedWith(layr.files.current?.())) {
  S.view = "new";
  save();
}
checkAi();
try {
  layr.connections.onChange?.(() => checkAi().then(() => S.view === "work" && drawInspector()));
} catch {
  // an older layr: checked once
}
// A link opens the panel again at a place: its params are a show() target.
layr.panel.onParams?.(async (p) => {
  if (!p || !Object.keys(p).length) return;
  if (p.ref) return openRef(p.ref); // a layr://research/open/… link (a reference in a Wiki page)
  if (p.file) {
    if (await openedWith(p.file)) go({ view: "new" });
    return;
  }
  show(p);
});
layr.events.on("research:lab", (ev) => {
  if (ev.what === "run" && ev.trial) {
    // A trial run (a change of the code being tried): it lights up its step, and it is not the experiment's new
    // latest run, so nothing else is redrawn.
    if (S.view === "work" && ME && ev.experiment === ME.exp)
      ME.flow?.setRunning(!ev.finished && ev.step ? { step: ev.step } : null);
    return;
  }
  if (ev.what === "run" && !ev.finished && !ev.step && ev.message && live && live.exp === ev.experiment)
    setStatus(ev.message); // e.g. installing the method's packages before its first run
  if (ev.what === "run" && !ev.finished && ev.step) {
    if (live && live.exp === ev.experiment) {
      live.step = ev.step;
      setStatus(`Calculating: ${human(ev.step)}`);
    }
    if (S.view === "work" && ME?.exp === ev.experiment) ME.flow?.setRunning({ step: ev.step });
  }
  // while the panel's own run is going, the screen is drawn again when it's done
  if (live && (ev.what === "run" || ev.experiment === live.exp)) return;
  if (ev.what === "run" && !ev.finished) return;
  if (S.view === "work" && ME) {
    if ((ev.method && ev.method === S.method) || ev.what === "comparison") refreshMethodMeta();
    if (ev.what === "run" && ev.experiment === ME.exp && !ME.trialRun && !S.run) {
      ME.flow?.update({});
      valuesChanged();
    }
    if (!ev.experiment || ev.experiment === S.exp || ME.mine.some((e) => e.experiment === ev.experiment)) render();
    return;
  }
  if (S.view === "home") render();
});
window.addEventListener("resize", () => {
  if (String(S.tab).startsWith("page:") && S.view === "work" && !PG.editing()) {
    clearTimeout(window.__rz);
    window.__rz = setTimeout(render, 250);
  }
});
window.addEventListener("beforeunload", (e) => {
  if (ME?.dirty) e.preventDefault();
});
render();
// started by a link to a place (a reference in a Wiki page): it goes there
if (layr.info.params?.ref) openRef(layr.info.params.ref);

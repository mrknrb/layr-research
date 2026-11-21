// Experiments: methods as Python code drawn as a flow and explained from itself, GC peak review, results, checks,
// corrections, the PhD database. Everything goes through the plugin's own tools (layr.tools.run), the same ones AI
// agents use. The flow (flow.js) is the main screen of an experiment and sits above the code of a method.
import { createEditor, createMerge, highlight } from "./editor.js";
import { mountFlow } from "./flow.js";

const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");
const actions = document.getElementById("actions");
const statusEl = document.getElementById("status");
const dialog = document.getElementById("dialog");

const S = {
  view: "home",
  exp: null,
  tab: "flow",
  run: null, // an earlier run of the experiment picked in the flow (null: the latest)
  setting: null, // a setting to show when the Settings tab opens
  method: null,
  fn: null,
  line: null,
  numbersExp: null,
  inj: null,
  comp: null,
  injFilter: "all",
  table: null,
  dbTable: null,
  flowFrac: 0.42, // the method screen: how much of the height the flow takes
  autoRecalc: true, // the method screen: a trial run by itself after a change
};
let live = null; // {exp, timer} while a run is in progress
let token = 0; // render generation: a slower, older render must not overwrite a newer one

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
  layr.panel.setState({ ...S }).catch(() => {});
}

function go(patch) {
  const leaving =
    S.view === "method" && ((patch.view && patch.view !== "method") || (patch.method && patch.method !== S.method));
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
  if (leaving) closeMethod();
  if ((patch.view && patch.view !== "experiment") || (patch.exp && patch.exp !== S.exp)) {
    closeExpFlow();
    if (!("run" in patch)) patch = { ...patch, run: null };
  }
  Object.assign(S, patch);
  save();
  render();
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

function openDialog({ title, body, buttons, wide = false }) {
  return new Promise((resolve) => {
    dialog.classList.toggle("wide", !!wide);
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
              onclick: () => {
                const v = b.value ? b.value() : b.id;
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

function table(rows, columns, { onRow, onCell, limit = 200 } = {}) {
  if (!rows.length) return h("div", { class: "layr-muted pad" }, "No rows.");
  const cols = columns || Object.keys(rows[0]);
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
            { class: onRow ? "click" : "", onclick: onRow && (() => onRow(r)) },
            cols.map((c) =>
              h(
                "td",
                {
                  class: typeof r[c] === "number" ? (onCell ? "num click" : "num") : "",
                  title: typeof r[c] === "number" && onCell ? "How was this number calculated?" : null,
                  onclick: typeof r[c] === "number" && onCell ? () => onCell(r, c) : null,
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

async function readText(types) {
  const ref = await layr.files.open({ types });
  if (!ref) return null;
  return { name: ref.name, text: new TextDecoder().decode(await layr.files.read(ref.id)) };
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

// ------------------------------------------------------------------------------------------------ chrome

function setCrumbs(items) {
  put(
    crumbs,
    ...items.flatMap((it, i) => [
      i ? h("span", { class: "sep" }, "›") : null,
      it.go
        ? h(
            "a",
            {
              href: "#",
              onclick: (e) => {
                e.preventDefault();
                go(it.go);
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

async function renderHome(t) {
  const [exps, methods] = await Promise.all([call("experiments"), call("methods")]);
  if (t !== token) return;
  setCrumbs([{ label: "Experiments" }]);
  layr.panel.setTitle("Experiments").catch(() => {});
  setActions([
    h("button", { class: "layr-button ghost", onclick: () => go({ view: "guide" }) }, "Python guide"),
    h("button", { class: "layr-button ghost", onclick: () => go({ view: "database" }) }, "Results database"),
  ]);
  const cards = exps.map((e) =>
    h(
      "div",
      {
        class: "layr-card exp-card click",
        onclick: () => go({ view: "experiment", exp: e.experiment, tab: "flow" }),
      },
      h("div", { class: "exp-title" }, e.title),
      h("div", { class: "layr-muted" }, e.method_name, " · ", Object.values(e.files || {}).join(", ") || "no file yet"),
      h(
        "div",
        { class: "exp-meta" },
        e.running
          ? badge("running", "accent")
          : e.last_run
            ? [
                badge(
                  e.last_run.status === "done" ? "calculated" : e.last_run.status,
                  e.last_run.status === "done" ? "success" : "danger",
                ),
                ...flagBadges(e.last_run.summary?.flags),
                h("span", { class: "layr-muted" }, when(e.last_run.finished)),
              ]
            : badge("not run yet", ""),
        e.proposed_corrections ? badge(`${e.proposed_corrections} proposed corrections`, "warning") : null,
      ),
    ),
  );
  put(
    view,
    h(
      "div",
      { class: "page" },
      h(
        "section",
        {},
        h(
          "div",
          { class: "section-head" },
          h("h2", {}, "Experiments"),
          h("span", { class: "layr-spacer" }),
          h("button", { class: "layr-button ghost", onclick: makeExample }, "Try the example"),
          h("button", { class: "layr-button primary", onclick: () => go({ view: "new" }) }, "New experiment"),
        ),
        cards.length
          ? h("div", { class: "cards" }, cards)
          : h(
              "div",
              { class: "layr-empty" },
              "No experiments yet. Start one from a GC sequence (GAML export), or try the example.",
            ),
      ),
      h(
        "section",
        {},
        h(
          "div",
          { class: "section-head" },
          h("h2", {}, "Methods"),
          h("span", { class: "layr-spacer" }),
          h("button", { class: "layr-button", onclick: newMethod }, "New method"),
        ),
        h(
          "p",
          { class: "layr-muted" },
          "A method is the recipe, written in plain Python: small formulas, steps that work on tables, and a recipe " +
            "that calls the steps. Open one to read or change its code; every number it makes can be traced back to " +
            "its line. Changes Claude makes wait for you to accept them.",
        ),
        h(
          "ul",
          { class: "layr-list lined" },
          methods.map((m) =>
            h(
              "li",
              {
                class: m.legacy ? "" : "click",
                onclick: m.legacy ? null : () => go({ view: "method", method: m.id, fn: null }),
              },
              h("b", {}, m.name),
              " ",
              h(
                "span",
                { class: "layr-muted" },
                m.legacy
                  ? "old step method (0.2): no longer runs"
                  : m.draft_only
                    ? "new, proposed by Claude"
                    : `version ${m.version} · ${m.steps} steps${m.builtin ? " · built in" : ""}`,
              ),
              m.draft && !m.draft_only ? [" ", badge("change proposed", "warning")] : null,
              m.draft_only ? [" ", badge("waiting for you", "warning")] : null,
              h("div", { class: "layr-muted small" }, m.description),
            ),
          ),
        ),
      ),
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
    go({ view: "method", method: id, fn: null, numbersExp: null });
  } catch (e) {
    fail(e);
  }
}

async function makeExample() {
  let e;
  try {
    setStatus("Making the example…");
    e = await call("lab_sample");
  } catch (err) {
    fail(err);
  } finally {
    setStatus("");
  }
  if (!e) return;
  go({ view: "experiment", exp: e.experiment, tab: "flow" });
  startRun(e.experiment); // after the status above is cleared: the status now follows the run
}

// ------------------------------------------------------------------------------------------------ new experiment

async function renderNew(t, preset) {
  const methods = await call("methods");
  if (t !== token) return;
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: "New experiment" }]);
  setActions([]);
  const st = { files: {}, params: {}, corrections: [], title: "", method: methods[0]?.id };
  if (preset) st.files.raw = preset;
  const title = h("input", { class: "layr-input", placeholder: "e.g. Activity assay 06 - VLB120 ΔalkB, glucose" });
  const methodSel = h(
    "select",
    { class: "layr-select" },
    methods
      .filter((m) => !m.legacy)
      .map((m) => h("option", { value: m.id, selected: m.id === S.newMethod }, m.name)),
  );
  S.newMethod = null;
  const filesBox = h("div", {});
  const gcBox = h("div", { class: "layr-muted" });
  const drawFiles = () => {
    const m = methods.find((x) => x.id === methodSel.value);
    st.method = m.id;
    put(
      filesBox,
      ...(m.inputs || []).map((inp) =>
        h(
          "div",
          { class: "file-row" },
          h("span", {}, inp.label || inp.id),
          " ",
          st.files[inp.id] ? h("b", {}, st.files[inp.id].name) : h("span", { class: "layr-muted" }, "no file"),
          " ",
          h(
            "button",
            {
              class: "layr-button small",
              onclick: async () => {
                try {
                  const f = await pickAndStore(inp.types || []);
                  if (f) {
                    st.files[inp.id] = f;
                    drawFiles();
                  }
                } catch (err) {
                  fail(err);
                }
              },
            },
            "Choose file…",
          ),
        ),
      ),
    );
  };
  methodSel.onchange = drawFiles;
  drawFiles();
  const importGc = async () => {
    try {
      const cfg = await readText([".yaml", ".yml"]);
      if (!cfg) return;
      let corr = null;
      if (
        await openDialog({
          title: "Corrections file",
          body:
            "Does this experiment have a gcassay corrections file " +
            "(renames, exclusions, manual integrations with reasons)?",
          buttons: [
            { label: "No", id: false, kind: "ghost" },
            { label: "Choose it…", id: true, kind: "primary" },
          ],
        })
      ) {
        corr = await readText([".yaml", ".yml"]);
      }
      const got = await call("gcassay_settings", { config: cfg.text, corrections: corr?.text || null });
      st.params = got.params;
      st.corrections = got.corrections;
      if (!title.value) title.value = got.title;
      put(
        gcBox,
        `Settings from ${cfg.name}: ${Object.keys(got.params).length} parameters`,
        corr ? `, ${got.corrections.length} corrections from ${corr.name}` : "",
        ".",
        got.raw_file ? h("div", {}, `Its raw file was ${got.raw_file}: choose that file above.`) : null,
      );
    } catch (err) {
      fail(err);
    }
  };
  const create = async () => {
    try {
      const m = methods.find((x) => x.id === methodSel.value);
      const missing = (m.inputs || []).filter((i) => !st.files[i.id] && !i.optional);
      if (missing.length)
        return layr.ui.notify(`Choose the file for: ${missing.map((i) => i.label || i.id).join(", ")}`, "error");
      const e = await call("experiment_create", {
        title: title.value || "Experiment",
        method: m.id,
        files: st.files,
        params: st.params,
        corrections: st.corrections,
      });
      go({ view: "experiment", exp: e.experiment, tab: "settings" });
    } catch (err) {
      fail(err);
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
      h("label", { class: "layr-label" }, "Raw data"),
      filesBox,
      h(
        "div",
        { class: "layr-card gc-import" },
        h("div", {}, h("b", {}, "Already analysed with gcassay?"), " Take over its settings and corrections."),
        h("button", { class: "layr-button small", onclick: importGc }, "Settings from a gcassay config…"),
        gcBox,
      ),
      h("div", { class: "row-end" }, h("button", { class: "layr-button primary", onclick: create }, "Create")),
    ),
  );
}

// ------------------------------------------------------------------------------------------------ experiment

const TABS = [
  ["flow", "Flow"],
  ["peaks", "Peaks"],
  ["results", "Results"],
  ["checks", "Checks"],
  ["corrections", "Corrections"],
  ["settings", "Settings"],
];

// The experiment's flow stays mounted while the experiment is shown (its view, selection and inside panel survive a
// redraw of the screen around it): {exp, host, flow, run, lastRun}.
let EF = null;

function closeExpFlow() {
  EF?.flow.destroy();
  EF = null;
}

/** The run the experiment's tabs show: an earlier one picked in the flow, else its latest. */
const runArgs = (exp) => (S.run ? { run: S.run } : { experiment: exp.id });

async function renderExperiment(t) {
  const exp = await call("experiment_get", { experiment: S.exp });
  if (t !== token) return;
  layr.panel.setTitle(exp.title).catch(() => {});
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: exp.title }]);
  const running = exp.running || (live && live.exp === exp.id);
  setActions([
    h(
      "button",
      { class: "layr-button ghost", disabled: !exp.last_run, onclick: (e) => exportMenu(e, exp) },
      "Export ▾",
    ),
    running
      ? h(
          "button",
          {
            class: "layr-button danger",
            onclick: () => call("run_stop", { experiment: exp.id }).catch(fail),
          },
          "Stop",
        )
      : exp.last_run
        ? h(
            "button",
            {
              class: "layr-button ghost run-all",
              title: "Calculate every step again, reusing nothing from before",
              onclick: () => startRun(exp.id, { fresh: true }),
            },
            "Run all again",
          )
        : null,
    h(
      "button",
      { class: "layr-button primary", disabled: running, onclick: () => startRun(exp.id) },
      running ? "Running…" : "Run",
    ),
  ]);
  const isGc = (exp.method_inputs || []).some((i) => (i.types || []).includes(".gaml"));
  const corr = exp.corrections || [];
  const proposed = corr.filter((c) => c.status === "proposed").length;
  const flags = exp.last_run?.summary?.flags || {};
  if (S.tab === "method" || !TABS.some(([id]) => id === S.tab)) S.tab = "flow";
  const tab = S.tab === "peaks" && !isGc ? "flow" : S.tab;
  const tabs = h(
    "div",
    { class: "tabs" },
    TABS.filter(([id]) => id !== "peaks" || isGc).map(([id, label]) =>
      h(
        "button",
        { class: `tab ${tab === id ? "active" : ""} tab-${id}`, onclick: () => go({ tab: id }) },
        label,
        id === "checks" && (flags.error || flags.warning)
          ? h("span", { class: "count" }, (flags.error || 0) + (flags.warning || 0))
          : null,
        id === "corrections" && proposed ? h("span", { class: "count warn" }, proposed) : null,
      ),
    ),
  );
  const body = h("div", { class: `tab-body${tab === "flow" ? " flow-tab" : ""}` });
  put(view, h("div", { class: "exp" }, tabs, h("div", { class: "exp-banners" }, expBanners(exp, tab)), body));
  const fn =
    {
      flow: tabFlow,
      peaks: tabPeaks,
      results: tabResults,
      checks: tabChecks,
      corrections: tabCorrections,
      settings: tabSettings,
    }[tab] || tabFlow;
  await fn(body, exp, t);
}

/** What the experiment's screen says above its tab: a problem, a failed or missing run, an earlier run shown. */
function expBanners(exp, tab) {
  const last = exp.last_run;
  const at = last?.error_at;
  const out = [];
  if (exp.method_problem) out.push(h("div", { class: "banner danger" }, exp.method_problem));
  else if (last && last.status !== "done" && !S.run)
    out.push(
      h(
        "div",
        { class: "banner danger" },
        `The last run ${last.status === "stopped" ? "was stopped" : "failed"}: `,
        last.error || "",
        at?.line
          ? [
              " ",
              h(
                "a",
                {
                  href: "#",
                  onclick: (e) => {
                    e.preventDefault();
                    go({ view: "method", method: exp.method, fn: at.function, line: at.line, numbersExp: exp.id });
                  },
                },
                `line ${at.line} in ${at.function}`,
              ),
            ]
          : null,
      ),
    );
  else if (!last) out.push(h("div", { class: "banner" }, "Not calculated yet. Check the settings, then press Run."));
  else if (exp.changed && exp.changed > (last.started || last.finished) && !S.run)
    out.push(h("div", { class: "banner" }, "Changed since the last run: press Run to recalculate."));
  if (S.run) {
    const follows = tab === "results" || tab === "checks" || tab === "flow";
    out.push(
      h(
        "div",
        { class: "banner earlier" },
        h("b", {}, "You are looking at an earlier run. "),
        follows
          ? "The flow, Results and Checks show it."
          : "This tab shows the experiment as it is now (Results and Checks show the earlier run).",
        " ",
        h(
          "a",
          {
            href: "#",
            onclick: (e) => {
              e.preventDefault();
              go({ run: null });
            },
          },
          "Back to the latest run",
        ),
      ),
    );
  }
  return out;
}

// ---------- Flow tab: the method as boxes with this experiment's data; a box opens what belongs to it

async function tabFlow(body, exp) {
  const lastRun = exp.last_run?.id || null;
  if (!EF || EF.exp !== exp.id) {
    closeExpFlow();
    const host = h("div", { class: "flow-host" });
    body.append(host);
    EF = { exp: exp.id, host, run: S.run || null, lastRun, flow: null };
    EF.flow = mountFlow(host, {
      call,
      tex,
      method: exp.method,
      version: exp.method_version ?? null,
      experiment: exp.id,
      run: S.run || null,
      editable: false,
      inside: "panel",
      claude: false,
      runNote: false, // the screen's own banner says when the last run failed, with a link to the line
      figure: figureOf,
      onSetting: (id, value) => settingChanged(exp, id, value),
      onPick: (p) => pickedInFlow(exp, p),
      onOpen: (o) => openFromFlow(exp, o),
      boxActions: (n) => expBoxActions(exp, n),
    });
  } else {
    body.append(EF.host);
    if (EF.run !== (S.run || null) || EF.lastRun !== lastRun) {
      EF.run = S.run || null;
      EF.lastRun = lastRun;
      EF.flow.update({ run: EF.run });
    }
  }
  EF.flow.setRunning(live && live.exp === exp.id && live.step ? { step: live.step } : null);
}

/** A figure a run returned, as an image's src (the flow shows it in the result's card and inside its box). */
async function figureOf({ run, output }) {
  const res = await layr.tools.call("figure", { run, output });
  const img = res.content?.find((c) => c.type === "image");
  return img ? `data:${img.mimeType};base64,${img.data}` : null;
}

/** The links a box gets on the experiment's screen: the peaks to review, the checks, the corrections. */
function expBoxActions(exp, n) {
  const isGc = (exp.method_inputs || []).some((i) => (i.types || []).includes(".gaml"));
  if (n.folded && n.quality)
    return [{ label: "See the checks", title: "Every check with what it found", run: () => go({ tab: "checks" }) }];
  if (n.input?.kind === "corrections")
    return [{ label: "Open the corrections", run: () => go({ tab: "corrections" }) }];
  if (n.input)
    return [{ label: n.input.value ? "Use another file" : "Choose the file", title: "In the experiment's settings",
      run: () => go({ tab: "settings" }) }];
  if (isGc && (n.fn === "integrate_peaks" || (n.outs || []).includes("peaks")))
    return [
      {
        label: "Review the peaks",
        title: "Each injection's trace with the integrated peaks; drag a boundary to correct it",
        run: () => go({ tab: "peaks" }),
      },
    ];
  return [];
}

function openFromFlow(exp, o) {
  if (o.what === "results") go({ tab: "results", table: o.table || null });
  else if (o.what === "settings") go({ tab: "settings", setting: o.setting || null });
  else if (o.what === "corrections") go({ tab: "corrections" });
  else if (o.what === "checks") go({ tab: "checks" });
  else if (o.what === "peaks") go({ tab: "peaks" });
  else if (o.what === "function")
    go({
      view: "method",
      method: exp.method,
      fn: o.fn || null,
      line: o.fn ? null : o.line || null,
      numbersExp: exp.id,
    });
  else if (o.what === "new-experiment") go({ view: "new", newMethod: o.method || exp.method });
}

function pickedInFlow(exp, { experiment, run }) {
  if (experiment && experiment !== exp.id) {
    go({ view: "experiment", exp: experiment, run: null });
    return;
  }
  if (!experiment) return;
  // The flow already shows the run: keep the record so the other tabs follow, and redraw the banner.
  S.run = run || null;
  if (EF) EF.run = S.run;
  save();
  render();
}

/** A setting changed in a box: it is the experiment's now, and the experiment is calculated again by itself. */
async function settingChanged(exp, id, value) {
  try {
    await call("experiment_update", { experiment: exp.id, params: { [id]: value } });
    S.run = null;
    startRun(exp.id);
  } catch (err) {
    fail(err);
    render();
  }
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
 * Calculates the experiment (`fresh`: every step again, nothing reused). The flow lights up the step being
 * calculated; when it is done the screen shows the new numbers and how many steps were reused.
 */
function startRun(expId, { fresh = false } = {}) {
  const mine = (live = { exp: expId, step: null });
  S.run = null;
  setStatus("Calculating…");
  // The run answers when it is done (a remembered run in well under a second); its steps light up from its events.
  // A long one answers "running" after the wait: then it is asked again every half second.
  call("run", { experiment: expId, wait_s: 30, ...(fresh ? { fresh: true } : {}) }).then(
    (r) => live === mine && (r.status === "running" || r.status === "waiting" ? poll() : done(r)),
    (err) => {
      if (live !== mine) return;
      live = null;
      if (EF?.exp === expId) EF.flow.setRunning(null);
      setStatus("");
      fail(err);
    },
  );
  const done = (r) => {
    live = null;
    if (EF?.exp === expId) {
      EF.flow.setRunning(null);
      // the new numbers go into the flow at once; the screen around it (banners, buttons) is drawn as well
      if (r.run && !S.run && r.status !== "stopped") {
        EF.lastRun = r.run;
        EF.run = null;
        EF.flow.update({ run: null, refresh: true });
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
          if (EF?.exp === expId) EF.flow.setRunning({ step: r.step });
        }
        live.timer = setTimeout(poll, 500);
        return;
      }
      done(r);
    } catch (err) {
      live = null;
      if (EF?.exp === expId) EF.flow.setRunning(null);
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

// The step that made a saved table, from its file name (steps/03_integrate_peaks.csv).
function stepOfFile(file) {
  const m = /steps\/\d+_(.+?)(_in_.+|_\d+)?\.csv$/.exec(file || "");
  return m ? m[1] : null;
}

function tableChips(inputs, output, fnName, exp) {
  const open = (step) => exp && step && go({ view: "experiment", exp: exp.id, tab: "results", table: `step:${step}` });
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

// ---------- Peaks tab: review the integration, drag the boundaries

const STATUS_CLASS = { ok: "ok", "<LOQ": "loq", "n.d.": "nd", manual: "manual", excluded: "excl" };

async function tabPeaks(body, exp, t) {
  // The peaks are reviewed (and corrected) on the latest run; an earlier run picked in the flow doesn't change that.
  if (!exp.last_run || exp.last_run.status !== "done") {
    put(body, h("div", { class: "layr-empty" }, "Run the experiment first: the peaks come from a run."));
    return;
  }
  const [pk, fl] = await Promise.all([
    call("result_table", {
      experiment: exp.id,
      table: "peaks",
      limit: 2000,
      columns: ["injection", "name", "role", "compound", "status", "area", "snr", "rt", "rt_expected", "note"],
    }),
    call("result_table", { experiment: exp.id, table: "flags", limit: 2000 }).catch(() => ({ rows: [] })),
  ]);
  if (t !== token) return;
  const peaks = pk.rows;
  const comps = [...new Set(peaks.map((p) => p.compound))];
  const byInj = new Map();
  for (const p of peaks) {
    if (!byInj.has(p.injection))
      byInj.set(p.injection, { injection: p.injection, name: p.name, role: p.role, peaks: {} });
    byInj.get(p.injection).peaks[p.compound] = p;
  }
  const flagged = new Map();
  for (const f of fl.rows)
    if (f.injection != null && f.injection !== "") {
      const k = Number(f.injection);
      if (!flagged.has(k)) flagged.set(k, []);
      flagged.get(k).push(f);
    }
  const injs = [...byInj.values()];
  if (!S.comp || !comps.includes(S.comp))
    S.comp = comps.find((c) => c !== exp.resolved_params.internal_standard) || comps[0];
  if (!S.inj || !byInj.has(S.inj)) S.inj = injs.find((i) => i.role === "sample")?.injection || injs[0]?.injection;
  const filters = {
    all: () => true,
    sample: (i) => i.role === "sample",
    standard: (i) => i.role === "standard",
    blank: (i) => i.role === "blank",
    flagged: (i) =>
      flagged.has(i.injection) || Object.values(i.peaks).some((p) => p.status !== "ok" && p.status !== "n.d."),
  };
  const shown = injs.filter(filters[S.injFilter] || filters.all);
  const filterSel = h(
    "select",
    {
      class: "layr-select small",
      onchange: (e) => {
        S.injFilter = e.target.value;
        save();
        tabPeaks(body, exp, token);
      },
    },
    [
      ["all", "All injections"],
      ["sample", "Samples"],
      ["standard", "Standards"],
      ["blank", "Blanks"],
      ["flagged", "Needs a look"],
    ].map(([v, l]) => h("option", { value: v, selected: S.injFilter === v }, l)),
  );
  const list = h(
    "ul",
    { class: "inj-list" },
    shown.map((i) =>
      h(
        "li",
        {
          class: i.injection === S.inj ? "active" : "",
          onclick: () => {
            S.inj = i.injection;
            save();
            tabPeaks(body, exp, token);
          },
        },
        h("span", { class: "inj-num" }, `#${i.injection}`),
        h("span", { class: "inj-name", title: i.name }, i.name),
        h(
          "span",
          { class: "dots" },
          comps.map((c) =>
            h("i", {
              class: `dot ${STATUS_CLASS[i.peaks[c]?.status] || "nd"}`,
              title: `${c}: ${i.peaks[c]?.status || "–"}`,
            }),
          ),
        ),
        flagged.has(i.injection)
          ? h(
              "span",
              {
                class: "flag-mark",
                title: flagged
                  .get(i.injection)
                  .map((f) => f.message)
                  .join("\n"),
              },
              "!",
            )
          : null,
      ),
    ),
  );
  const chips = h(
    "div",
    { class: "chips" },
    comps.map((c) =>
      h(
        "button",
        {
          class: `chip ${c === S.comp ? "active" : ""}`,
          onclick: () => {
            S.comp = c;
            save();
            tabPeaks(body, exp, token);
          },
        },
        c,
      ),
    ),
  );
  const idx = shown.findIndex((i) => i.injection === S.inj);
  const nav = h(
    "span",
    {},
    h(
      "button",
      {
        class: "layr-button small ghost",
        disabled: idx <= 0,
        onclick: () => {
          S.inj = shown[idx - 1].injection;
          save();
          tabPeaks(body, exp, token);
        },
      },
      "‹ Previous",
    ),
    h(
      "button",
      {
        class: "layr-button small ghost",
        disabled: idx < 0 || idx >= shown.length - 1,
        onclick: () => {
          S.inj = shown[idx + 1].injection;
          save();
          tabPeaks(body, exp, token);
        },
      },
      "Next ›",
    ),
  );
  const plotBox = h(
    "div",
    { class: "plot-box" },
    h("div", { class: "layr-empty" }, h("span", { class: "layr-spinner" })),
  );
  const legend = h(
    "div",
    { class: "legend layr-muted small" },
    h("i", { class: "dot ok" }),
    " found  ",
    h("i", { class: "dot loq" }),
    " below LOQ  ",
    h("i", { class: "dot nd" }),
    " not detected  ",
    h("i", { class: "dot manual" }),
    " set by hand  ",
    h("i", { class: "dot excl" }),
    " excluded",
  );
  put(
    body,
    h(
      "div",
      { class: "peaks-grid" },
      h("div", { class: "inj-col" }, filterSel, list),
      h(
        "div",
        { class: "plot-col" },
        h("div", { class: "plot-head" }, chips, h("span", { class: "layr-spacer" }), nav),
        plotBox,
        legend,
      ),
    ),
  );
  const inj = byInj.get(S.inj);
  if (inj) drawPeak(plotBox, exp, inj, S.comp, flagged.get(inj.injection) || [], t);
}

async function drawPeak(box, exp, inj, comp, flags, t) {
  const p0 = inj.peaks[comp];
  const center = p0?.rt_expected ?? p0?.rt;
  if (center == null) {
    put(box, h("div", { class: "layr-empty" }, "No expected time for this compound."));
    return;
  }
  let half = S.zoom || 0.25;
  const data = await call("chromatogram", {
    experiment: exp.id,
    injection: inj.injection,
    start_min: center - half,
    end_min: center + half,
    points: 1400,
  });
  if (t !== token) return;
  const peak = data.peaks.find((p) => p.compound === comp) || {};
  const corrHere = data.corrections.filter(
    (c) => (c.compound || null) === comp || (!c.compound && c.kind === "exclude"),
  );
  const active = corrHere.filter((c) => c.status === "approved");
  const W = Math.max(420, box.clientWidth || 700),
    H = 300,
    L = 54,
    R = 14,
    T = 12,
    B = 30;
  const xs = data.time,
    ys = data.signal;
  let ymin = Math.min(...ys),
    ymax = Math.max(...ys);
  const pad = (ymax - ymin) * 0.08 || 1;
  ymin -= pad;
  ymax += pad;
  const X = (v) => L + ((v - xs[0]) / (xs[xs.length - 1] - xs[0])) * (W - L - R);
  const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * (H - T - B);
  const Xinv = (px) => xs[0] + ((px - L) / (W - L - R)) * (xs[xs.length - 1] - xs[0]);
  const svgNS = "http://www.w3.org/2000/svg";
  const s = (tag, attrs) => {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  };
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "chrom" });
  // axes
  for (let i = 0; i <= 4; i++) {
    const v = ymin + ((ymax - ymin) * i) / 4;
    svg.append(s("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "grid" }));
    const lab = s("text", { x: L - 6, y: Y(v) + 4, class: "axis", "text-anchor": "end" });
    lab.textContent = fmt(v);
    svg.append(lab);
  }
  for (let i = 0; i <= 5; i++) {
    const v = xs[0] + ((xs[xs.length - 1] - xs[0]) * i) / 5;
    const lab = s("text", { x: X(v), y: H - 10, class: "axis", "text-anchor": "middle" });
    lab.textContent = v.toFixed(3);
    svg.append(lab);
  }
  const yl = s("text", { x: 12, y: T + 8, class: "axis" });
  yl.textContent = "pA";
  svg.append(yl);
  const xl = s("text", { x: W - R, y: H - B - 4, class: "axis", "text-anchor": "end" });
  xl.textContent = "min";
  svg.append(xl);
  // expected time and tolerance
  if (peak.rt_expected != null) {
    const tol = peak.rt_tolerance || 0;
    svg.append(
      s("rect", {
        x: X(peak.rt_expected - tol),
        y: T,
        width: Math.max(1, X(peak.rt_expected + tol) - X(peak.rt_expected - tol)),
        height: H - T - B,
        class: "tol",
      }),
    );
    svg.append(s("line", { x1: X(peak.rt_expected), x2: X(peak.rt_expected), y1: T, y2: H - B, class: "expected" }));
  }
  // other compounds' integrated peaks, lightly
  for (const o of data.peaks)
    if (o.compound !== comp && o.start != null && o.start > xs[0] && o.end < xs[xs.length - 1]) {
      svg.append(
        s("polygon", {
          points: areaPoints(xs, ys, o.start, o.end, o.baseline_start_y, o.baseline_end_y, X, Y),
          class: "area other",
        }),
      );
    }
  const shade = s("polygon", { class: "area" });
  const base = s("line", { class: "baseline" });
  svg.append(shade, base);
  svg.append(
    s("polyline", { points: xs.map((x, i) => `${X(x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" "), class: "trace" }),
  );
  if (peak.vendor_rt != null && peak.vendor_rt > xs[0] && peak.vendor_rt < xs[xs.length - 1]) {
    const vm = s("text", { x: X(peak.vendor_rt), y: T + 10, class: "vendor", "text-anchor": "middle" });
    vm.textContent = "▼ instrument";
    svg.append(vm);
  }
  // boundaries: automatic or manual, draggable
  const sel = { start: peak.start, end: peak.end, dirty: false };
  const yAt = (tm) => {
    let k = 0;
    while (k < xs.length - 1 && xs[k] < tm) k++;
    return ys[k];
  };
  const handles = {};
  for (const which of ["start", "end"]) {
    const g = s("g", { class: "handle" });
    g.append(
      s("line", { y1: T, y2: H - B, class: "handle-line" }),
      s("rect", { y: T, width: 12, height: 18, rx: 3, class: "handle-grip" }),
    );
    svg.append(g);
    handles[which] = g;
    g.addEventListener("pointerdown", (e) => {
      g.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const r = svg.getBoundingClientRect();
        let tm = Xinv(((ev.clientX - r.left) / r.width) * W);
        tm = Math.max(xs[0], Math.min(xs[xs.length - 1], tm));
        if (which === "start") sel.start = Math.min(tm, (sel.end ?? tm) - 0.002);
        else sel.end = Math.max(tm, (sel.start ?? tm) + 0.002);
        sel.dirty = true;
        update();
      };
      const up = () => {
        g.removeEventListener("pointermove", move);
        g.removeEventListener("pointerup", up);
      };
      g.addEventListener("pointermove", move);
      g.addEventListener("pointerup", up);
    });
  }
  const info = h("div", { class: "peak-info" });
  const saveBtn = h(
    "button",
    {
      class: "layr-button primary small",
      disabled: true,
      onclick: async () => {
        const reason = await askReason(
          `Integrate ${comp} in #${inj.injection} by hand`,
          "e.g. Shoulder of the neighbouring impurity cut off; valley at 6.49 min",
        );
        if (!reason) return;
        await correct({
          kind: "manual_integration",
          compound: comp,
          start_min: +sel.start.toFixed(4),
          end_min: +sel.end.toFixed(4),
          reason,
        });
      },
    },
    "Save these boundaries…",
  );
  function update() {
    const ok = sel.start != null && sel.end != null;
    for (const [which, g] of Object.entries(handles)) {
      const v = sel[which];
      g.style.display = v == null ? "none" : "";
      if (v != null) {
        g.querySelector("line").setAttribute("x1", X(v));
        g.querySelector("line").setAttribute("x2", X(v));
        g.querySelector("rect").setAttribute("x", X(v) - 6);
      }
    }
    if (ok) {
      const y0 = sel.dirty ? yAt(sel.start) : peak.baseline_start_y,
        y1 = sel.dirty ? yAt(sel.end) : peak.baseline_end_y;
      shade.setAttribute("points", areaPoints(xs, ys, sel.start, sel.end, y0, y1, X, Y));
      base.setAttribute("x1", X(sel.start));
      base.setAttribute("x2", X(sel.end));
      base.setAttribute("y1", Y(y0));
      base.setAttribute("y2", Y(y1));
      shade.classList.toggle("excluded", peak.status === "excluded" && !sel.dirty);
    }
    shade.style.display = base.style.display = ok ? "" : "none";
    saveBtn.disabled = !sel.dirty;
  }
  update();
  async function correct(args) {
    try {
      await call("correction_propose", { experiment: exp.id, injection: inj.injection, ...args });
      startRun(exp.id);
    } catch (err) {
      fail(err);
    }
  }
  const setByHand = () => {
    if (sel.start == null) {
      sel.start = (peak.rt_expected ?? center) - 0.02;
      sel.end = (peak.rt_expected ?? center) + 0.02;
      sel.dirty = true;
      update();
    }
  };
  put(
    info,
    h(
      "div",
      { class: "peak-title" },
      h("b", {}, `${comp} in #${inj.injection}`),
      " ",
      h("span", { class: "layr-muted" }, inj.name),
    ),
    h(
      "div",
      { class: "kv" },
      kv("Status", h("span", { class: `st ${STATUS_CLASS[peak.status] || ""}` }, peak.status || "–")),
      kv("Area", peak.area != null ? `${fmt(peak.area)} pA·min` : "–"),
      kv("S/N", fmt(peak.snr)),
      kv("Apex", peak.rt != null ? `${fmt(peak.rt)} min` : "–"),
      kv("Expected", `${fmt(peak.rt_expected)} ± ${fmt(peak.rt_tolerance)} min`),
      peak.vendor_rt != null
        ? kv("Instrument said", `${fmt(peak.vendor_rt)} min, ${fmt(peak.vendor_area)} pA·min`)
        : null,
    ),
    peak.note ? h("div", { class: "layr-muted small" }, peak.note) : null,
    flags.length
      ? h(
          "ul",
          { class: "peak-flags" },
          flags.map((f) => h("li", { class: f.level }, `${f.compound ? f.compound + ": " : ""}${f.message}`)),
        )
      : null,
    active.length
      ? h(
          "div",
          { class: "active-corr" },
          active.map((c) =>
            h(
              "div",
              {},
              badge(c.kind.replace("_", " "), "accent"),
              " ",
              c.reason,
              h(
                "button",
                {
                  class: "layr-button small ghost",
                  onclick: async () => {
                    try {
                      await call("correction_decide", { experiment: exp.id, correction: c.id, decision: "remove" });
                      startRun(exp.id);
                    } catch (err) {
                      fail(err);
                    }
                  },
                },
                "Undo",
              ),
            ),
          ),
        )
      : null,
    h(
      "div",
      { class: "peak-actions" },
      sel.start == null
        ? h("button", { class: "layr-button small", onclick: setByHand }, "Set boundaries by hand")
        : null,
      saveBtn,
      peak.status !== "excluded"
        ? h(
            "button",
            {
              class: "layr-button small",
              onclick: async () => {
                const reason = await askReason(`Exclude ${comp} in #${inj.injection}`);
                if (reason) await correct({ kind: "exclude", compound: comp, reason });
              },
            },
            "Exclude this peak…",
          )
        : null,
      h(
        "button",
        {
          class: "layr-button small ghost",
          onclick: async () => {
            const reason = await askReason(`Exclude the whole injection #${inj.injection}`);
            if (reason) await correct({ kind: "exclude", reason });
          },
        },
        "Exclude injection…",
      ),
      h("span", { class: "layr-spacer" }),
      h(
        "button",
        {
          class: "layr-button small ghost icon",
          title: "Zoom in",
          onclick: () => {
            S.zoom = Math.max(0.03, half / 1.6);
            drawPeak(box, exp, inj, comp, flags, token);
          },
        },
        "+",
      ),
      h(
        "button",
        {
          class: "layr-button small ghost icon",
          title: "Zoom out",
          onclick: () => {
            S.zoom = Math.min(3, half * 1.6);
            drawPeak(box, exp, inj, comp, flags, token);
          },
        },
        "−",
      ),
    ),
    h(
      "div",
      { class: "layr-muted small" },
      "Drag the two handles to move the peak's start and end; the baseline is the straight line between them.",
    ),
  );
  put(box, svg, info);
}

function kv(k, v) {
  return h("div", { class: "kv-row" }, h("span", { class: "layr-muted" }, k), h("span", {}, v));
}

function areaPoints(xs, ys, a, b, y0, y1, X, Y) {
  const pts = [];
  for (let i = 0; i < xs.length; i++)
    if (xs[i] >= a && xs[i] <= b) pts.push(`${X(xs[i]).toFixed(1)},${Y(ys[i]).toFixed(1)}`);
  return [...pts, `${X(b).toFixed(1)},${Y(y1).toFixed(1)}`, `${X(a).toFixed(1)},${Y(y0).toFixed(1)}`].join(" ");
}

// ---------- Results tab

async function tabResults(body, exp, t) {
  if (!exp.last_run || exp.last_run.status !== "done") {
    put(body, h("div", { class: "layr-empty" }, "No results yet: run the experiment."));
    return;
  }
  const run = await call("run_get", runArgs(exp));
  if (t !== token) return;
  if (run.legacy) {
    put(
      body,
      h(
        "div",
        { class: "layr-empty" },
        "This run is from an older version of the plugin: press Run to calculate it again.",
      ),
    );
    return;
  }
  const outs = run.outputs || {};
  const tableNames = Object.keys(outs).filter((n) => outs[n].kind === "table");
  const figs = Object.keys(outs).filter((n) => outs[n].kind === "figure");
  const stepTables = (run.steps || []).filter((s) => s.table).map((s) => `step:${s.function}`);
  if (!S.table || !(tableNames.includes(S.table) || stepTables.includes(S.table))) S.table = tableNames[0];
  const figBox = h("div", { class: "figs" });
  for (const f of figs) {
    // a figure is read like every other value (value_get and its file); the figure tool only for an old run's
    const src = outs[f].file
      ? call("value_get", { run: run.run, file: outs[f].file }).then((r) =>
          r.data ? `data:image/png;base64,${r.data}` : null,
        )
      : figureOf({ run: run.run, output: f });
    src
      .then((s) => s && figBox.append(h("img", { src: s, class: "fig", alt: outs[f].label || f })))
      .catch(() => {});
  }
  const sel = h(
    "select",
    { class: "layr-select", onchange: (e) => go({ table: e.target.value }) },
    h(
      "optgroup",
      { label: "Results" },
      tableNames.map((n) => h("option", { value: n, selected: n === S.table }, outs[n].label || n)),
    ),
    h(
      "optgroup",
      { label: "Each step's table" },
      stepTables.map((n) => h("option", { value: n, selected: n === S.table }, human(n.slice(5)))),
    ),
  );
  const tbox = h("div", {}, h("span", { class: "layr-spinner" }));
  put(
    body,
    h(
      "div",
      { class: "page wide" },
      figBox,
      h(
        "div",
        { class: "section-head" },
        sel,
        h("span", { class: "layr-muted small" }, " Click a number to see how it was calculated."),
      ),
      tbox,
    ),
  );
  if (!S.table) {
    put(tbox, h("div", { class: "layr-empty" }, "This run returned no tables."));
    return;
  }
  const data = await call("result_table", { ...runArgs(exp), table: S.table, limit: 2000 });
  if (t !== token) return;
  const filterCols = data.columns.filter((c) =>
    ["compound", "substrate", "substrate_fed", "strain", "role", "status", "level", "category"].includes(c),
  );
  const filt = {};
  const draw = () => {
    const rows = data.rows.filter((r) => Object.entries(filt).every(([c, v]) => !v || String(r[c]) === v));
    put(
      tbox,
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
      table(rows, data.columns, { onCell: (r, c) => showProvenance(exp, S.table, r, c) }),
    );
  };
  draw();
}

const PROV_KEYS = [
  "injection",
  "compound",
  "strain",
  "substrate",
  "substrate_fed",
  "time_min",
  "interval_min",
  "replicate",
  "std_series",
  "level",
  "category",
];

async function showProvenance(exp, tableName, row, column) {
  let keys = PROV_KEYS.filter((k) => k in row && row[k] !== null && row[k] !== "" && k !== column);
  if (!keys.length) keys = Object.keys(row).filter((k) => typeof row[k] === "string");
  const where = Object.fromEntries(keys.map((k) => [k, row[k]]));
  try {
    const p = await call("provenance", { ...runArgs(exp), table: tableName, where, column });
    const chain = p.chain || [];
    const body = chain.length
      ? chain.map((c, i) =>
          h(
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
      buttons: [{ label: "Close", id: null }],
    });
  } catch (err) {
    fail(err);
  }
}

// ---------- Checks tab

async function tabChecks(body, exp, t) {
  if (!exp.last_run || exp.last_run.status !== "done") {
    put(body, h("div", { class: "layr-empty" }, "No checks yet: run the experiment."));
    return;
  }
  const fl = await call("result_table", { ...runArgs(exp), table: "flags", limit: 2000 }).catch(() => null);
  if (t !== token) return;
  if (!fl) {
    put(body, h("div", { class: "layr-empty" }, "This method has no quality checks."));
    return;
  }
  const groups = {};
  for (const f of fl.rows) (groups[f.category] ||= []).push(f);
  const order = { error: 0, warning: 1, info: 2 };
  put(
    body,
    h(
      "div",
      { class: "page wide" },
      h(
        "p",
        { class: "layr-muted" },
        "Nothing is changed by these checks: they say what to look at. Click one with an injection to open its peak.",
      ),
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
                  {
                    class: `${f.level} ${f.injection !== "" && f.injection != null ? "click" : ""}`,
                    onclick: () => {
                      if (f.injection !== "" && f.injection != null)
                        go({ tab: "peaks", inj: Number(f.injection), comp: f.compound || S.comp, injFilter: "all" });
                    },
                  },
                  badge(f.level, f.level === "error" ? "danger" : f.level === "warning" ? "warning" : ""),
                  " ",
                  f.injection !== "" && f.injection != null ? h("b", {}, `#${f.injection} `) : null,
                  f.compound ? h("b", {}, `${f.compound}: `) : null,
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
  const decide = async (c, decision) => {
    try {
      await call("correction_decide", { experiment: exp.id, correction: c.id, decision });
      startRun(exp.id);
    } catch (err) {
      fail(err);
    }
  };
  const statusKind = { approved: "success", proposed: "warning", rejected: "", removed: "", replaced: "" };
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
      corr.length
        ? h(
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
                  ["Injection", "What", "Reason", "By", "Status", ""].map((c) => h("th", {}, c)),
                ),
              ),
              h(
                "tbody",
                {},
                corr.map((c) =>
                  h(
                    "tr",
                    { class: c.status === "approved" || c.status === "proposed" ? "" : "dim" },
                    h("td", {}, `#${c.injection}`),
                    h(
                      "td",
                      {},
                      c.kind === "rename"
                        ? `rename to ${c.new_name}`
                        : c.kind === "exclude"
                          ? `exclude ${c.compound || "whole injection"}`
                          : `${c.compound}: ${c.start_min}–${c.end_min} min by hand`,
                    ),
                    h("td", {}, c.reason),
                    h("td", { class: "layr-muted small" }, c.by, h("br"), when(c.at)),
                    h("td", {}, badge(c.status, statusKind[c.status])),
                    h(
                      "td",
                      { class: "nowrap" },
                      c.status === "proposed"
                        ? [
                            h(
                              "button",
                              { class: "layr-button small primary", onclick: () => decide(c, "approve") },
                              "Approve",
                            ),
                            h(
                              "button",
                              { class: "layr-button small ghost", onclick: () => decide(c, "reject") },
                              "Reject",
                            ),
                          ]
                        : null,
                      c.status === "approved"
                        ? h("button", { class: "layr-button small ghost", onclick: () => decide(c, "remove") }, "Undo")
                        : null,
                      c.kind !== "rename"
                        ? h(
                            "button",
                            {
                              class: "layr-button small ghost",
                              onclick: () =>
                                go({ tab: "peaks", inj: c.injection, comp: c.compound || S.comp, injFilter: "all" }),
                            },
                            "Show",
                          )
                        : null,
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
  const files = (exp.method_inputs || []).map((inp) => {
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
      layr.ui.notify("Saved. Press Run to recalculate.", "success");
      render();
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
      h("h4", {}, "Raw data"),
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
          {
            class: "layr-button danger ghost",
            onclick: async () => {
              if (
                await openDialog({
                  title: "Delete this experiment?",
                  body: "Its runs and its rows in the results database go too. Raw files stay in the store.",
                  buttons: [
                    { label: "Cancel", id: false, kind: "ghost" },
                    { label: "Delete", id: true, kind: "danger" },
                  ],
                })
              ) {
                try {
                  await call("experiment_delete", { experiment: exp.id });
                  go({ view: "home", exp: null });
                } catch (err) {
                  fail(err);
                }
              }
            },
          },
          "Delete experiment",
        ),
        h("span", { class: "layr-spacer" }),
        h("button", { class: "layr-button primary", onclick: saveAll }, "Save settings"),
      ),
    ),
  );
  if (S.setting) {
    document.getElementById(`set-${S.setting}`)?.scrollIntoView({ block: "center" });
    S.setting = null;
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
      step: "any",
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
  if (p.type === "compounds")
    return rowsEditor(
      Object.entries(v || {}).map(([name, sp]) => ({ name, rt: sp.rt, tol: sp.rt_tolerance_min })),
      [
        ["name", "Compound", "text"],
        ["rt", "Retention time (min)", "number"],
        ["tol", "Own tolerance (min)", "number"],
      ],
      (rows) =>
        set(
          Object.fromEntries(
            rows
              .filter((r) => r.name)
              .map((r) => [
                r.name,
                r.tol != null && r.tol !== "" ? { rt: r.rt, rt_tolerance_min: r.tol } : { rt: r.rt },
              ]),
          ),
        ),
    );
  if (p.type === "patterns")
    return rowsEditor(
      (v || []).map((x) => ({ ...x })),
      [
        ["role", "Role", "text"],
        ["regex", "Pattern (regular expression)", "text"],
      ],
      (rows) => set(rows.filter((r) => r.regex)),
    );
  if (p.type === "biomass") {
    const isMap = v && typeof v === "object";
    return rowsEditor(
      isMap ? Object.entries(v).map(([sub, val]) => ({ sub, val })) : [{ sub: "*", val: v }],
      [
        ["sub", "Fed substrate (* = all)", "text"],
        ["val", "gCDW/L", "number"],
      ],
      (rows) => {
        const r = rows.filter((x) => x.sub && x.val != null && x.val !== "");
        set(r.length === 1 && r[0].sub === "*" ? r[0].val : Object.fromEntries(r.map((x) => [x.sub, x.val])));
      },
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

function rowsEditor(rows, cols, onChange) {
  const wrap = h("div", { class: "rows-editor" });
  const draw = () => {
    put(
      wrap,
      h(
        "table",
        {},
        h(
          "tr",
          {},
          cols.map(([, label]) => h("th", {}, label)),
          h("th"),
        ),
        rows.map((r, i) =>
          h(
            "tr",
            {},
            cols.map(([k, , type]) =>
              h(
                "td",
                {},
                h("input", {
                  class: `layr-input ${type === "number" ? "num" : ""}`,
                  type: type === "number" ? "number" : "text",
                  step: "any",
                  value: r[k] ?? "",
                  onchange: (e) => {
                    r[k] = type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value;
                    onChange(rows);
                  },
                }),
              ),
            ),
            h(
              "td",
              {},
              h(
                "button",
                {
                  class: "layr-button small ghost icon",
                  title: "Remove",
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
        "+ Add",
      ),
    );
  };
  draw();
  return wrap;
}

// ------------------------------------------------------------------------------------------------ method (code)

// The method editor: the outline on the left, the code in the middle (CodeMirror 6 through editor.js), and on the right
// the function at the cursor explained (its sentence, its formula drawn from the code, the last run's numbers) and the
// style checker's messages. Help while typing comes from the backend (Jedi), the shown run's values sit at the ends of
// the lines of the function at the cursor, its checks are folded. Saving makes a new version; a change Claude proposed
// waits in a banner and is reviewed chunk by chunk.
//
// The parts (banners, outline, editor with its status line, inspector, draft review) are drawn by their own functions
// into `ME.els`, so another layout (the flow above the editor) can place them again.
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

/** Ends the method screen: the flow, the editor, its hover card and a draft review are taken off the page. */
function closeMethod() {
  if (!ME) return;
  ME.review?.merge?.destroy();
  ME.flow?.destroy();
  ME.ed?.destroy();
  clearTimeout(ME.checkTimer);
  clearTimeout(ME.cursorTimer);
  clearTimeout(ME.metaTimer);
  ME.tickets.trial++;
  ME = null;
}

// For the end-to-end test and for poking at it in the developer tools.
window.__lab = {
  editor: () => ME?.ed || null,
  merge: () => ME?.review?.merge || null,
  flow: () => (S.view === "method" ? ME?.flow : EF?.flow) || null,
  format: () => formatCode(),
  claudeStep: (o) => claudeStep(o),
  state: () =>
    ME && {
      dirty: ME.dirty,
      trialRun: ME.trialRun,
      trialNote: ME.trialNote?.text || "",
      busy: !!ME.busy || ME.inspecting > 0,
      numbersExp: ME.numbersExp,
      fn: ME.fn,
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
    // folded to one line until she asks: the method screen keeps its room for the flow and the code
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

async function renderMethod(t) {
  const [m, docs, exps] = await Promise.all([
    call("method_get", { method: S.method }),
    toolboxList(),
    call("experiments"),
  ]);
  if (t !== token) return;
  await checkAi();
  layr.panel.setTitle(m.name).catch(() => {});
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: `${m.name} (method)` }]);
  const mine = exps.filter((e) => e.method === m.id);
  const keep = ME && ME.method === m.id && ME.dirty ? ME.ed.getValue() : null;
  const keepAi = ME && ME.method === m.id ? ME.aiStep : null;
  closeMethod();
  const numbersExp = mine.some((e) => e.experiment === S.numbersExp)
    ? S.numbersExp
    : m.reference || mine.find((e) => e.last_run?.status === "done")?.experiment || null;
  ME = {
    method: m.id,
    m,
    saved: m.source,
    dirty: false,
    messages: m.check || [],
    summary: m.summary,
    fn: S.fn || null,
    toolbox: docs,
    exps: mine,
    numbersExp,
    pickedRun: null, // an earlier run picked in the flow
    trialRun: null, // the newest trial run of the code as it is in the editor
    trialRunSource: null, // the code that trial run calculated
    inspecting: 0, // explanations being fetched
    trialSource: m.source, // the code the shown numbers belong to (a trial run is made when it differs)
    trialNote: null,
    flowSource: keep ?? m.source, // the code the flow was last drawn from
    flowSel: null,
    aiStep: keepAi, // {fn}: a step Claude wrote, not saved yet
    ai: null,
    inside: new Map(), // "run or experiment|function" → {promise, value} of function_inside
    tickets: { hover: 0, values: 0, check: 0, trial: 0 },
    review: null,
    busy: false,
  };
  const els = methodParts();
  ME.els = els;
  put(view, h("div", { class: "mv" }, els.banners, els.main));
  layoutMethod();
  ME.ed = mountMethodEditor(els.editor, keep ?? m.source);
  ME.flow = mountMethodFlow(els.flow, keep ?? m.source);
  methodActions();
  drawBanners();
  drawOutline();
  drawLint();
  drawStatus();
  if (keep) onMethodEdit();
  const target = S.line || (ME.summary?.outline || []).find((x) => x.name === ME.fn)?.line;
  if (target) setTimeout(() => ME?.ed.reveal(target), 30);
  S.line = null;
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

/** The method screen's parts, empty: each is filled by its own draw function. */
function methodParts() {
  const els = {
    banners: h("div", { class: "mv-banners" }),
    main: h("div", { class: "mv-main" }),
    flow: h("div", { class: "mv-flow" }),
    divider: h("div", { class: "mv-divider", title: "Drag to give the flow or the code more room" }),
    outline: h("div", { class: "mv-outline" }),
    editor: h("div", { class: "mv-editor" }),
    status: h("div", { class: "mv-status small" }),
    inspector: h("div", { class: "mv-inspector" }),
  };
  els.middle = h("div", { class: "mv-middle" }, els.editor, els.status);
  els.grid = h("div", { class: "mv-grid" }, els.outline, els.middle, els.inspector);
  return els;
}

/** The flow on top, a divider she can drag (remembered), the outline, the code and the explanation under it. */
function layoutMethod() {
  const els = ME.els;
  const split = h("div", { class: "mv-split" }, els.flow, els.divider, els.grid);
  put(els.main, split);
  const apply = () => {
    const f = Math.min(0.8, Math.max(0.15, Number(S.flowFrac) || 0.42));
    split.style.gridTemplateRows = `minmax(120px, ${(f * 100).toFixed(1)}%) 7px minmax(160px, 1fr)`;
  };
  apply();
  els.divider.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    els.divider.setPointerCapture(e.pointerId);
    els.divider.classList.add("dragging");
    const box = split.getBoundingClientRect();
    const move = (ev) => {
      S.flowFrac = (ev.clientY - box.top) / Math.max(1, box.height);
      apply();
    };
    const up = () => {
      els.divider.removeEventListener("pointermove", move);
      els.divider.removeEventListener("pointerup", up);
      els.divider.classList.remove("dragging");
      apply();
      save();
    };
    els.divider.addEventListener("pointermove", move);
    els.divider.addEventListener("pointerup", up);
  });
  els.divider.addEventListener("dblclick", () => {
    S.flowFrac = 0.42;
    apply();
    save();
  });
}

// ---------- the flow above the code

/**
 * Where the values shown come from: a trial run of the code in the editor, an earlier run, or the latest run; and the
 * code they belong to (`source`), so that numbers of other code are not passed off as this code's.
 */
function dataRef() {
  if (!ME?.numbersExp) return null;
  if (ME.trialRun) return { key: ME.trialRun, args: { run: ME.trialRun }, source: ME.trialRunSource };
  if (ME.pickedRun) return { key: ME.pickedRun, args: { run: ME.pickedRun }, source: null };
  return { key: ME.numbersExp, args: { experiment: ME.numbersExp }, source: ME.saved };
}

/** The code in the editor is being calculated (or will be after the pause): the numbers shown are of other code. */
function numbersComing() {
  const ref = dataRef();
  return !!ref && S.autoRecalc !== false && (ME.busy || ME.trialGoing) && ref.source !== ME.ed.getValue() &&
    !ME.pickedRun;
}

function mountMethodFlow(host, source) {
  // Once the flow has drawn for the first time, the box of the function at the cursor is selected.
  // The latest run is of an older version of the code: it is calculated as it is now (a trial run, steps reused), so
  // every box has its numbers without her pressing anything.
  const sync = (tries = 0) => {
    if (!ME || (ME.flow && ME.flow !== flow)) return;
    if (flow.graph && ME.ed) {
      cursorIn(ME.ed.cursor().line);
      if (flow.graph.stale && S.autoRecalc !== false && ME.numbersExp && !ME.trialRun && !ME.pickedRun && !ME.busy)
        recalc(ME.ed.getValue());
    } else if (tries < 80) setTimeout(() => sync(tries + 1), 250);
  };
  setTimeout(sync, 250);
  const flow = mountFlow(host, {
    call,
    tex,
    method: ME.method,
    source,
    experiment: ME.numbersExp,
    run: null,
    editable: true,
    inside: "host",
    claude: AI.claude,
    figure: figureOf,
    onSource: (src, info) => flowEdited(src, info),
    onSetting: (id, value) => flowSetting(id, value),
    onPick: (p) => flowPicked(p),
    onSelect: (s) => flowSelected(s),
    onOpen: (o) => flowOpened(o),
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

/** A setting changed in a box of the method's flow: the picked experiment keeps it, and it is calculated again. */
async function flowSetting(id, value) {
  if (!ME?.numbersExp) return layr.ui.notify("Pick an experiment in the flow first: settings belong to it.", "info");
  try {
    await call("experiment_update", { experiment: ME.numbersExp, params: { [id]: value } });
    recalc(ME.ed.getValue());
  } catch (err) {
    fail(err);
  }
}

function flowPicked({ experiment, run }) {
  if (!ME) return;
  ME.numbersExp = experiment || null;
  S.numbersExp = ME.numbersExp;
  ME.pickedRun = run || null;
  ME.trialRun = null;
  ME.trialSource = ME.dirty ? null : ME.saved;
  ME.trialNote = null;
  save();
  valuesChanged();
  drawInspector();
  if (ME.dirty && S.autoRecalc !== false && experiment) settle();
}

/** A box was selected: its function (or its recipe line) is shown in the editor. */
function flowSelected({ node, fn, line }) {
  if (!ME) return;
  if (ME.selFromEditor === node) return; // the editor's cursor selected it: nothing to move
  ME.flowSel = node;
  const own = fn && (ME.summary?.outline || []).find((f) => f.name === fn && f.kind !== "recipe");
  const toolbox = !own && fn && (ME.toolbox || []).some((t) => t.name === fn);
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
  } else if (fn && (ME.toolbox || []).some((t) => t.name === fn)) {
    ME.fn = fn;
    S.fn = fn;
    save();
    drawOutline();
    drawInspector();
    drawValues();
  }
}

function flowOpened(o) {
  if (!ME) return;
  if (o.what === "function") {
    if (o.created && o.fn) return cursorInFunction(o.fn);
    const own = o.fn && (ME.summary?.outline || []).find((f) => f.name === o.fn);
    const line = own?.line || (o.fn ? defLine(o.fn) : null) || o.line;
    if (line) jumpTo(line, own?.name || o.fn || null);
    return;
  }
  if (o.what === "claude") return claudeStep(o);
  if (o.what === "new-experiment") return newExperimentHere();
  const exp = ME.numbersExp;
  if (!exp) return layr.ui.notify("Pick an experiment in the flow first.", "info");
  const tab = { results: "results", settings: "settings", corrections: "corrections", checks: "checks" }[o.what];
  if (tab) go({ view: "experiment", exp, tab, table: o.table || null, setting: o.setting || null });
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

/**
 * The flow asked for an experiment for this method (it has a raw file and no experiment yet): she picks each file,
 * the experiment is made with the method as saved, calculated, and the flow shows its values.
 */
async function newExperimentHere() {
  try {
    if (ME.dirty) {
      const ok = await openDialog({
        title: "Save the method first?",
        body: "An experiment uses the method as it is saved. Save your changes as a new version, then pick the files.",
        buttons: [
          { label: "Cancel", id: false, kind: "ghost" },
          { label: "Save…", id: true, kind: "primary" },
        ],
      });
      if (!ok || !(await saveMethod())) return;
    }
    const inputs = (ME.flow?.graph?.inputs || []).filter((i) => i.kind !== "corrections");
    if (!inputs.length) return layr.ui.notify("Add a raw file to the method first (+ Raw file in the flow).", "info");
    const files = {};
    for (const i of inputs) {
      layr.ui.notify(`Choose the file for: ${i.label || i.id}`, "info");
      const f = await pickAndStore(i.types || []);
      if (!f) return;
      files[i.id] = f;
    }
    const e = await call("experiment_create", { title: `${ME.m.name}: ${Object.values(files)[0].name}`,
      method: ME.method, files });
    ME.numbersExp = e.experiment;
    S.numbersExp = e.experiment;
    save();
    setStatus("Calculating…");
    const r = await call("run", { experiment: e.experiment, wait_s: 55 });
    setStatus("");
    if (r.status !== "done") layr.ui.notify(`The calculation stopped: ${cleanError(r.error)}`, "error");
    if (!ME) return;
    ME.trialRun = null;
    ME.pickedRun = null;
    ME.flow.update({ experiment: e.experiment, run: null, refresh: true });
    valuesChanged();
    drawInspector();
  } catch (err) {
    setStatus("");
    fail(err);
  }
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
  const box = h("div", { class: "review-merge" });
  const counts = h("span", { class: "layr-muted small" });
  ME.review = { merge: null, ai: true };
  const close = () => {
    closeReview();
    put(ME.els.main);
    layoutMethod();
    drawBanners();
  };
  put(
    ME.els.main,
    h(
      "div",
      { class: "review" },
      h(
        "div",
        { class: "review-head" },
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
        { class: "row-gap review-buttons" },
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
  const trial = S.autoRecalc !== false && ME.numbersExp && source !== ME.trialSource;
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
  if (!ME?.numbersExp) return;
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
  const exp = ME.numbersExp;
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
    ME.pickedRun = null;
    ME.flowSource = source;
    // the flow, the values in the code and the explanation are fetched at once (not one after the other)
    ME.inside = new Map();
    await Promise.all([ME.flow?.update({ source, run: r.run }), drawValues(), drawInspector()]);
    if (!ME || ME.tickets.trial !== n) return;
  }
  const total = (r.cache?.hits || 0) + (r.cache?.misses || 0);
  if (r.status === "done")
    setTrialNote(
      `calculated just now${r.cache?.hits ? ` · ${r.cache.hits} of ${total} reused` : ""}`,
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

function onMethodEdit({ now = false } = {}) {
  if (!ME) return;
  const wasDirty = ME.dirty;
  ME.dirty = ME.ed.getValue() !== ME.saved;
  if (wasDirty !== ME.dirty) {
    methodActions();
    drawBanners();
  }
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
    drawStatus();
    return;
  }
  const got = await insideOf(ref, fn);
  if (!ME || ME.tickets.values !== n) return;
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

// ---------- the toolbar, the status line, the checker's marks

function methodActions() {
  if (!ME) return;
  setActions([
    h("button", { class: "layr-button ghost", onclick: () => go({ view: "guide" }) }, "Python guide"),
    h("button", { class: "layr-button ghost", onclick: showVersions }, "Versions"),
    h(
      "button",
      {
        class: "layr-button ghost",
        onclick: (e) =>
          layr.ui.contextMenu(
            [
              { label: "Format the code (tidy layout)", icon: "format_align_left", run: formatCode },
              "-",
              {
                label: "Folder that runs without layr (zip)",
                icon: "folder_zip",
                run: () => exportFolder(ME.numbersExp ? { experiment: ME.numbersExp } : { method: ME.method }),
              },
              { label: "Duplicate the method…", icon: "content_copy", run: () => duplicate(ME.m) },
            ],
            e,
          ),
      },
      "More ▾",
    ),
    h("button", { class: "layr-button primary", disabled: !ME.dirty, onclick: () => saveMethod() }, "Save"),
  ]);
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
      { class: "recalc-toggle", title: "After a change, calculate what it changed on the picked experiment" },
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
            },
            h("span", { class: "ol-name" }, f.name),
            count(f.name)
              ? h("span", { class: "lint-dot", title: `${count(f.name)} style notes` }, count(f.name))
              : null,
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
    closeReview();
    if (decision === "reject" && ME.m.draft_only) go({ view: "home", method: null });
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
    const box = h("div", { class: "review-merge" });
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
        { class: "review" },
        h(
          "div",
          { class: "review-head" },
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
          { class: "row-gap review-buttons" },
          h("button", { class: "layr-button ghost", onclick: () => (closeReview(), render()) }, "Back to the code"),
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

/** Saves the code as a new version (she says what changed); true when it was saved. */
async function saveMethod() {
  if (!ME || !ME.dirty) return false;
  const source = ME.ed.getValue();
  const note = h("input", {
    class: "layr-input",
    placeholder: "e.g. Rates from 0-20 min instead of 0-30",
    value: ME.aiStep ? `Added ${ME.aiStep.fn} (written by Claude, checked by me)` : ME.fn ? `Changed ${ME.fn}` : "",
  });
  setTimeout(() => note.select(), 30);
  const change = await openDialog({
    title: "Save a new version",
    body: [h("label", { class: "layr-label" }, "What changed, and why? (for the version history)"), note],
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
    layr.ui.notify(`Saved as version ${r.version}`, "success");
    await refreshMethodMeta();
    return true;
  } catch (err) {
    fail(err);
    return false;
  }
}

// After a save or an event: new versions, drafts and comparisons, without touching the code being edited.
async function refreshMethodMeta({ late = false } = {}) {
  if (!ME) return;
  try {
    const m = await call("method_get", { method: ME.method });
    if (!ME) return;
    ME.m = m;
    if (!ME.dirty && m.source !== ME.ed.getValue()) {
      ME.ed.setValue(m.source, { keepHistory: true });
      ME.dirty = false;
      drawLint();
      drawValues();
    }
    ME.saved = ME.dirty ? ME.saved : m.source;
    methodActions();
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
                        render();
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
  const shownExp = ME.exps.find((e) => e.experiment === ME.numbersExp);
  const picker = ME.numbersExp
    ? h(
        "div",
        { class: "numbers-from small" },
        "Numbers from ",
        h("b", {}, shownExp?.title || "the picked experiment"),
        h(
          "span",
          { class: "layr-muted" },
          ME.trialRun ? " (your change, calculated)" : ME.pickedRun ? " (an earlier run)" : " (its latest run)",
        ),
        ME.numbersExp
          ? h(
              "label",
              { class: "ref-toggle", title: "Rerun it after every change and list the numbers that moved" },
              h("input", {
                type: "checkbox",
                checked: ME.m.reference === ME.numbersExp,
                onchange: async (e) => {
                  try {
                    await call("method_reference", {
                      method: ME.method,
                      experiment: e.target.checked ? ME.numbersExp : null,
                    });
                    await refreshMethodMeta();
                  } catch (err) {
                    fail(err);
                  }
                },
              }),
              " reference",
            )
          : null,
      )
    : h(
        "div",
        { class: "layr-muted small" },
        ME.exps.length
          ? "Pick an experiment in the flow above: its numbers show here and in the code."
          : "No experiment uses this method yet: its numbers show here after a run.",
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
        ME.exps.find((e) => e.experiment === ME.numbersExp) ? { id: ME.numbersExp } : null,
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
  } else if (ME.numbersExp) {
    parts.push(h("div", { class: "layr-muted small" }, "Not called in the last run (or it hasn't run yet)."));
  }
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
    closeMethod();
    go({ view: "method", method: id.value.trim(), fn: null });
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

// ------------------------------------------------------------------------------------------------ database

async function renderDatabase(t) {
  const tables = await call("database_tables");
  if (t !== token) return;
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: "Results database" }]);
  setActions([]);
  layr.panel.setTitle("Results database").catch(() => {});
  if (!tables.length) {
    put(
      view,
      h(
        "div",
        { class: "layr-empty" },
        "Empty until an experiment has been run: every run adds its results here, to compare across the PhD.",
      ),
    );
    return;
  }
  if (!S.dbTable || !tables.some((x) => x.table === S.dbTable))
    S.dbTable = tables.find((x) => x.table === "activities")?.table || tables[0].table;
  const data = await call("database_query", { table: S.dbTable, limit: 20000 });
  if (t !== token) return;
  const filterCols = data.columns.filter((c) =>
    ["experiment_title", "strain", "substrate", "substrate_fed", "compound", "role"].includes(c),
  );
  const filt = {};
  const tbox = h("div", {});
  const visible = () => data.rows.filter((r) => Object.entries(filt).every(([c, v]) => !v || String(r[c]) === v));
  const draw = () => {
    const rows = visible();
    put(
      tbox,
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
            h("option", { value: "" }, `All ${c.replace("_", " ")}`),
            [...new Set(data.rows.map((r) => String(r[c] ?? "")))]
              .sort()
              .map((v) => h("option", { value: v, selected: filt[c] === v }, v)),
          ),
        ),
        h("span", { class: "layr-muted small" }, `${rows.length} rows`),
      ),
      table(
        rows,
        data.columns.filter((c) => c !== "run"),
        { limit: 500 },
      ),
    );
  };
  const csv = async () => {
    const rows = visible();
    const esc = (v) => {
      const s = fmt(v);
      return /[",\n;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const text = [data.columns.join(","), ...rows.map((r) => data.columns.map((c) => esc(r[c])).join(","))].join("\n");
    try {
      const ref = await layr.files.saveAs({ name: `${S.dbTable}.csv`, types: [".csv"] });
      if (ref) {
        await layr.files.write(ref.id, text);
        layr.ui.notify(`Saved ${ref.name}`, "success");
      }
    } catch (err) {
      fail(err);
    }
  };
  put(
    view,
    h(
      "div",
      { class: "page wide" },
      h(
        "p",
        { class: "layr-muted" },
        "The latest results of every experiment, side by side. Ask Claude to compare strains or conditions: it reads this database.",
      ),
      h(
        "div",
        { class: "section-head" },
        h(
          "select",
          { class: "layr-select", onchange: (e) => go({ dbTable: e.target.value }) },
          tables.map((x) =>
            h(
              "option",
              { value: x.table, selected: x.table === S.dbTable },
              `${x.table} (${x.experiments} experiments, ${x.rows} rows)`,
            ),
          ),
        ),
        h("span", { class: "layr-spacer" }),
        h("button", { class: "layr-button", onclick: csv }, "Save as CSV…"),
      ),
      tbox,
    ),
  );
  draw();
}

// ------------------------------------------------------------------------------------------------ main

async function render() {
  const t = ++token;
  try {
    if (S.view === "experiment" && S.exp) await renderExperiment(t);
    else if (S.view === "method" && S.method) await renderMethod(t);
    else if (S.view === "new") await renderNew(t, S.preset);
    else if (S.view === "database") await renderDatabase(t);
    else if (S.view === "guide") await renderGuide(t);
    else await renderHome(t);
  } catch (err) {
    if (t !== token) return;
    if (/No experiment|No method/.test(err?.message || "")) {
      go({ view: "home", exp: null, method: null });
      return;
    }
    put(
      view,
      h(
        "div",
        { class: "layr-empty" },
        h("div", { class: "layr-error" }, err?.message || String(err)),
        h("button", { class: "layr-button", onclick: () => go({ view: "home" }) }, "Back to the experiments"),
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
const opened = layr.files.current?.();
if (opened && !(layr.info.state || {}).openedFile) {
  try {
    const f = await storeFileRef(opened);
    S.preset = f;
    S.view = "new";
    layr.panel.setState({ ...S, openedFile: opened.id }).catch(() => {});
  } catch (err) {
    fail(err);
  }
}
checkAi();
try {
  layr.connections.onChange?.(() => checkAi().then(() => S.view === "method" && drawInspector()));
} catch {
  // an older layr: checked once
}
layr.events.on("research:lab", (ev) => {
  if (ev.what === "run" && ev.trial) {
    // A trial run (the method screen calculating a change): it lights up its step, and it is not the experiment's
    // new latest run, so nothing else is redrawn.
    if (S.view === "method" && ME && ev.experiment === ME.numbersExp)
      ME.flow?.setRunning(!ev.finished && ev.step ? { step: ev.step } : null);
    return;
  }
  if (ev.what === "run" && !ev.finished && ev.step) {
    if (live && live.exp === ev.experiment) {
      live.step = ev.step;
      setStatus(`Calculating: ${human(ev.step)}`);
    }
    if (S.view === "experiment" && EF?.exp === ev.experiment) EF.flow.setRunning({ step: ev.step });
  }
  // while the panel's own run is going, the screen is drawn again when it's done
  if (live && (ev.what === "run" || ev.experiment === live.exp)) return;
  if (ev.what === "run" && !ev.finished) return;
  if (S.view === "method") {
    if ((ev.method && ev.method === S.method) || ev.what === "comparison") refreshMethodMeta();
    if (ev.what === "run" && ME && ev.experiment === ME.numbersExp && !ME.trialRun && !ME.pickedRun) {
      ME.flow?.update({});
      valuesChanged();
    }
    return;
  }
  if (S.view === "home" || (S.view === "experiment" && (!ev.experiment || ev.experiment === S.exp))) render();
});
window.addEventListener("resize", () => {
  if (S.tab === "peaks" && S.view === "experiment") {
    clearTimeout(window.__rz);
    window.__rz = setTimeout(render, 250);
  }
});
window.addEventListener("beforeunload", (e) => {
  if (ME?.dirty) e.preventDefault();
});
render();

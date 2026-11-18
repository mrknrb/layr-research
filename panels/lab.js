// Experiments: methods explained step by step, GC peak review, results, checks, corrections, the PhD database.
// Everything goes through the plugin's own tools (layr.tools.run), the same ones AI agents use.

const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");
const actions = document.getElementById("actions");
const statusEl = document.getElementById("status");
const dialog = document.getElementById("dialog");

const S = {
  view: "home",
  exp: null,
  tab: "method",
  method: null,
  step: null,
  general: false,
  inj: null,
  comp: null,
  injFilter: "all",
  table: null,
  dbTable: null,
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

function openDialog({ title, body, buttons }) {
  return new Promise((resolve) => {
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

function table(rows, columns, { onRow, limit = 200 } = {}) {
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
            cols.map((c) => h("td", { class: typeof r[c] === "number" ? "num" : "" }, fmt(r[c]))),
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
    h("button", { class: "layr-button ghost", onclick: () => go({ view: "database" }) }, "Results database"),
  ]);
  const cards = exps.map((e) =>
    h(
      "div",
      {
        class: "layr-card exp-card click",
        onclick: () => go({ view: "experiment", exp: e.experiment, tab: "method", step: null }),
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
                  e.last_run.status === "done" ? "calculated" : "failed",
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
        h("div", { class: "section-head" }, h("h2", {}, "Methods"), h("span", { class: "layr-spacer" })),
        h(
          "p",
          { class: "layr-muted" },
          "A method is the recipe: the same steps for every experiment, each explained with its formulas. " +
            "Ask Claude to make a new one for another kind of analysis; every change becomes a version you can go back from.",
        ),
        h(
          "ul",
          { class: "layr-list lined" },
          methods.map((m) =>
            h(
              "li",
              { class: "click", onclick: () => go({ view: "method", method: m.id, step: null }) },
              h("b", {}, m.name),
              " ",
              h(
                "span",
                { class: "layr-muted" },
                `version ${m.version} · ${m.steps} steps${m.builtin ? " · built in" : ""}`,
              ),
              h("div", { class: "layr-muted small" }, m.description),
            ),
          ),
        ),
      ),
    ),
  );
}

async function makeExample() {
  try {
    setStatus("Making the example…");
    const e = await call("lab_sample");
    go({ view: "experiment", exp: e.experiment, tab: "method", step: null });
    startRun(e.experiment);
  } catch (err) {
    fail(err);
  } finally {
    setStatus("");
  }
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
    methods.map((m) => h("option", { value: m.id }, m.name)),
  );
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
      go({ view: "experiment", exp: e.experiment, tab: "settings", step: null });
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
  ["method", "Method"],
  ["peaks", "Peaks"],
  ["results", "Results"],
  ["checks", "Checks"],
  ["corrections", "Corrections"],
  ["settings", "Settings"],
];

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
  const tabs = h(
    "div",
    { class: "tabs" },
    TABS.filter(([id]) => id !== "peaks" || isGc).map(([id, label]) =>
      h(
        "button",
        { class: `tab ${S.tab === id ? "active" : ""}`, onclick: () => go({ tab: id }) },
        label,
        id === "checks" && (flags.error || flags.warning)
          ? h("span", { class: "count" }, (flags.error || 0) + (flags.warning || 0))
          : null,
        id === "corrections" && proposed ? h("span", { class: "count warn" }, proposed) : null,
      ),
    ),
  );
  const body = h("div", { class: "tab-body" });
  const banner =
    exp.last_run?.status === "failed"
      ? h("div", { class: "banner danger" }, "The last run failed: ", exp.last_run.error || "")
      : !exp.last_run
        ? h("div", { class: "banner" }, "Not calculated yet. Check the settings, then press Run.")
        : exp.changed && exp.changed > (exp.last_run.started || exp.last_run.finished)
          ? h("div", { class: "banner" }, "Changed since the last run: press Run to recalculate.")
          : null;
  put(view, h("div", { class: "exp" }, tabs, banner, body));
  const tab = S.tab === "peaks" && !isGc ? "method" : S.tab;
  const fn =
    {
      method: tabMethod,
      peaks: tabPeaks,
      results: tabResults,
      checks: tabChecks,
      corrections: tabCorrections,
      settings: tabSettings,
    }[tab] || tabMethod;
  await fn(body, exp, t);
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
    ],
    ev,
  );
}

function startRun(expId) {
  call("run", { experiment: expId, wait_s: 0 }).catch(fail);
  live = { exp: expId, step: null };
  setStatus("Calculating…");
  const poll = async () => {
    if (!live || live.exp !== expId) return;
    try {
      const r = await call("run_get", { experiment: expId });
      if (r.status === "running") {
        setStatus(`Calculating: ${r.message || r.step || "…"}`);
        live.timer = setTimeout(poll, 700);
        return;
      }
      live = null;
      setStatus(r.status === "done" ? "" : "The run failed");
      if (r.status === "done") layr.ui.notify("Calculated", "success");
      render();
    } catch (err) {
      live = null;
      setStatus("");
      fail(err);
    }
  };
  live.timer = setTimeout(poll, 400);
  render();
}

// ---------- Method tab: the steps and their explanation

async function tabMethod(body, exp, t) {
  const ex = await call("explain", { experiment: exp.id, general: S.general });
  if (t !== token) return;
  const steps = ex.steps;
  const sel = steps.find((s) => s.step === S.step) || steps[0];
  const list = h(
    "ol",
    { class: "steps" },
    steps.map((s, i) =>
      h(
        "li",
        {
          class: `step ${s === sel ? "active" : ""}`,
          onclick: () => {
            S.step = s.step;
            save();
            put(body);
            tabMethod(body, exp, token);
          },
        },
        h(
          "div",
          { class: "step-head" },
          h("span", { class: "stepnum" }, i + 1),
          h("b", {}, s.title),
          h("span", { class: "layr-spacer" }),
          s.run ? stepStatus(s.run) : null,
        ),
        h("div", { class: "step-sentence" }, s.sentence),
        s.formulas?.[0] ? h("div", { class: "step-formula" }, tex(s.formulas[0].latex)) : null,
      ),
    ),
  );
  const mtext = h(
    "div",
    { class: "layr-card methods-text" },
    h(
      "div",
      { class: "section-head" },
      h("b", {}, "Methods paragraph"),
      h("span", { class: "layr-spacer" }),
      h(
        "button",
        {
          class: "layr-button small ghost",
          onclick: () =>
            navigator.clipboard.writeText(ex.methods_text).then(() => layr.ui.notify("Copied", "success"), fail),
        },
        "Copy",
      ),
    ),
    h("p", { class: S.fullText ? "" : "clamp" }, ex.methods_text),
    h(
      "a",
      {
        href: "#",
        class: "small",
        onclick: (e) => {
          e.preventDefault();
          S.fullText = !S.fullText;
          save();
          e.target.previousSibling.classList.toggle("clamp", !S.fullText);
          e.target.textContent = S.fullText ? "Show less" : "Show all";
        },
      },
      S.fullText ? "Show less" : "Show all",
    ),
  );
  const toggle = h(
    "div",
    { class: "seg" },
    h(
      "button",
      { class: !S.general ? "active" : "", disabled: !ex.run && !S.general, onclick: () => go({ general: false }) },
      "With this run's numbers",
    ),
    h("button", { class: S.general ? "active" : "", onclick: () => go({ general: true }) }, "General"),
  );
  put(
    body,
    h(
      "div",
      { class: "method-grid" },
      h(
        "div",
        { class: "steps-col" },
        h(
          "div",
          { class: "method-head" },
          h("b", {}, ex.method.name),
          h("span", { class: "layr-muted" }, ` version ${ex.method.version}`),
          h(
            "a",
            {
              href: "#",
              class: "small",
              onclick: (e) => {
                e.preventDefault();
                go({ view: "method", method: ex.method.id, step: null });
              },
            },
            " · the method",
          ),
          h("span", { class: "layr-spacer" }),
          toggle,
        ),
        mtext,
        list,
      ),
      h("div", { class: "sheet-col" }, sel ? renderSheet(sel, exp) : null),
    ),
  );
}

function stepStatus(run) {
  if (run.status === "failed") return badge("failed", "danger");
  if (run.status !== "done") return badge(run.status, "");
  return h("span", { class: "layr-muted small" }, run.ms != null ? `${run.ms} ms` : "");
}

function renderSheet(s, exp) {
  const parts = [h("h3", {}, s.title), h("p", { class: "sentence" }, s.sentence)];
  if (s.note) parts.push(h("p", { class: "layr-muted" }, s.note));
  if (s.run?.status === "failed") parts.push(h("div", { class: "banner danger" }, s.run.error));
  if (s.formulas?.length) {
    parts.push(h("h4", {}, "Formulas"));
    for (const f of s.formulas)
      parts.push(h("div", { class: "formula" }, h("div", { class: "flabel" }, f.label), tex(f.latex, true)));
  }
  if (s.symbols?.length) {
    parts.push(
      h(
        "table",
        { class: "symbols" },
        s.symbols.map((x) =>
          h(
            "tr",
            {},
            h("td", {}, tex(x.symbol)),
            h("td", {}, x.meaning),
            h("td", { class: "layr-muted" }, x.unit || ""),
          ),
        ),
      ),
    );
  }
  if (s.run?.examples?.length) {
    parts.push(h("h4", {}, "In this run"));
    for (const x of s.run.examples) {
      parts.push(
        h("div", { class: "example" }, h("div", { class: "flabel" }, x.label), x.latex ? tex(x.latex, true) : null),
      );
    }
  }
  if (s.rules?.length) {
    parts.push(h("h4", {}, "How it decides"));
    parts.push(
      h(
        "ul",
        { class: "rules" },
        s.rules.map((r) => h("li", {}, r)),
      ),
    );
  }
  if (s.assumptions?.length) {
    parts.push(h("h4", {}, "Assumptions"));
    parts.push(
      h(
        "ul",
        { class: "rules" },
        s.assumptions.map((r) => h("li", {}, r)),
      ),
    );
  }
  const params = (s.params || []).filter(
    (p) => p.value != null && p.value !== "" && !(Array.isArray(p.value) && !p.value.length),
  );
  if (params.length) {
    parts.push(h("h4", {}, "Settings used"));
    parts.push(
      h(
        "table",
        { class: "symbols" },
        params.map((p) =>
          h(
            "tr",
            {},
            h("td", {}, p.label || p.id),
            h("td", {}, fmt(p.value), p.unit ? ` ${p.unit}` : ""),
            h("td", { class: "layr-muted small" }, p.from ? `experiment setting ${p.from.slice(1)}` : ""),
          ),
        ),
      ),
    );
  }
  const tables = Object.entries(s.run?.outputs || {}).filter(([, o]) => o.kind === "table");
  if (tables.length) {
    parts.push(h("h4", {}, "Gives"));
    parts.push(
      h(
        "div",
        { class: "gives" },
        tables.map(([port, o]) =>
          h(
            "button",
            { class: "layr-button small", onclick: () => go({ tab: "results", table: `${s.step}.${port}` }) },
            `${port} (${o.rows} rows)`,
          ),
        ),
      ),
    );
  }
  if (s.code) parts.push(h("p", { class: "code" }, "Computed with ", h("code", {}, s.code)));
  if (s.reference) parts.push(h("p", { class: "layr-muted small" }, "Reference: ", s.reference));
  return h("div", { class: "sheet" }, parts);
}

// ---------- Peaks tab: review the integration, drag the boundaries

const STATUS_CLASS = { ok: "ok", "<LOQ": "loq", "n.d.": "nd", manual: "manual", excluded: "excl" };

async function tabPeaks(body, exp, t) {
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
  const [run, m] = await Promise.all([
    call("run_get", { experiment: exp.id }),
    call("method_get", { method: exp.method }),
  ]);
  if (t !== token) return;
  const outs = m.method.outputs || {};
  const tableNames = Object.keys(outs).filter((n) => !(outs[n].from || "").endsWith(".figure"));
  const figs = Object.keys(outs).filter((n) => (outs[n].from || "").endsWith(".figure"));
  if (!S.table || !(tableNames.includes(S.table) || S.table.includes("."))) S.table = tableNames[0];
  const figBox = h("div", { class: "figs" });
  for (const f of figs) {
    layr.tools
      .call("figure", { experiment: exp.id, output: f })
      .then((res) => {
        const img = res.content?.find((c) => c.type === "image");
        if (img)
          figBox.append(
            h("img", { src: `data:${img.mimeType};base64,${img.data}`, class: "fig", alt: outs[f].label || f }),
          );
      })
      .catch(() => {});
  }
  const sel = h(
    "select",
    { class: "layr-select", onchange: (e) => go({ table: e.target.value }) },
    tableNames.map((n) => h("option", { value: n, selected: n === S.table }, outs[n].label || n)),
    S.table && !tableNames.includes(S.table) ? h("option", { value: S.table, selected: true }, S.table) : null,
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
        h("span", { class: "layr-muted small" }, " Click a row to see how it was calculated."),
      ),
      tbox,
    ),
  );
  const data = await call("result_table", { experiment: exp.id, table: S.table, limit: 2000 });
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
      table(rows, data.columns, { onRow: (r) => showProvenance(exp, S.table, r) }),
    );
  };
  draw();
}

async function showProvenance(exp, tableName, row) {
  const keys = ["injection", "compound"].every((k) => k in row)
    ? ["injection", "compound"]
    : ["strain", "substrate_fed", "compound"].every((k) => k in row)
      ? ["strain", "substrate_fed", "compound"]
      : ["strain", "substrate", "compound", "time_min"].filter((k) => k in row);
  const where = Object.fromEntries(keys.map((k) => [k, row[k]]));
  try {
    const p = keys.length ? await call("provenance", { experiment: exp.id, table: tableName, where }) : { chain: [] };
    const body = p.chain.length
      ? p.chain.map((c, i) =>
          h(
            "div",
            { class: "prov-step" },
            h("div", {}, h("span", { class: "stepnum" }, i + 1), h("b", {}, c.title)),
            c.examples.length
              ? c.examples.map((x) =>
                  h("div", { class: "example" }, h("div", { class: "flabel" }, x.label), tex(x.latex, true)),
                )
              : h("div", { class: "layr-muted small" }, "(from the table above)"),
          ),
        )
      : h(
          "p",
          { class: "layr-muted" },
          "This row combines several values (a mean, a regression or a sum): the step's own explanation in the " +
            "Method tab shows its formula with this run's numbers.",
        );
    await openDialog({
      title: `How this row was calculated`,
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
  const fl = await call("result_table", { experiment: exp.id, table: "flags", limit: 2000 }).catch(() => null);
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
          { class: "field" },
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

// ------------------------------------------------------------------------------------------------ method

async function renderMethod(t) {
  const [m, versions, ex] = await Promise.all([
    call("method_get", { method: S.method }),
    call("method_versions", { method: S.method }),
    call("explain", { method: S.method }),
  ]);
  if (t !== token) return;
  const method = m.method;
  layr.panel.setTitle(method.name).catch(() => {});
  setCrumbs([{ label: "Experiments", go: { view: "home" } }, { label: `${method.name} (method)` }]);
  setActions([h("button", { class: "layr-button ghost", onclick: () => duplicate(method) }, "Duplicate…")]);
  const sel = ex.steps.find((s) => s.step === S.step) || ex.steps[0];
  const diffBox = h("div", {});
  const vlist = h(
    "ul",
    { class: "layr-list lined versions" },
    versions
      .slice()
      .reverse()
      .map((v) =>
        h(
          "li",
          {},
          h("b", {}, `Version ${v.version}`),
          " ",
          h("span", { class: "layr-muted small" }, `${v.by || ""} ${when(v.at)}`),
          h("div", { class: "small" }, v.summary),
          h(
            "div",
            {},
            v.version > 1
              ? h(
                  "button",
                  {
                    class: "layr-button small ghost",
                    onclick: async () => {
                      const lines = await call("method_diff", {
                        method: method.id,
                        from_version: v.version - 1,
                        to_version: v.version,
                      }).catch(fail);
                      put(
                        diffBox,
                        h(
                          "div",
                          { class: "layr-card" },
                          h("b", {}, `Changes in version ${v.version}`),
                          h(
                            "ul",
                            { class: "small" },
                            (lines || []).map((l) => h("li", {}, l)),
                          ),
                        ),
                      );
                    },
                  },
                  "Changes",
                )
              : null,
            v.version !== method.version
              ? h(
                  "button",
                  {
                    class: "layr-button small ghost",
                    onclick: async () => {
                      if (
                        !(await openDialog({
                          title: `Go back to version ${v.version}?`,
                          body: "It is saved as a new version, so nothing is lost.",
                          buttons: [
                            { label: "Cancel", id: false, kind: "ghost" },
                            { label: "Go back", id: true, kind: "primary" },
                          ],
                        }))
                      )
                        return;
                      try {
                        await call("method_revert", { method: method.id, version: v.version });
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
  put(
    view,
    h(
      "div",
      { class: "method-grid" },
      h(
        "div",
        { class: "steps-col" },
        h("p", {}, method.description),
        m.problems.length
          ? h(
              "div",
              { class: "banner danger" },
              m.problems.map((p) => h("div", {}, `${p.step}: ${p.message}`)),
            )
          : null,
        h(
          "div",
          { class: "layr-card methods-text" },
          h("b", {}, "Methods paragraph (general)"),
          h("p", {}, ex.methods_text),
        ),
        h(
          "ol",
          { class: "steps" },
          ex.steps.map((s, i) =>
            h(
              "li",
              { class: `step ${s === sel ? "active" : ""}`, onclick: () => go({ step: s.step }) },
              h(
                "div",
                { class: "step-head" },
                h("span", { class: "stepnum" }, i + 1),
                h("b", {}, s.title),
                h("span", { class: "layr-spacer" }),
                h("code", { class: "small" }, s.type),
              ),
              h("div", { class: "step-sentence" }, s.sentence),
            ),
          ),
        ),
        h("h4", {}, "Versions"),
        vlist,
        diffBox,
      ),
      h("div", { class: "sheet-col" }, sel ? renderSheet(sel, null) : null),
    ),
  );
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
    if (!r.saved) return layr.ui.notify(r.problems.map((p) => p.message).join("; "), "error");
    go({ view: "method", method: id.value.trim(), step: null });
  } catch (err) {
    fail(err);
  }
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
  for (let i = 0; ; i++) {
    try {
      await call("methods");
      clearTimeout(hint);
      return;
    } catch (err) {
      if (i > 120) throw err;
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
      h("div", { class: "layr-error" }, `The analysis engine didn't start: ${err.message}`),
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
layr.events.on("research:lab", (ev) => {
  if (live && ev.what === "run") return; // the poll redraws when it's done
  if (ev.what === "run" && !ev.finished) return;
  if (
    S.view === "home" ||
    (S.view === "experiment" && (!ev.experiment || ev.experiment === S.exp)) ||
    (S.view === "method" && ev.method === S.method)
  )
    render();
});
window.addEventListener("resize", () => {
  if (S.tab === "peaks" && S.view === "experiment") {
    clearTimeout(window.__rz);
    window.__rz = setTimeout(render, 250);
  }
});
render();

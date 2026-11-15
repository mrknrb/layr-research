// Research panel: load a CSV, look at it, chart it, compare groups. All the work happens in the Python backend
// (backend/server.py); this page reads files the user picks and shows what the tools return.
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const els = {
  layout: document.querySelector(".layout"),
  dataset: $("dataset"),
  file: $("file"),
  status: $("status"),
  welcome: $("welcome"),
  view: $("datasetView"),
  dsName: $("dsName"),
  dsMeta: $("dsMeta"),
  chips: $("chips"),
  dataTable: $("dataTable"),
  moreRows: $("moreRows"),
  by: $("by"),
  summary: $("summary"),
  cx: $("cx"),
  cy: $("cy"),
  cg: $("cg"),
  chart: $("chart"),
  tc: $("tc"),
  tg: $("tg"),
  ta: $("ta"),
  tb: $("tb"),
  pairField: $("pairField"),
  compareResult: $("compareResult"),
  historyList: $("historyList"),
  historyCount: $("historyCount"),
  dropOverlay: $("dropOverlay"),
  dialog: $("resultDialog"),
};

const NUM = new Set(["integer", "number"]);
const TYPE_MARK = { integer: "123", number: "1.5", text: "Aa", date: "📅", boolean: "✓" };
const state = {
  datasets: [], // from list_datasets
  current: null, // {dataset, name, rows, columns}
  rowsShown: 0,
  tab: "data",
  chart: { kind: "box", x: null, y: null, group: null },
  compare: { column: null, group: null, a: null, b: null },
  by: "",
};
let busyCount = 0;

// ---------- small helpers ----------

function busy(text) {
  busyCount++;
  els.status.innerHTML = `<span class="layr-spinner"></span>${esc(text)}`;
  return () => {
    busyCount = Math.max(0, busyCount - 1);
    if (!busyCount) els.status.textContent = "";
  };
}

function fail(err, what) {
  let msg = err?.message || String(err);
  msg = msg.replace(/^Error executing tool \w+: /, "");
  if (err?.code === layr.errors.NOT_AVAILABLE) msg = "The analyses run on your PC: turn it on (or start layr there).";
  layr.ui.notify(`${what}: ${msg}`, "error").catch(() => {});
  return msg;
}

function fmt(v, digits = 3) {
  if (v == null) return "–";
  if (typeof v !== "number") return esc(v);
  if (Number.isInteger(v)) return v.toLocaleString();
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e6)) return v.toExponential(2);
  return v.toLocaleString(undefined, { maximumSignificantDigits: Math.max(digits, Math.ceil(Math.log10(a + 1)) + 2) });
}

const fmtP = (p) => (p == null ? "–" : p < 0.001 ? "< 0.001" : p.toFixed(3));

function timeAgo(iso) {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function cols(filter) {
  return (state.current?.columns || []).filter(filter || (() => true));
}

function fillSelect(sel, options, value, { empty } = {}) {
  const opts = (empty ? [{ value: "", label: empty }] : []).concat(options);
  sel.innerHTML = opts.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
  if (opts.some((o) => o.value === value)) sel.value = value;
  return sel.value;
}

function remember() {
  layr.panel
    .setState({
      dataset: state.current?.dataset || null,
      tab: state.tab,
      chart: state.chart,
      compare: state.compare,
      by: state.by,
      history: !els.layout.classList.contains("no-history"),
    })
    .catch(() => {});
}

/** The image and text of a raw MCP result. */
function partsOf(result) {
  const image = (result.content || []).find((c) => c.type === "image");
  const text = (result.content || []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
  return { image: image ? `data:${image.mimeType || "image/png"};base64,${image.data}` : null, text };
}

const theme = () => (document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");

// ---------- loading data ----------

/** Reads a file the user picked as text: UTF-8, else Windows-1250 (Hungarian Excel exports). */
async function readText(file) {
  const buf = await file.arrayBuffer();
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    return new TextDecoder("windows-1250").decode(buf);
  }
}

async function loadFile(file) {
  if (!file) return;
  if (file.size > 50 * 1024 * 1024) {
    layr.ui.notify("That file is over 50 MB: too big to send from the page.", "error");
    return;
  }
  const done = busy(`Loading ${file.name}…`);
  try {
    const content = await readText(file);
    const res = await layr.tools.run("load_csv", { content, name: file.name.replace(/\.(csv|tsv|txt)$/i, "") });
    await refreshDatasets();
    await openDataset(res.dataset, res);
    layr.ui.notify(`Loaded ${res.name}: ${res.rows} rows`, "success").catch(() => {});
  } catch (err) {
    fail(err, "Couldn't load the file");
  } finally {
    done();
  }
}

async function loadSample() {
  const done = busy("Loading the sample…");
  try {
    const res = await layr.tools.run("load_sample", {});
    await refreshDatasets();
    await openDataset(res.dataset, res);
  } catch (err) {
    fail(err, "Couldn't load the sample");
  } finally {
    done();
  }
}

async function refreshDatasets() {
  state.datasets = await layr.tools.run("list_datasets", {});
  els.dataset.hidden = !state.datasets.length;
  fillSelect(
    els.dataset,
    state.datasets.map((d) => ({ value: d.dataset, label: `${d.name} · ${d.rows} rows` })),
    state.current?.dataset
  );
}

// ---------- the dataset view ----------

async function openDataset(id, loaded) {
  const meta = state.datasets.find((d) => d.dataset === id);
  if (!meta) {
    showWelcome();
    return;
  }
  const switching = state.current?.dataset !== id;
  state.current = { ...meta };
  els.dataset.value = id;
  els.welcome.hidden = true;
  els.view.hidden = false;
  els.dsName.textContent = meta.name;
  const numeric = meta.columns.filter((c) => NUM.has(c.type)).length;
  els.dsMeta.textContent = `${meta.rows.toLocaleString()} rows · ${meta.columns.length} columns (${numeric} with numbers)`;
  els.chips.innerHTML = meta.columns
    .map((c) => `<span class="chip ${NUM.has(c.type) ? "num" : ""}" title="${esc(c.type)}">
      <span class="t">${TYPE_MARK[c.type] || "?"}</span>${esc(c.name)}</span>`)
    .join("");
  layr.panel.setTitle(`Research · ${meta.name}`).catch(() => {});

  if (switching) {
    els.chart.innerHTML = '<div class="layr-empty">Pick what to show and press Draw.</div>';
    els.compareResult.innerHTML =
      '<div class="layr-empty">Choose a number column and the column that says each row\'s group.</div>';
    els.summary.innerHTML = "";
  }
  setupChartControls();
  setupCompareControls();
  fillSelect(els.by, cols((c) => !NUM.has(c.type)).map((c) => ({ value: c.name, label: c.name })), state.by, {
    empty: "Nothing (whole table)",
  });
  state.by = els.by.value;

  state.rowsShown = 0;
  els.dataTable.innerHTML = "";
  if (loaded?.preview) renderRows(loaded.preview, 0, true);
  await moreRows(true);
  showTab(state.tab);
  remember();
}

function showWelcome() {
  state.current = null;
  els.welcome.hidden = false;
  els.view.hidden = true;
  layr.panel.setTitle("Research").catch(() => {});
}

function renderRows(rows, offset, replace) {
  const columns = state.current.columns;
  const head = `<thead><tr><th class="rowno">#</th>${columns
    .map((c) => `<th class="${NUM.has(c.type) ? "num" : ""}">${esc(c.name)}</th>`)
    .join("")}</tr></thead>`;
  const body = rows
    .map(
      (r, i) =>
        `<tr><td class="rowno">${offset + i + 1}</td>${columns
          .map((c) => {
            const v = r[c.name];
            if (v == null) return '<td class="missing">–</td>';
            const shown = c.type === "date" ? String(v).replace("T00:00:00", "") : NUM.has(c.type) ? fmt(v, 6) : esc(v);
            return `<td class="${NUM.has(c.type) ? "num" : ""}">${shown}</td>`;
          })
          .join("")}</tr>`
    )
    .join("");
  if (replace || !els.dataTable.tHead) els.dataTable.innerHTML = head + `<tbody>${body}</tbody>`;
  else els.dataTable.tBodies[0].insertAdjacentHTML("beforeend", body);
  state.rowsShown = offset + rows.length;
  els.moreRows.hidden = state.rowsShown >= state.current.rows;
  els.moreRows.textContent = `Show more rows (${state.rowsShown} of ${state.current.rows.toLocaleString()})`;
}

async function moreRows(first) {
  const offset = first ? 0 : state.rowsShown;
  try {
    const res = await layr.tools.run("preview", { dataset: state.current.dataset, offset, limit: first ? 100 : 200 });
    renderRows(res.data, offset, first);
  } catch (err) {
    fail(err, "Couldn't read the rows");
  }
}

function showTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tabs button")) {
    b.classList.toggle("active", b.dataset.tab === tab);
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  }
  for (const p of document.querySelectorAll(".tab")) p.classList.toggle("active", p.dataset.pane === tab);
  if (tab === "summary" && !els.summary.innerHTML) loadSummary();
  remember();
}

// ---------- summary ----------

async function loadSummary() {
  if (!state.current) return;
  const done = busy("Summarizing…");
  els.summary.innerHTML = '<div class="layr-empty"><span class="layr-spinner"></span></div>';
  try {
    const args = { dataset: state.current.dataset, remember: false };
    if (state.by) args.by = state.by;
    renderSummary(await layr.tools.run("describe", args));
  } catch (err) {
    els.summary.innerHTML = `<div class="layr-empty layr-error">${esc(fail(err, "Couldn't summarize"))}</div>`;
  } finally {
    done();
  }
}

function renderSummary(d) {
  const entries = d.columns.map((c) => [c.name, c]);
  const nums = entries.filter(([, s]) => NUM.has(s.type));
  const others = entries.filter(([, s]) => !NUM.has(s.type));
  let html = "";
  if (nums.length) {
    const keys = ["count", "missing", "mean", "sd", "min", "q1", "median", "q3", "max"];
    const names = { sd: "SD", q1: "Q1", q3: "Q3" };
    html += `<div class="section-title">Numbers</div><div class="stat-table"><table class="layr-table"><thead><tr><th>Column</th>
      ${keys.map((k) => `<th class="num">${names[k] || k[0].toUpperCase() + k.slice(1)}</th>`).join("")}</tr></thead><tbody>
      ${nums.map(([n, s]) => `<tr><td>${esc(n)}</td>${keys.map((k) => `<td class="num">${fmt(s[k])}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>`;
  }
  if (d.groups) {
    const numCols = nums.map(([n]) => n).filter((n) => n !== d.by);
    for (const col of numCols) {
      html += `<div class="section-title">${esc(col)} by ${esc(d.by)}</div><div class="stat-table"><table class="layr-table">
        <thead><tr><th>${esc(d.by)}</th><th class="num">N</th><th class="num">Mean</th><th class="num">SD</th>
        <th class="num">SEM</th><th class="num">Median</th><th class="num">Min</th><th class="num">Max</th></tr></thead><tbody>
        ${d.groups
          .map(({ group: g, stats }) => {
            const c = stats[col] || {};
            return `<tr><td>${esc(g)}</td><td class="num">${fmt(c.count)}</td><td class="num">${fmt(c.mean)}</td>
              <td class="num">${fmt(c.sd)}</td><td class="num">${fmt(c.sem)}</td><td class="num">${fmt(c.median)}</td>
              <td class="num">${fmt(c.min)}</td><td class="num">${fmt(c.max)}</td></tr>`;
          })
          .join("")}</tbody></table></div>`;
    }
  }
  if (others.length) {
    html += `<div class="section-title">Other columns</div><div class="stat-table"><table class="layr-table"><thead><tr>
      <th>Column</th><th>Type</th><th class="num">Count</th><th class="num">Missing</th><th class="num">Different</th>
      <th>Most common</th></tr></thead><tbody>
      ${others
        .map(
          ([n, s]) => `<tr><td>${esc(n)}</td><td class="layr-muted">${esc(s.type)}</td><td class="num">${fmt(s.count)}</td>
          <td class="num">${fmt(s.missing)}</td><td class="num">${s.unique != null ? fmt(s.unique) : "–"}</td>
          <td><div class="top-values">${
            s.top
              ? s.top.slice(0, 5).map((t) => `<span class="layr-badge">${esc(t.value)} · ${t.count}</span>`).join("")
              : s.type === "date"
                ? `${esc(String(s.min).slice(0, 10))} → ${esc(String(s.max).slice(0, 10))}`
                : ""
          }</div></td></tr>`
        )
        .join("")}</tbody></table></div>`;
  }
  els.summary.innerHTML = html || '<div class="layr-empty">Nothing to summarize.</div>';
}

// ---------- charts ----------

const KIND_FIELDS = {
  box: { x: "Groups (x)", xFilter: "cat", xEmpty: "None (one box)", y: "Values (y)", group: false },
  bar: { x: "Groups (x)", xFilter: "cat", xEmpty: "None", y: "Values (y, mean ± SEM)", group: false },
  scatter: { x: "X", xFilter: "num", y: "Y", group: true },
  line: { x: "X", xFilter: "any", y: "Y (mean per x)", group: true },
  hist: { x: "Values", xFilter: "num", y: null, group: true },
};

function setupChartControls() {
  const c = state.chart;
  for (const b of document.querySelectorAll("#kinds .layr-button")) b.classList.toggle("active", b.dataset.kind === c.kind);
  const f = KIND_FIELDS[c.kind];
  const num = cols((x) => NUM.has(x.type)).map((x) => ({ value: x.name, label: x.name }));
  const cat = cols((x) => !NUM.has(x.type)).map((x) => ({ value: x.name, label: x.name }));
  const all = cols().map((x) => ({ value: x.name, label: x.name }));
  $("cxLabel").textContent = f.x;
  const xOptions = f.xFilter === "num" ? num : f.xFilter === "cat" ? cat : all;
  const firstCat = cat[0]?.value;
  c.x = fillSelect(els.cx, xOptions, c.x ?? (f.xFilter === "cat" ? firstCat : xOptions[0]?.value), {
    empty: f.xEmpty,
  });
  $("cyField").hidden = !f.y;
  if (f.y) {
    $("cyLabel").textContent = f.y;
    const preferY = num.find((o) => o.value !== c.x)?.value;
    c.y = fillSelect(els.cy, num, c.y && c.y !== c.x ? c.y : preferY);
  }
  $("cgField").hidden = !f.group;
  if (f.group) c.group = fillSelect(els.cg, cat, c.group, { empty: "Nothing" }) || null;
}

async function drawChart() {
  const c = state.chart;
  const f = KIND_FIELDS[c.kind];
  const args = { dataset: state.current.dataset, kind: c.kind, theme: theme() };
  if (c.kind === "box" || c.kind === "bar") {
    args.y = c.y;
    if (c.x) args.x = c.x;
  } else {
    args.x = c.x;
    if (f.y) args.y = c.y;
    if (f.group && c.group) args.group = c.group;
  }
  const done = busy("Drawing…");
  els.chart.innerHTML = '<div class="layr-empty"><span class="layr-spinner"></span>Drawing…</div>';
  try {
    const res = await layr.tools.call("plot", args);
    const { image, text } = partsOf(res);
    if (res.isError || !image) throw new Error(text || "No picture came back");
    els.chart.innerHTML = `<img src="${image}" alt="${esc(text)}" title="${esc(text)}">`;
  } catch (err) {
    els.chart.innerHTML = `<div class="layr-empty layr-error">${esc(fail(err, "Couldn't draw it"))}</div>`;
  } finally {
    done();
  }
  remember();
}

// ---------- comparing groups ----------

let levels = []; // values of the chosen group column

async function setupCompareControls() {
  const c = state.compare;
  const num = cols((x) => NUM.has(x.type)).map((x) => ({ value: x.name, label: x.name }));
  const cat = cols((x) => !NUM.has(x.type)).map((x) => ({ value: x.name, label: x.name }));
  c.column = fillSelect(els.tc, num, c.column ?? num[0]?.value);
  c.group = fillSelect(els.tg, cat, c.group ?? cat[0]?.value);
  await loadLevels();
}

async function loadLevels() {
  const c = state.compare;
  levels = [];
  els.pairField.hidden = true;
  if (!c.group || !state.current) return;
  try {
    const d = await layr.tools.run("describe", { dataset: state.current.dataset, remember: false });
    const col = d.columns.find((x) => x.name === c.group);
    levels = (col?.top || []).map((t) => t.value);
    if ((col?.unique || 0) > levels.length) levels.push("…");
  } catch {
    return;
  }
  if (levels.length > 2) {
    const opts = levels.filter((l) => l !== "…").map((l) => ({ value: l, label: l }));
    els.pairField.hidden = false;
    c.a = fillSelect(els.ta, opts, c.a, { empty: "All groups (ANOVA)" }) || null;
    c.b = fillSelect(els.tb, opts.filter((o) => o.value !== c.a), c.b, { empty: "…" }) || null;
    els.tb.disabled = !c.a;
  }
}

async function compare() {
  const c = state.compare;
  if (!c.column || !c.group) {
    layr.ui.notify("Pick a number column and a group column first.", "error");
    return;
  }
  const base = { dataset: state.current.dataset, column: c.column, group: c.group };
  const pair = levels.length <= 2 ? null : c.a && c.b ? [c.a, c.b] : null;
  const useTtest = levels.length === 2 || pair;
  const done = busy(useTtest ? "Running a t-test…" : "Running an ANOVA…");
  els.compareResult.innerHTML = '<div class="layr-empty"><span class="layr-spinner"></span></div>';
  try {
    const r = useTtest
      ? await layr.tools.run("ttest", pair ? { ...base, groups: pair } : base)
      : await layr.tools.run("anova", base);
    els.compareResult.innerHTML = renderTest(r);
  } catch (err) {
    els.compareResult.innerHTML = `<div class="layr-empty layr-error">${esc(fail(err, "Couldn't compare"))}</div>`;
  } finally {
    done();
  }
  remember();
}

function renderTest(r) {
  const badge = r.significant
    ? '<span class="layr-badge success">Significant</span>'
    : '<span class="layr-badge">Not significant</span>';
  const groups = r.groups
    .map((s) => `<tr><td>${esc(s.group)}</td><td class="num">${fmt(s.n)}</td><td class="num">${fmt(s.mean)}</td>
      <td class="num">${fmt(s.sd)}</td></tr>`)
    .join("");
  let html = `<div class="verdict">${badge}<div>${esc(r.interpretation)}</div></div>
    <div class="stat-table"><table class="layr-table"><thead><tr><th>${esc(r.group)}</th><th class="num">N</th>
    <th class="num">Mean</th><th class="num">SD</th></tr></thead><tbody>${groups}</tbody></table></div>`;
  if (r.t != null) {
    html += `<div class="stat-table"><table class="layr-table"><tbody>
      <tr><td>Test</td><td>${esc(r.test)}</td></tr>
      <tr><td>Mean difference</td><td>${fmt(r.mean_difference)} (95% CI ${fmt(r.ci95?.[0])} to ${fmt(r.ci95?.[1])})</td></tr>
      <tr><td>t</td><td>${fmt(r.t)} with ${fmt(r.df)} degrees of freedom</td></tr>
      <tr><td>p</td><td>${fmtP(r.p)}</td></tr>
      <tr><td>Effect size (Cohen's d)</td><td>${fmt(r.cohens_d)}</td></tr></tbody></table></div>`;
  } else {
    html += `<div class="stat-table"><table class="layr-table"><tbody>
      <tr><td>Test</td><td>${esc(r.test)}</td></tr>
      <tr><td>F</td><td>${fmt(r.F)}</td></tr><tr><td>p</td><td>${fmtP(r.p)}</td></tr>
      <tr><td>Effect size (η²)</td><td>${fmt(r.eta_squared)}</td></tr></tbody></table></div>
      <div class="section-title">Pairs (Tukey HSD)</div>
      <div class="stat-table"><table class="layr-table"><thead><tr><th>Pair</th><th class="num">Difference</th>
      <th class="num">95% CI</th><th class="num">p</th><th></th></tr></thead><tbody>
      ${r.pairs
        .map((q) => `<tr><td>${esc(q.a)} – ${esc(q.b)}</td><td class="num">${fmt(q.mean_difference)}</td>
          <td class="num">${fmt(q.ci95[0])} to ${fmt(q.ci95[1])}</td><td class="num">${fmtP(q.p)}</td>
          <td>${q.significant ? '<span class="layr-badge success">differs</span>' : ""}</td></tr>`)
        .join("")}</tbody></table></div>`;
  }
  return html;
}

// ---------- history ----------

const ICONS = {
  plot: '<path d="M4 19h16v2H2V3h2zm3-2V9h3v8zm5 0V5h3v12zm5 0v-5h3v5z" fill="currentColor"/>',
  load_csv: '<path d="M6 2h8l6 6v14H6zm7 1.5V9h5.5z" fill="currentColor"/>',
  describe: '<path d="M3 5h18v2H3zm0 6h12v2H3zm0 6h15v2H3z" fill="currentColor"/>',
  ttest: '<path d="M7 3h10v2h-4v16h-2V5H7z" fill="currentColor"/>',
  anova: '<path d="M4 20 10 4h2l6 16h-2.2l-1.5-4H8.7l-1.5 4zm5.4-6h5.2L12 6.9z" fill="currentColor"/>',
};
let historyItems = [];
const thumbs = new Map(); // plot id -> data URL

async function refreshHistory() {
  try {
    historyItems = await layr.tools.run("history", { limit: 40 });
  } catch {
    return;
  }
  els.historyCount.textContent = historyItems.length;
  if (!historyItems.length) {
    els.historyList.innerHTML = '<li class="layr-empty">Results show here: charts, summaries and tests, also the ones an AI made.</li>';
    return;
  }
  els.historyList.innerHTML = historyItems
    .map((h) => {
      const ds = state.datasets.find((d) => d.dataset === h.dataset);
      return `<li data-id="${esc(h.id)}">
        <span class="h-icon ${h.tool === "plot" ? "plot" : ""}"><svg viewBox="0 0 24 24" width="15" height="15">${ICONS[h.tool] || ICONS.describe}</svg></span>
        <div class="h-body"><div class="h-title">${esc(h.title)}</div>
          <div class="h-meta">${esc(timeAgo(h.at))}${ds ? " · " + esc(ds.name) : ""}</div>
          ${h.plot ? `<img class="h-thumb" data-plot="${esc(h.plot)}" alt="">` : ""}</div></li>`;
    })
    .join("");
  // Thumbnails for the newest few charts.
  const imgs = [...els.historyList.querySelectorAll("img[data-plot]")].slice(0, 6);
  for (const img of imgs) loadThumb(img);
}

async function loadThumb(img) {
  const id = img.dataset.plot;
  if (!thumbs.has(id)) {
    try {
      const { image } = partsOf(await layr.tools.call("get_plot", { plot: id }));
      if (image) thumbs.set(id, image);
    } catch {
      return;
    }
  }
  if (thumbs.has(id)) img.src = thumbs.get(id);
}

async function showHistoryItem(id) {
  const h = historyItems.find((x) => x.id === id);
  if (!h) return;
  $("dialogTitle").textContent = h.title;
  const body = $("dialogBody");
  if (h.plot) {
    body.innerHTML = '<div class="layr-empty"><span class="layr-spinner"></span></div>';
    els.dialog.showModal();
    await loadThumb({ dataset: { plot: h.plot }, set src(v) {} });
    body.innerHTML = thumbs.has(h.plot)
      ? `<img src="${thumbs.get(h.plot)}" alt="${esc(h.title)}"><p class="layr-muted">${esc(h.result?.path || "")}</p>`
      : '<div class="layr-empty">The chart file is gone.</div>';
  } else if (h.tool === "ttest" || h.tool === "anova") {
    body.innerHTML = renderTest(h.result);
    els.dialog.showModal();
  } else {
    body.innerHTML = `<pre><code>${esc(JSON.stringify(h.result, null, 2))}</code></pre>`;
    els.dialog.showModal();
  }
  if (h.dataset && h.dataset !== state.current?.dataset && state.datasets.some((d) => d.dataset === h.dataset)) {
    openDataset(h.dataset);
  }
}

// ---------- wiring ----------

function wire() {
  const pick = () => els.file.click();
  $("open").onclick = pick;
  $("open2").onclick = pick;
  els.file.onchange = () => {
    loadFile(els.file.files[0]);
    els.file.value = "";
  };
  $("sample").onclick = loadSample;
  $("sample2").onclick = loadSample;
  els.dataset.onchange = () => openDataset(els.dataset.value);
  $("toggleHistory").onclick = () => {
    const narrow = innerWidth <= 760;
    if (narrow) els.layout.classList.toggle("show-history-narrow");
    else els.layout.classList.toggle("no-history");
    remember();
  };
  $("remove").onclick = async () => {
    if (!state.current) return;
    const btn = $("remove");
    if (btn.textContent !== "Remove?") {
      btn.textContent = "Remove?";
      setTimeout(() => (btn.textContent = "Remove"), 3000);
      return;
    }
    btn.textContent = "Remove";
    try {
      await layr.tools.run("remove_dataset", { dataset: state.current.dataset });
      await refreshDatasets();
      if (state.datasets[0]) await openDataset(state.datasets[0].dataset);
      else showWelcome();
      remember();
    } catch (err) {
      fail(err, "Couldn't remove it");
    }
  };

  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => showTab(b.dataset.tab);
  els.moreRows.onclick = () => moreRows(false);
  els.by.onchange = () => {
    state.by = els.by.value;
    loadSummary();
    remember();
  };

  for (const b of document.querySelectorAll("#kinds .layr-button")) {
    b.onclick = () => {
      state.chart.kind = b.dataset.kind;
      state.chart.x = null;
      setupChartControls();
      remember();
    };
  }
  els.cx.onchange = () => (state.chart.x = els.cx.value || null);
  els.cy.onchange = () => (state.chart.y = els.cy.value || null);
  els.cg.onchange = () => (state.chart.group = els.cg.value || null);
  $("draw").onclick = drawChart;

  els.tc.onchange = () => (state.compare.column = els.tc.value);
  els.tg.onchange = () => {
    state.compare.group = els.tg.value;
    state.compare.a = state.compare.b = null;
    loadLevels();
  };
  els.ta.onchange = () => {
    state.compare.a = els.ta.value || null;
    state.compare.b = null;
    loadLevels();
  };
  els.tb.onchange = () => (state.compare.b = els.tb.value || null);
  $("compare").onclick = compare;

  els.historyList.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) showHistoryItem(li.dataset.id);
  });
  $("dialogClose").onclick = () => els.dialog.close();
  els.dialog.addEventListener("click", (e) => e.target === els.dialog && els.dialog.close());

  // Drag and drop anywhere on the panel.
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    depth++;
    els.dropOverlay.hidden = false;
  });
  addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) els.dropOverlay.hidden = true;
  });
  addEventListener("dragover", (e) => hasFiles(e) && e.preventDefault());
  addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    els.dropOverlay.hidden = true;
    loadFile(e.dataTransfer.files[0]);
  });

  // Ctrl+O opens a file.
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && !e.altKey && !e.shiftKey && e.code === "KeyO") {
      e.preventDefault();
      pick();
    }
  });

  // Results from anywhere (this panel, another tab, an AI agent) arrive as research:result.
  let t;
  layr.events.on("research:result", (data) => {
    clearTimeout(t);
    t = setTimeout(async () => {
      if (data?.tool === "load_csv") {
        await refreshDatasets();
        if (!state.current && state.datasets[0]) await openDataset(state.datasets[0].dataset);
      }
      refreshHistory();
    }, 150);
  });
}

async function main() {
  try {
    await layr.ready;
  } catch (err) {
    $("startingText").textContent = err.message;
    return;
  }
  wire();
  const saved = layr.info.state || {};
  Object.assign(state.chart, saved.chart || {});
  Object.assign(state.compare, saved.compare || {});
  state.by = saved.by || "";
  state.tab = saved.tab || "data";
  if (saved.history === false) els.layout.classList.add("no-history");

  const hint = setTimeout(() => ($("startingHint").hidden = false), 4000);
  try {
    await refreshDatasets();
  } catch (err) {
    clearTimeout(hint);
    const msg = fail(err, "The analysis engine didn't start");
    $("startingText").innerHTML = `<span class="layr-error">${esc(msg)}</span>`;
    $("startingHint").hidden = true;
    document.querySelector("#starting .layr-spinner").remove();
    return;
  }
  clearTimeout(hint);
  $("starting").hidden = true;
  const want = layr.info.params?.dataset || saved.dataset;
  const first = state.datasets.find((d) => d.dataset === want) || state.datasets[0];
  if (first) await openDataset(first.dataset);
  else showWelcome();
  refreshHistory();
}

main();

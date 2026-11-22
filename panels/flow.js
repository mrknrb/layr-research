// The Research plugin's flow: a method's recipe drawn as boxes and wires, with the data of a chosen run flowing
// through it. Everything it shows comes from the plugin's tools (flow_get, value_get, function_inside, blocks, ...),
// and every change made in it is a `flow_edit` of the code handed to the host: the flow never saves anything.
// No framework and no build step. Load flow.css and editor.css with it.
//
//   import { mountFlow } from "./flow.js";
//   const flow = mountFlow(container, { call, method, experiment, editable, inside: "panel", tex, onSource, ... });
//
// The container needs a height (the flow fills it). See docs/proposals/research-flow/build-contract.md › Phase 2.
import { createEditor, highlight } from "./editor.js";

const SVGNS = "http://www.w3.org/2000/svg";
const BOX_TYPES = new Set(["table", "flags", "data", "figure"]);
// The units a name can end in (rate_mM_per_min → rate [mM/min]): the same list as the backend's (lab/latex.py UNITS),
// so a column is labelled as the formulas label it.
const UNITS = {
  mM: "mM", uM: "µM", nM: "nM", M: "M", mol: "mol", mmol: "mmol", umol: "µmol", nmol: "nmol",
  min: "min", s: "s", h: "h", ms: "ms", d: "d",
  L: "L", l: "L", mL: "mL", ml: "mL", uL: "µL", ul: "µL",
  g: "g", mg: "mg", ug: "µg", kg: "kg", gCDW: "gCDW", gcdw: "gCDW", gDW: "gDW",
  U: "U", pA: "pA", mAU: "mAU", pct: "%", percent: "%", nm: "nm", um: "µm", mm: "mm",
  degC: "°C", K: "K", rpm: "rpm", OD: "OD", Da: "Da", kDa: "kDa", bar: "bar",
};

// -------------------------------------------------------------------------------- small helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value" || k === "checked" || k === "selected" || k === "disabled") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  add(el, kids);
  return el;
}
const flat = (kids) => [kids].flat(Infinity).filter((k) => k != null && k !== false && k !== "");
function add(el, ...kids) {
  for (const k of flat(kids)) el.append(k.nodeType ? k : String(k));
  return el;
}
const put = (el, ...kids) => (el.replaceChildren(), add(el, kids));
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}
function human(name) {
  const s = String(name || "")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .trim()
    .toLowerCase();
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}
const ord = (n) => {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
};
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
const cleanError = (e) =>
  String(e?.message || e || "Something went wrong")
    .replace(/^Error executing tool \w+:\s*/, "")
    .replace(/^LabError:\s*/, "");
/** What went wrong in a step, without Python's name for the kind of error ("ValueError: …"). */
const stepError = (e) => String(e?.message || e || "").replace(/^[A-Z]\w*(Error|Exception|Warning):\s*/, "");
function fmtNum(x) {
  if (typeof x !== "number" || !isFinite(x)) return String(x);
  if (Number.isInteger(x)) return String(x);
  const a = Math.abs(x);
  if (a >= 1e5 || a < 1e-3) return x.toPrecision(3);
  return String(+x.toPrecision(4));
}
/** A name's words and its unit, read from the end of the name: rate_mM_per_min → ["rate", "mM/min"]; growth_per_h →
 * ["growth", "1/h"]; [name, null] when it ends in no unit. */
function splitUnit(name) {
  const tokens = String(name).split("_");
  for (let i = 1; i < tokens.length; i++) {
    let tail = tokens.slice(i);
    const inverse = tail[0] === "per" && tail.length > 1;
    if (inverse) tail = tail.slice(1);
    if (tail[tail.length - 1] === "per" || !(tail[0] in UNITS)) continue;
    if (!tail.every((t) => t in UNITS || t === "per")) continue;
    const unit = tail.map((t) => (t === "per" ? "/" : UNITS[t])).join("");
    return [tokens.slice(0, i).join("_"), inverse ? `1/${unit}` : unit];
  }
  return [name, null];
}
function colName(c) {
  const [words, unit] = splitUnit(c);
  if (!unit || !words) return [c];
  return [words, h("span", { class: "fl-unit" }, unit)];
}
/**
 * A Python literal written in the code (a text in quotes, a number, True, False, None, a list or tuple of them) as a
 * JavaScript value; undefined for anything else (a name, a call, an f-string), which stays shown as code.
 */
function pyValue(code) {
  const s = String(code ?? "").trim();
  let i = 0;
  const ws = () => {
    while (i < s.length && /\s/.test(s[i])) i++;
  };
  const val = () => {
    ws();
    const c = s[i];
    if (c === '"' || c === "'") {
      let out = "";
      for (i++; i < s.length && s[i] !== c; i++) {
        if (s[i] === "\\") {
          const e = s[++i];
          out += e === "n" ? "\n" : e === "t" ? "\t" : e;
        } else out += s[i];
      }
      if (s[i++] !== c) throw new Error("open text");
      return out;
    }
    if (c === "[" || c === "(") {
      const close = c === "[" ? "]" : ")";
      const arr = [];
      for (i++, ws(); s[i] !== close; ws()) {
        if (i >= s.length) throw new Error("open list");
        arr.push(val());
        ws();
        if (s[i] === ",") i++;
        else if (s[i] !== close) throw new Error("no comma");
      }
      i++;
      return arr;
    }
    const m = /^(?:True|False|None|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(?![\w.])/.exec(s.slice(i));
    if (!m) throw new Error("not a literal");
    i += m[0].length;
    return m[0] === "True" ? true : m[0] === "False" ? false : m[0] === "None" ? null : Number(m[0]);
  };
  try {
    const v = val();
    ws();
    return i === s.length ? v : undefined;
  } catch {
    return undefined;
  }
}
/** A value written in the code, as she reads it: texts without quotes, lists as "a, b", yes / no. */
function plainValue(v, field) {
  if (v === null) return "none";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return fmtNum(v);
  if (Array.isArray(v)) return v.length ? v.map((x) => plainValue(x, field)).join(", ") : "none";
  const choice = field?.choices?.find((c) => c.value === v);
  return choice ? choice.label : String(v);
}
/** "a, b, 3" typed in a field: the list ["a", "b", 3]. */
const listOf = (t) =>
  String(t).split(",").map((x) => x.trim()).filter(Boolean).map((x) => (isFinite(Number(x)) ? Number(x) : x));
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// -------------------------------------------------------------------------------- values (recorder summaries)

/** table, flags, figure, data, number, text or none: the type of a recorded value summary. */
function kindOf(v) {
  if (v === undefined || v === null) return "none";
  if (typeof v === "boolean") return "data";
  if (typeof v === "number") return "number";
  if (typeof v === "string") return "text";
  if (typeof v !== "object") return "none";
  if (v.kind === "table") {
    const c = v.columns || [];
    return c.includes("level") && c.includes("message") ? "flags" : "table";
  }
  if (v.kind === "figure") return "figure";
  if (v.kind === "number") return "number";
  return "data";
}
const dotOf = (t) => (BOX_TYPES.has(t) ? t : "none");
function shape(v) {
  if (v === undefined) return "";
  if (v === null) return "None";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return fmtNum(v);
  if (typeof v === "string") return JSON.stringify(v.length > 26 ? v.slice(0, 25) + "…" : v);
  if (typeof v !== "object") return String(v);
  switch (v.kind) {
    case "table":
      return `${v.rows} × ${(v.columns || []).length}`;
    case "list": {
      const items = v.items || [];
      if (v.length <= 4 && items.length === v.length && items.every((x) => x === null || typeof x !== "object"))
        return "[" + items.map(shape).join(", ") + "]";
      return plural(v.length, "item");
    }
    case "dict":
      return plural(v.length, "entry", "entries");
    case "record":
      return v.type || "record";
    case "column":
      return plural(v.length, "value");
    case "array":
      return (v.shape || []).join(" × ") + " values";
    case "figure":
      return "figure";
    case "number":
      return v.text ?? fmtNum(v.value);
    case "function":
      return v.name || "function";
    case "bytes":
      return `${v.size} bytes`;
    default:
      return v.type || v.text || "value";
  }
}
function longShape(v) {
  if (v && v.kind === "table")
    return `table · ${plural(v.rows, "row")} × ${plural((v.columns || []).length, "column")}`;
  if (v && v.kind === "list") return `list · ${plural(v.length, "item")}`;
  return shape(v);
}
const cell = (x) => (x == null ? "" : typeof x === "number" ? fmtNum(x) : typeof x === "object" ? shape(x) : String(x));
function tableEl(columns, rows, maxCols = 99) {
  const cols = columns.slice(0, maxCols);
  return h(
    "table",
    { class: "fl-tv" },
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, colName(c))))),
    h(
      "tbody",
      {},
      rows.map((r) =>
        h(
          "tr",
          {},
          cols.map((c) => {
            const x = Array.isArray(r) ? r[columns.indexOf(c)] : r[c];
            return h("td", { class: typeof x === "string" ? "s" : "" }, cell(x));
          }),
        ),
      ),
    ),
  );
}

// -------------------------------------------------------------------------------- the component

/**
 * The options are the contract's (build-contract.md › The flow component), and a few more a host may give:
 * `picker: "runs"` (the host picks the experiment: the bar offers only its runs), `boxActions(node)` →
 * [{label, run, title?}] (links at a box's foot), `runNote: false` (the host says itself that the
 * shown run failed), `figure({run, output})` → an image's src (a returned figure in cards and the inside panel).
 * Beyond the contract's methods: `setProblem({line, message} | null)` (the host's code can't be read now),
 * `setInside("panel" | "host")` and `fit()`.
 * Keys: Escape closes the newest thing open, Delete removes the selected box (editable), Enter or Space on a box's
 * head looks inside.
 */
export function mountFlow(container, options = {}) {
  const O = { inside: "panel", claude: true, ...options };
  const call = O.call;
  const S = {
    method: O.method || null,
    version: O.version ?? null,
    source: O.source ?? null,
    experiment: O.experiment ?? null,
    run: O.run ?? null,
    zoom: 0.9,
    tx: 16,
    ty: 16,
    viewSet: false,
    sel: null,
    multi: new Set(),
    trace: false,
    quality: false, // the quality checks unfolded
    folded: new Set(), // group ids folded
    openDefaults: new Set(), // boxes whose inputs left as they are are shown
    python: false,
    running: null,
    pending: {}, // setting id -> value typed, until the host draws again
    side: null,
    stack: [],
    round: {},
    loadTicket: 0,
    insideTicket: 0,
  };
  let G = null; // the graph (flow_get)
  let VIEW = null; // the last layout
  let BLOCKS = {}; // ready-made steps by function
  let BLOCK_LIST = [];
  let READERS = []; // the readers of raw files: [{function, module, label, extensions, out, param}]
  let FUNCS = []; // the method's own functions, read from the source
  let RUNS = null; // the chosen experiment's runs
  let EXPS = null; // experiments of this method
  let editor = null; // the inside panel's code
  let destroyed = false;
  const rowsCache = new Map();

  // ---------------------------------------------------------------- the frame
  const root = h("div", { class: "fl" });
  const bar = h("div", { class: "fl-bar" });
  const note = h("div", { class: "fl-note" });
  const world = h("div", { class: "fl-world" });
  const wires = svgEl("svg", { class: "fl-wires" });
  world.append(wires);
  const mini = svgEl("svg", { class: "fl-mini", width: 180, height: 64 });
  const hint = h(
    "div",
    { class: "fl-hint" },
    h("span", { class: "fl-legend" }, legendDot("table", "table"), legendDot("flags", "quality flags"),
      legendDot("data", "other data"), legendDot("figure", "figure")),
  );
  const zoomBox = h(
    "div",
    { class: "fl-zoom" },
    h("button", { class: "layr-button small icon", title: "Zoom out", "aria-label": "Zoom out",
      onclick: () => zoomBy(0.85) }, "−"),
    h("button", { class: "layr-button small", title: "Show all of it", onclick: () => fit() }, "Fit"),
    h("button", { class: "layr-button small icon", title: "Zoom in", "aria-label": "Zoom in",
      onclick: () => zoomBy(1.18) }, "+"),
  );
  const empty = h("div", { class: "fl-empty" });
  const canvas = h("div", { class: "fl-canvas" }, world, empty, hint, zoomBox, mini);
  const sideHead = h("div", { class: "fl-sh" });
  const sideBody = h("div", { class: "fl-sb" });
  const side = h("div", { class: "fl-side" }, sideHead, sideBody);
  const main = h("div", { class: "fl-main" }, canvas, side);
  const pyHead = h("div", { class: "fl-ph" }, "The same recipe as Python. Click a line to find its box.");
  const pyBody = h("div", { class: "fl-pb" });
  const py = h("div", { class: "fl-py" }, pyHead, pyBody);
  const cardEl = h("div", { class: "fl-card" });
  root.append(bar, note, main, py, cardEl);
  container.append(root);

  function legendDot(t, label) {
    return h("span", {}, h("i", { class: "fl-dot t-" + t }), " ", label);
  }

  // ---------------------------------------------------------------- calling the tools
  async function read(tool, args) {
    try {
      return await call(tool, args);
    } catch (e) {
      // the backend may be restarting for a moment: one more try
      await new Promise((r) => setTimeout(r, 1200));
      return await call(tool, args);
    }
  }
  const runId = () => G?.run?.id || null;
  function rowsOf(file, offset = 0, limit = 5) {
    const run = runId();
    const key = `${run}|${file}|${offset}|${limit}`;
    if (!rowsCache.has(key)) {
      const p = read("value_get", { run, file, offset, limit });
      p.catch(() => rowsCache.delete(key));
      rowsCache.set(key, p);
    }
    return rowsCache.get(key);
  }

  // ---------------------------------------------------------------- the hover card (one for the whole flow)
  const card = (() => {
    let timer = 0, ticket = 0, anchor = null, x = 0, y = 0;
    const place = () => {
      const w = cardEl.offsetWidth, hh = cardEl.offsetHeight;
      let left = x + 14, top = y + 16;
      if (left + w > innerWidth - 6) left = Math.max(6, x - w - 12);
      if (top + hh > innerHeight - 6) top = Math.max(6, innerHeight - hh - 6);
      cardEl.style.left = left + "px";
      cardEl.style.top = top + "px";
    };
    const hide = () => {
      clearTimeout(timer);
      ticket++;
      anchor = null;
      cardEl.classList.remove("open");
    };
    const show = async (target, make) => {
      if (drag || connecting) return;
      anchor = target;
      const mine = ++ticket;
      put(cardEl, h("div", { class: "fl-wait" }, "…"));
      cardEl.classList.add("open");
      place();
      let body;
      try {
        body = await make();
      } catch (e) {
        body = h("p", { class: "fl-dim" }, "Could not show this: " + cleanError(e));
      }
      if (mine !== ticket) return;
      if (!body) return hide();
      put(cardEl, body);
      place();
    };
    // the card grows when a picture in it has loaded: placed again then, so it stays on screen
    new ResizeObserver(() => cardEl.classList.contains("open") && place()).observe(cardEl);
    const attach = (el, make) => {
      // a click opens what the card would show (or a field): the card goes and stays away until the pointer leaves
      el.addEventListener("pointerdown", () => {
        hide();
        el._noCard = true;
      });
      el.addEventListener("pointerenter", (e) => {
        if (e.pointerType !== "mouse" || el._noCard) return;
        x = e.clientX;
        y = e.clientY;
        clearTimeout(timer);
        timer = setTimeout(() => show(el, make), cardEl.classList.contains("open") ? 60 : 260);
      });
      el.addEventListener("pointermove", (e) => {
        x = e.clientX;
        y = e.clientY;
        if (anchor === el && cardEl.classList.contains("open")) place();
      });
      el.addEventListener("pointerleave", () => {
        el._noCard = false;
        clearTimeout(timer);
        if (anchor === el || !anchor) hide();
      });
      return el;
    };
    return { attach, hide };
  })();
  const cardBox = (title, ...body) => h("div", {}, h("div", { class: "fl-ct" }, title), ...flat(body));

  // ---------------------------------------------------------------- the graph's lookups
  let NODE = {}, SET = {}, INPUT = {}, USERS = {}, BASE = [];
  function index() {
    NODE = Object.fromEntries((G?.nodes || []).map((n) => [n.id, n]));
    SET = Object.fromEntries((G?.settings || []).map((s) => [s.id, s]));
    INPUT = Object.fromEntries((G?.inputs || []).map((i) => [i.id, i]));
    BASE = [];
    for (const e of G?.edges || []) BASE.push({ from: e.from, to: e.to, wire: e.wire, port: e.port });
    for (const n of G?.nodes || [])
      for (const p of n.ports) {
        if (p.source === "input") BASE.push({ from: "in:" + p.input, to: n.id, wire: p.input, port: p.name });
        if (p.source === "value")
          for (const r of p.reads || []) if (INPUT[r]) BASE.push({ from: "in:" + r, to: n.id, wire: r, port: p.name });
      }
    USERS = {};
    for (const e of BASE) {
      const k = e.from + "|" + e.wire;
      (USERS[k] ||= []).includes(e.to) || USERS[k].push(e.to);
    }
  }
  const titleOf = (id) => {
    if (id?.startsWith("in:")) return INPUT[id.slice(3)]?.label || human(id.slice(3));
    const n = NODE[id];
    return n ? boxTitle(n) : id;
  };
  function boxTitle(n) {
    if (n.kind === "call") return BLOCKS[n.fn]?.title || n.title || human(n.fn);
    return n.title;
  }
  function wireValue(from, wire) {
    if (from?.startsWith("in:")) return undefined;
    return NODE[from]?.values?.[wire];
  }
  function wireType(from, wire) {
    if (from?.startsWith("in:")) return INPUT[from.slice(3)]?.kind === "corrections" ? "data" : "none";
    const n = NODE[from];
    if (!n) return "none";
    const v = n.values?.[wire];
    return n.types?.[wire] || (v !== undefined ? kindOf(v) : "none");
  }
  const columnsOf = (from, wire) => wireValue(from, wire)?.columns || [];

  // ---------------------------------------------------------------- cards' contents
  const figures = new Map();
  function figureSrc(output) {
    const key = `${runId()}|${output}`;
    if (!figures.has(key)) figures.set(key, Promise.resolve(O.figure({ run: runId(), output })).catch(() => null));
    return figures.get(key);
  }
  async function valueBody(v, big = false, wire = null) {
    if (v === undefined) return h("p", { class: "fl-dim" }, "Not calculated yet.");
    if (v && typeof v === "object" && v.kind === "table") {
      const cols = v.columns || [];
      const maxCols = big ? 99 : 8;
      let rows = null, total = v.rows, why = null;
      if (v.file && runId()) {
        try {
          const r = await rowsOf(v.file, 0, big ? 8 : 5);
          rows = r.rows;
          total = r.total ?? total;
        } catch (e) {
          why = cleanError(e);
        }
      }
      return h(
        "div",
        {},
        rows
          ? [
              h("p", { class: "fl-dim" }, cols.length > maxCols ? `first ${maxCols} of ${cols.length} columns · ` : "",
                `first ${Math.min(rows.length, total)} of ${plural(total, "row")}`),
              h("div", { class: "fl-scroll" }, tableEl(cols, rows, maxCols)),
              cols.length > maxCols ? h("p", { class: "fl-dim" }, "also: ", cols.slice(maxCols).join(", ")) : null,
            ]
          : [
              h("p", { class: "fl-dim" }, "Columns:"),
              h("div", { class: "fl-chips" }, cols.map((c) => h("span", { class: "fl-chip" }, colName(c)))),
              why ? h("p", { class: "fl-dim" }, why) : null,
            ],
      );
    }
    if (v && v.kind === "figure") {
      if (v.file && runId()) {
        const r = await rowsOf(v.file, 0, 1);
        if (r.data) return h("img", { class: "fl-fig", src: "data:image/png;base64," + r.data, alt: "figure" });
      }
      // an older run's figure has no file: one the method returns is read by the host (O.figure → an image's src)
      const out = !v.file && wire && (G?.outputs || []).find((o) => o.wire === wire && o.type === "figure");
      if (out && runId() && O.figure) {
        const src = await figureSrc(out.id);
        if (src) return h("img", { class: "fl-fig", src, alt: out.label || out.id });
      }
      return h("p", { class: "fl-dim" }, runId() ? "A figure (this older calculation kept no picture of it)." :
        "A figure.");
    }
    if (v && v.kind === "list") {
      const items = v.items || [];
      return h("div", {}, h("p", { class: "fl-dim" }, `${plural(v.length, "item")}` +
        (items.length < v.length ? `; the first ${items.length}:` : ":")),
      items.slice(0, big ? 30 : 6).map((x) => h("div", { class: "fl-mono" }, x && x.text ? x.text : shape(x))),
      items.length > (big ? 30 : 6) ? h("div", { class: "fl-dim" }, "…") : null);
    }
    if (v && (v.kind === "record" || v.kind === "dict")) {
      const items = Object.entries(v.fields || v.items || {});
      return h("div", {}, items.slice(0, big ? 60 : 10).map(([k, x]) =>
        h("div", { class: "fl-mono" }, h("b", {}, k), ": ", x && typeof x === "object" ? shape(x) : JSON.stringify(x))),
      items.length > (big ? 60 : 10)
        ? h("div", { class: "fl-dim" }, `and ${items.length - (big ? 60 : 10)} more`) : null);
    }
    if (v && (v.kind === "column" || v.kind === "array")) {
      const vals = (v.values || []).map(cell).join(", ");
      return h("p", { class: "fl-mono" }, (v.name ? v.name + ": " : "") + vals + (v.truncated ? ", …" : ""));
    }
    if (typeof v === "string") return h("p", { class: "fl-mono fl-wrap" }, v);
    return h("p", { class: "fl-mono" }, shape(v));
  }
  function wireCard(from, wire) {
    return async () => {
      const v = wireValue(from, wire);
      const users = (USERS[from + "|" + wire] || []).map(titleOf);
      if (from?.startsWith("in:")) return inputCard(INPUT[from.slice(3)]);
      return cardBox(
        h("span", {}, h("span", { class: "fl-mono" }, wire), "  ", h("span", { class: "fl-dim" }, longShape(v))),
        h("p", { class: "fl-dim" }, `made by ${titleOf(from)}` +
          (users.length ? ` · used by ${users.join(", ")}` : "")),
        await valueBody(v, false, wire),
        v && v.file ? h("p", { class: "fl-dim" }, v.kind === "figure" ? "Click to see it bigger." :
          "Click to see the whole table.") : null,
      );
    };
  }
  function inputCard(i) {
    if (!i) return null;
    return cardBox(
      i.label || human(i.id),
      i.kind === "corrections"
        ? h("p", {}, /reason/.test(i.label || "") ? "" : "Each with a written reason.",
          i.value != null ? ` This experiment has ${plural(i.value, "approved correction")}.` : "")
        : [
            h("p", { class: "fl-mono" }, i.value || "No file chosen yet."),
            h("p", { class: "fl-dim" }, "A raw file of the experiment" +
              (i.types?.length ? ` (${i.types.join(", ")})` : "") + ": copied into the store, never changed."),
          ],
      h("p", { class: "fl-dim" }, "In the code: ", h("span", { class: "fl-mono" }, i.id)),
    );
  }

  // ---------------------------------------------------------------- boxes
  function badge(text, cls = "", title) {
    return h("span", { class: "fl-badge " + cls, title }, text);
  }
  function statusBadges(n) {
    const out = [];
    if (n.status === "failed") out.push(badge("failed", "bad"));
    else if (n.status === "cached")
      out.push(badge("reused", "", "Not calculated again: its code and what went in didn't change, so the result " +
        "from before was kept."));
    else if (n.status === "not_run") out.push(badge("not calculated", "dim"));
    else if (n.ms != null) out.push(h("span", { class: "fl-ms" }, n.ms < 10 ? n.ms.toFixed(1) + " ms" :
      n.ms < 10000 ? Math.round(n.ms) + " ms" : (n.ms / 1000).toFixed(1) + " s"));
    out.push(badge("calculating…", "run"));
    return out;
  }
  /** A box's head or a "+" that a keyboard can reach: Tab to it, Enter or Space does what a click does. */
  function keyClick(el, label, onclick) {
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    el.setAttribute("aria-label", label);
    el.addEventListener("click", onclick);
    el.addEventListener("keydown", (e) => {
      if (e.target !== el || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      onclick({ target: el, currentTarget: el, clientX: r.left, clientY: r.bottom, stopPropagation() {},
        preventDefault() {} });
    });
    return el;
  }
  function menuButton(onclick) {
    return h("button", { class: "fl-more", title: "More", "aria-label": "More choices", "aria-haspopup": "menu",
      onclick: (e) => (e.stopPropagation(), onclick(e)) }, "⋯");
  }

  function renderNode(n) {
    const cls = ["fl-node", n.kind === "code" ? "code" : "", n.quality ? "q" : "", n.status ? "st-" + n.status : ""];
    const el = h("div", { class: cls.join(" "), "data-id": n.id });
    const title = h("span", { class: "fl-nt" }, boxTitle(n));
    const head = h(
      "div",
      { class: "fl-nh" },
      title,
      n.kind === "code" ? badge("code") : n.fn_kind === "toolbox" && !BLOCKS[n.fn] ? badge("toolbox", "dim") : null,
      statusBadges(n),
      menuButton((e) => nodeMenu(e, n)),
    );
    keyClick(head, `${boxTitle(n)}: look inside`, (e) => boxClick(n.id, e));
    card.attach(title, () =>
      cardBox(boxTitle(n), n.sentence ? h("p", {}, n.sentence) : null,
        n.kind === "call" ? h("p", { class: "fl-dim fl-mono" }, n.code.split("\n")[0].trim()) : null,
        h("p", { class: "fl-dim" }, O.inside === "panel" ? "Click to look inside: its code, with this run's values." :
          "Click to select it.")),
    );
    el.append(head);
    if (n.kind === "code") el.append(h("div", { class: "fl-code" }, highlight(n.code)));
    const defaults = [];
    for (const p of n.ports) {
      if (p.source === "default") {
        defaults.push(p);
        continue;
      }
      el.append(renderPort(n, p));
      if (p.missing?.length)
        el.append(h("div", { class: "fl-miss" }, "no column ", p.missing.map((c, i) => [i ? ", " : "",
          h("span", { class: "fl-mono" }, c)])));
    }
    if (defaults.length) {
      const open = O.editable && S.openDefaults.has(n.id);
      const row = h("div", { class: "fl-port set fl-defs" + (O.editable ? " live" : "") }, h("span", { class: "fl-sl" },
        open ? "fewer inputs" : `${plural(defaults.length, "other input")} as they are`));
      card.attach(row, () => cardBox("Left as they are", defaults.map((p) =>
        h("div", { class: "fl-mono" }, `${p.name} = ${p.code ?? "None"}`)), O.editable ? h("p", { class: "fl-dim" },
        open ? "Click to hide them." : "Click to show them and set one.") : null));
      if (O.editable)
        row.addEventListener("click", (e) => {
          e.stopPropagation();
          if (open) S.openDefaults.delete(n.id);
          else S.openDefaults.add(n.id);
          draw();
        });
      el.append(row);
      // shown when asked for, and while a wire is being dragged (to connect one of them)
      if (O.editable) for (const p of defaults) el.append(renderPort(n, p, !open));
    }
    for (const o of n.outs) el.append(renderOut(n, o));
    if (n.result) el.append(resultRow(n.result, n.id, n.outs[0]));
    if (n.status === "failed" && n.error) el.append(h("div", { class: "fl-err" }, stepError(n.error)));
    add(el, actionsRow(n));
    return el;
  }
  /** The host's own actions for a box (`boxActions(node)` → [{label, run, title?}]), as links at its foot. */
  function actionsOf(n) {
    try {
      return (O.boxActions?.(n) || []).filter((a) => a && a.label && a.run);
    } catch {
      return [];
    }
  }
  function actionsRow(n) {
    const acts = actionsOf(n);
    if (!acts.length) return null;
    return h("div", { class: "fl-acts" }, acts.map((a) => h("button", { class: "fl-act", title: a.title || null,
      onclick: (e) => (e.stopPropagation(), card.hide(), a.run(e)) }, a.label)));
  }

  function renderPort(n, p, folded = false) {
    const row = h("div", { class: "fl-port" + (folded ? " folded-def" : ""), "data-port": p.name, "data-node": n.id });
    const label = (text) => h("span", { class: "fl-sl" }, text);
    if (p.source === "wire" || p.source === "input") {
      const from = p.source === "wire" ? p.from : "in:" + p.input;
      const wire = p.wire || p.input;
      const t = p.source === "wire" ? wireType(from, wire) : wireType(from, wire);
      add(row, h("i", { class: "fl-dot t-" + dotOf(t) }), h("span", { class: "fl-pn fl-mono" }, wire),
        p.name !== wire && !/\[/.test(p.name) ? h("span", { class: "fl-dim fl-pn" }, "as " + p.name) : null);
      if (p.missing?.length) row.classList.add("misfit");
      card.attach(row, async () => {
        const base = await wireCard(from, wire)();
        if (p.columns_read?.length)
          base.append(h("p", { class: "fl-dim" }, `${boxTitle(n)} reads the columns `, p.columns_read.join(", "), "."));
        if (p.missing?.length)
          base.append(h("p", { class: "fl-warn" }, `Missing in ${wire}: ${p.missing.join(", ")}. ` +
            "Connect a table that has them, or change the step."));
        return base;
      });
      row.addEventListener("click", (e) => {
        e.stopPropagation();
        if (O.editable && n.kind === "call") portMenu(e, n, p);
        else if (p.source === "wire") openData(from, wire);
      });
      return row;
    }
    if (p.source === "setting") return settingRow(n, p, row);
    if (p.source === "formula" || p.source === "function") {
      add(row, label(p.name), h("span", {
        class: "fl-fn",
        title: "Look inside this function",
        onclick: (e) => (e.stopPropagation(), openInside(p.function, { push: true })),
      }, (p.source === "formula" ? "ƒ " : "") + p.function));
      row.classList.add("set");
      return row;
    }
    row.classList.add("set");
    if (p.source === "empty") {
      row.classList.add("needs");
      add(row, label(p.name), h("span", { class: "fl-needs" }, "not connected"));
      card.attach(row, () => cardBox(p.name, h("p", {}, `${boxTitle(n)} needs this input: connect a wire, a setting ` +
        "or a value to it."), O.editable ? h("p", { class: "fl-dim" }, "Click for the choices, or drag an output here.")
        : null));
      if (O.editable) row.addEventListener("click", (e) => (e.stopPropagation(), portMenu(e, n, p)));
      return row;
    }
    if (p.source === "default") {
      row.classList.add("def");
      const lit = pyValue(p.code ?? "None");
      add(row, label(BLOCKS[n.fn]?.fields?.find((f) => f.name === p.name)?.label || p.name),
        h("span", { class: "fl-sv dim" }, lit === undefined ? p.code : plainValue(lit)));
      card.attach(row, () => cardBox(p.name, h("p", { class: "fl-dim" }, "Left as it is: ",
        h("span", { class: "fl-mono" }, p.code ?? "None"), ". Click to set it, or drag an output here.")));
      row.addEventListener("click", (e) => (e.stopPropagation(), portMenu(e, n, p)));
      return row;
    }
    // a value written in place
    if (p.reads?.length) row.prepend(h("i", { class: "fl-dot t-none" }));
    const field = BLOCKS[n.fn]?.fields?.find((f) => f.name === p.name);
    add(row, label(field?.label || p.name), valueField(n, p));
    if (O.editable && n.kind === "call") {
      row.classList.add("live");
      row.addEventListener("click", (e) => {
        if (e.target.closest("input, select")) return;
        e.stopPropagation();
        portMenu(e, n, p);
      });
    }
    return row;
  }

  /** A value written in the code: plain text, or (editable) a field that writes the code back. */
  function valueField(n, p) {
    const code = p.code ?? "";
    const blockField = BLOCKS[n.fn]?.fields?.find((f) => f.name === p.name);
    const lit = pyValue(code);
    const label = blockField?.label || p.name;
    if (!O.editable || n.kind !== "call") {
      const text = lit === undefined ? code : plainValue(lit, blockField);
      const chip = h("span", { class: "fl-sv" + (lit === undefined ? " fl-mono" : ""), title: code }, text);
      card.attach(chip, () => cardBox(label, lit === undefined ? h("p", { class: "fl-mono fl-wrap" }, code) :
        h("p", { class: "fl-wrap" }, text), h("p", { class: "fl-dim" }, "Written in the method's code: ",
        h("span", { class: "fl-mono" }, `${p.name}=${code}`))));
      return chip;
    }
    const stop = (e) => e.stopPropagation();
    const commit = async (spec, anchor) => {
      if (spec.code !== undefined && spec.code.trim() === code.trim()) return;
      if (spec.value !== undefined && JSON.stringify(spec.value) === JSON.stringify(lit)) return;
      await edit([{ op: "connect", node: n.id, port: p.name, ...spec }], anchor).catch(() => draw());
    };
    if (blockField && lit !== undefined) {
      const c = blockControl(n, p, blockField, lit, commit);
      if (c) return c;
    }
    if (code === "True" || code === "False") {
      const box = h("input", { type: "checkbox", checked: code === "True", "aria-label": label,
        onpointerdown: stop, onclick: stop });
      box.addEventListener("change", () => commit({ code: box.checked ? "True" : "False" }, box));
      return box;
    }
    const input = textField(code, (t) => commit({ code: t }, input), label);
    card.attach(input, () => cardBox(label, h("p", { class: "fl-dim" },
      "Written in the code as Python: text in quotes (\"sample\"), numbers as they are, lists in [ ]."),
    blockField && blockField.kind === "columns" ? h("p", { class: "fl-dim" }, "Columns of the incoming table: ",
      blockColumns(n, blockField).join(", ") || "not known yet") : null));
    return input;
  }
  /** A field in a box: Enter or leaving it keeps what was typed, Escape puts the old text back. */
  function textField(value, onDone, label) {
    const stop = (e) => e.stopPropagation();
    const input = h("input", { class: "fl-field", value, spellcheck: "false", "aria-label": label,
      onpointerdown: stop, onclick: stop });
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") input.blur();
      if (e.key === "Escape") (input.value = value), input.blur();
    });
    input.addEventListener("change", () => input.value.trim() !== String(value).trim() && onDone(input.value.trim()));
    return input;
  }
  /**
   * A ready-made step's input in its box, as the same kind of field as in the form that added it: a column picked
   * from the incoming table, a choice, yes / no, a number, a text or a list. Each change is a `connect` with a value.
   */
  function blockControl(n, p, f, lit, commit) {
    const stop = (e) => e.stopPropagation();
    const label = f.label || p.name;
    const hint = (el, ...text) =>
      card.attach(el, () => cardBox(label, text.map((t) => h("p", { class: "fl-dim" }, t))));
    const cols = f.kind === "column" || f.kind === "columns" ? blockColumns(n, f) : [];
    if (f.kind === "column" && typeof lit === "string" && cols.length) {
      const sel = h("select", { class: "fl-field", "aria-label": label, onpointerdown: stop, onclick: stop },
        cols.includes(lit) ? null : h("option", { value: "", selected: true }, lit),
        cols.map((c) => h("option", { value: c, selected: c === lit }, c)));
      sel.addEventListener("change", () => sel.value && commit({ value: sel.value }, sel));
      hint(sel, "A column of the table that comes in (as it was in this run).");
      return sel;
    }
    if (f.kind === "choice" && !f.multiple && f.choices?.length && typeof lit === "string") {
      const sel = h("select", { class: "fl-field", "aria-label": label, onpointerdown: stop, onclick: stop },
        f.choices.some((c) => c.value === lit) ? null : h("option", { value: lit, selected: true }, lit),
        f.choices.map((c) => h("option", { value: c.value, selected: c.value === lit }, c.label)));
      sel.addEventListener("change", () => commit({ value: sel.value }, sel));
      return sel;
    }
    if (typeof lit === "boolean") {
      const box = h("input", { type: "checkbox", checked: lit, "aria-label": label, onpointerdown: stop,
        onclick: stop });
      box.addEventListener("change", () => commit({ value: box.checked }, box));
      return box;
    }
    const many = (f.kind === "columns" && cols.length) || (f.kind === "choice" && f.multiple && f.choices?.length);
    if (many && (typeof lit === "string" || (Array.isArray(lit) && lit.every((x) => typeof x === "string")))) {
      const picked = [lit].flat();
      const btn = h("button", { class: "fl-field fl-pickmany", type: "button", "aria-label": label,
        "aria-haspopup": "dialog", onpointerdown: stop }, plainValue(lit, f) || "none");
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const options = f.kind === "choice" ? f.choices.map((c) => [c.value, c.label]) :
          [...new Set([...cols, ...picked])].map((c) => [c, c]);
        const boxes = options.map(([v, l]) => h("label", { class: "fl-colpick" },
          h("input", { type: "checkbox", value: v, checked: picked.includes(v) }), l));
        ask(btn, {
          title: `${label} · ${boxTitle(n)}`,
          text: f.kind === "columns" ? "Columns of the table that comes in." : null,
          fields: [],
          extra: h("div", { class: "fl-cols" }, boxes),
          submit: "Use these",
          run: async (_v, where) => {
            const got = boxes.map((b) => b.firstChild).filter((b) => b.checked).map((b) => b.value);
            if (!got.length) {
              bubble(where, "Pick at least one.");
              return false;
            }
            const value = f.kind === "columns" && got.length === 1 && !Array.isArray(lit) ? got[0] : got;
            if (JSON.stringify(value) === JSON.stringify(lit)) return true;
            return tryEdit([{ op: "connect", node: n.id, port: p.name, value }], where);
          },
        });
      });
      return btn;
    }
    if (Array.isArray(lit) && lit.every((x) => x === null || typeof x !== "object")) {
      const input = textField(plainValue(lit, f), (t) => commit({ value: listOf(t) }, input), label);
      hint(input, f.kind === "range" ? "From, to: two numbers with a comma between." : "Items with commas between.");
      return input;
    }
    if (typeof lit === "number") {
      const input = textField(fmtNum(lit), (t) => {
        const x = Number(t.replace(",", "."));
        if (t === "" || !isFinite(x)) return bubble(input, "A number, please."), draw();
        commit({ value: x }, input);
      }, label);
      input.classList.add("num");
      return input;
    }
    if (typeof lit === "string") {
      const input = textField(lit, (t) => commit({ value: t }, input), label);
      hint(input, f.kind === "column" ? "A column of the table that comes in." : "A text: type it without quotes.");
      return input;
    }
    return null;
  }
  function blockColumns(n, field) {
    const out = [];
    for (const name of field.of || []) {
      const port = n.ports.find((q) => q.name === name);
      if (port?.source === "wire") for (const c of columnsOf(port.from, port.wire)) out.includes(c) || out.push(c);
    }
    return out;
  }

  function settingText(s, v) {
    if (v === undefined || v === null || v === "") return "—";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (Array.isArray(v)) {
      if (!v.length) return "all";
      if (v.every((x) => x === null || typeof x !== "object"))
        return v.length === 2 && v.every((x) => typeof x === "number") ? `${v[0]} – ${v[1]}` : v.join(", ");
      return plural(v.length, "item");
    }
    if (typeof v === "object") return Object.keys(v).join(", ");
    return typeof v === "number" ? fmtNum(v) : String(v);
  }
  /** A setting's value as short lines she can read: a list item or a table row per line, no brackets or quotes. */
  function readableLines(v, max = 12) {
    const one = (x) => (x === null || x === undefined ? "none" : typeof x === "boolean" ? (x ? "yes" : "no") :
      typeof x === "number" ? fmtNum(x) : Array.isArray(x) ? x.map(one).join(" – ") : typeof x === "object" ?
        Object.entries(x).map(([k, y]) => `${human(k).toLowerCase()} ${one(y)}`).join(", ") : String(x));
    let lines;
    if (Array.isArray(v)) lines = v.every((x) => x === null || typeof x !== "object") ? [v.map(one).join(", ")] :
      v.map(one);
    else if (v && typeof v === "object") lines = Object.entries(v).map(([k, x]) => `${k}: ${one(x)}`);
    else lines = [one(v)];
    return lines.length > max ? [...lines.slice(0, max), `and ${lines.length - max} more`] : lines;
  }
  const simpleSetting = (s) =>
    ["number", "integer", "text", "boolean", "choice"].includes(s.type) || (s.choices && s.choices.length <= 8);
  function settingRow(n, p, row) {
    const s = SET[p.setting] || { id: p.setting, label: human(p.setting), type: p.type };
    const v = s.id in S.pending ? S.pending[s.id] : s.value;
    row.classList.add("set");
    const chip = h("span", { class: "fl-sv" + (s.id in S.pending ? " edited" : "") }, settingText(s, v));
    add(row, h("span", { class: "fl-sl" }, s.label || human(s.id)), chip, s.unit ? h("span", { class: "fl-unit" },
      s.unit) : null);
    chip.addEventListener("click", (e) => {
      e.stopPropagation();
      card.hide();
      if (!chip.querySelector("input, select")) editSetting(s, chip, v);
    });
    chip.setAttribute("tabindex", "0");
    chip.setAttribute("role", "button");
    chip.setAttribute("aria-label", `${s.label || human(s.id)}: ${settingText(s, v)}. Change it`);
    chip.addEventListener("keydown", (e) => {
      if (e.target !== chip || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      chip.click();
    });
    card.attach(row, () => {
      const others = (s.used_by || []).filter((id) => id !== n.id).map(titleOf);
      return cardBox(s.label || human(s.id), s.help ? h("p", {}, s.help) : null,
        h("p", { class: "fl-dim" }, `a setting${s.group ? " (" + s.group + ")" : ""} · in the code: `,
          h("span", { class: "fl-mono" }, s.id)),
        readableLines(v).map((line) => h("div", { class: "fl-wrap" }, line)),
        s.set === false && s.default !== undefined ? h("p", { class: "fl-dim" }, "The method's default.") : null,
        others.length ? h("p", { class: "fl-dim" }, `also used by ${others.join(", ")}`) : null,
        h("p", { class: "fl-dim" }, simpleSetting(s) ? "Click the value to change it." :
          "A list or a table: click to edit it in its own form."));
    });
    return row;
  }
  function editSetting(s, chip, v) {
    const done = (value) => {
      if (JSON.stringify(value) === JSON.stringify(s.value)) {
        delete S.pending[s.id];
      } else {
        S.pending[s.id] = value;
        O.onSetting?.(s.id, value);
      }
      draw();
    };
    if (!simpleSetting(s)) return O.onOpen?.({ what: "settings", setting: s.id });
    if (s.type === "boolean") return done(!v);
    if (s.choices?.length) {
      const sel = h("select", { class: "fl-field" }, s.choices.map((c) => {
        const val = typeof c === "object" ? c.value : c;
        return h("option", { value: JSON.stringify(val), selected: val === v },
          typeof c === "object" ? c.label ?? val : c);
      }));
      chip.replaceChildren(sel);
      sel.focus();
      sel.addEventListener("change", () => done(JSON.parse(sel.value)));
      sel.addEventListener("blur", () => draw());
      return;
    }
    const input = h("input", { class: "fl-field", value: v ?? "", spellcheck: "false" });
    chip.replaceChildren(input);
    input.focus();
    input.select();
    let finished = false;
    const finish = (keep) => {
      if (finished) return;
      finished = true;
      if (!keep) return draw();
      const t = input.value.trim();
      if (s.type === "number" || s.type === "integer") {
        if (t === "") return done(null);
        const x = Number(t.replace(",", "."));
        if (!isFinite(x) || (s.type === "integer" && !Number.isInteger(x))) {
          bubble(input, s.type === "integer" ? "A whole number, please." : "A number, please.");
          finished = false;
          return;
        }
        return done(x);
      }
      done(t === "" && v == null ? null : t);
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("pointerdown", (e) => e.stopPropagation());
  }

  function renderOut(n, o) {
    const v = n.values?.[o];
    const t = n.types?.[o] || kindOf(v);
    const dot = h("i", { class: "fl-dot t-" + dotOf(t), "data-drag": "1" });
    const row = h("div", { class: "fl-out", "data-out": o }, h("span", { class: "fl-on" }, o),
      h("span", { class: "fl-os" }, n.status === "not_run" || n.status === "failed" || v == null ? "" : shape(v)), dot);
    if (O.editable) row.append(plusButton({ from: n.id, wire: o, type: t },
      "Add a step that uses this (or drag it to an input)"));
    card.attach(row, wireCard(n.id, o));
    row.addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.closest(".fl-add")) return;
      openData(n.id, o);
    });
    if (O.editable) row.addEventListener("pointerdown", (e) => startConnect(e, n.id, o, t));
    return row;
  }
  /** An output's "+": a click (or dragging it) is handled by startConnect; Enter or Space opens the same menu. */
  function plusButton(src, title) {
    const plus = h("b", { class: "fl-add", title, "data-drag": "1", tabindex: "0", role: "button",
      "aria-label": `${title.replace(/ \(.*\)$/, "")}: ${src.wire}`, "aria-haspopup": "menu" }, "+");
    plus.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      e.stopPropagation();
      openAddMenu(plus, src);
    });
    return plus;
  }
  function resultRow(r, from, wire) {
    const row = h("div", { class: "fl-res", onclick: (e) => (e.stopPropagation(), O.onOpen?.({ what: "results",
      table: r.id })) }, "★ ", r.label || human(r.id));
    card.attach(row, async () => {
      const v = wireValue(from, wire);
      return cardBox(`Result: ${r.label || human(r.id)}`, h("p", { class: "fl-dim" }, "what the method returns as ",
        h("span", { class: "fl-mono" }, r.id), v !== undefined ? " · " + longShape(v) : ""),
      await valueBody(v, false, wire),
      h("p", { class: "fl-dim" }, "Click to open it with the results."));
    });
    return row;
  }

  function renderInput(i) {
    const id = "in:" + i.id;
    const file = i.kind !== "corrections";
    const el = h("div", { class: "fl-node src", "data-id": id });
    const head = h("div", { class: "fl-nh" }, h("span", { class: "fl-nt" }, file ? "Raw file" : "Corrections"),
      badge(file ? "input" : "by hand"), O.editable && file ? menuButton((e) => inputMenu(e, i)) : null);
    keyClick(head, file ? `Raw file ${i.id}` : "Corrections", (e) => boxClick(id, e));
    card.attach(head, () => inputCard(i));
    el.append(head);
    if (file) el.append(h("div", { class: "fl-port set" }, h("span", { class: "fl-sl fl-file" },
      i.value || (G?.experiment ? "no file chosen" : i.label || ""))));
    else if (i.value != null) el.append(h("div", { class: "fl-port set" }, h("span", { class: "fl-sl" },
      plural(i.value, "approved correction"))));
    const t = file ? "none" : "data";
    const row = h("div", { class: "fl-out", "data-out": i.id }, h("span", { class: "fl-on" }, i.id),
      h("i", { class: "fl-dot t-" + t, "data-drag": "1" }), O.editable ? plusButton({ from: id, wire: i.id,
        type: file ? "file" : "data" }, "Add a step that reads it") : null);
    card.attach(row, () => inputCard(i));
    if (O.editable) row.addEventListener("pointerdown", (e) => startConnect(e, id, i.id, file ? "file" : "data"));
    el.append(row);
    add(el, actionsRow({ id, input: i }));
    return el;
  }

  /** The quality checks or a group, folded into one box. */
  function renderFolded(id, title, members, ins, outs, isQuality) {
    const el = h("div", { class: "fl-node folded" + (isQuality ? " q" : ""), "data-id": id });
    const failed = members.filter((m) => NODE[m]?.status === "failed").length;
    const head = h("div", { class: "fl-nh", title: "Click to unfold" }, h("span", { class: "fl-nt" }, title),
      badge(plural(members.length, "step")), failed ? badge(`${failed} failed`, "bad") : null,
      h("span", { class: "fl-unfold" }, "unfold"));
    keyClick(head, `${title}: unfold`, (e) => {
      e.stopPropagation();
      if (isQuality) S.quality = true;
      else S.folded.delete(id.slice(2));
      draw();
    });
    card.attach(head, () => cardBox(title, h("p", {}, members.map(titleOf).join(", ")),
      h("p", { class: "fl-dim" }, "Folded away: click the title to unfold.")));
    el.append(head);
    for (const [wire, from] of ins) {
      const row = h("div", { class: "fl-port", "data-port": wire }, h("i", { class: "fl-dot t-" +
        dotOf(wireType(from, wire)) }), h("span", { class: "fl-pn fl-mono" }, wire));
      card.attach(row, wireCard(from, wire));
      if (!from.startsWith("in:")) row.addEventListener("click", (e) => (e.stopPropagation(), openData(from, wire)));
      el.append(row);
    }
    if (isQuality) {
      const counts = flagCounts();
      el.append(h("div", { class: "fl-qbody" }, counts === null ? h("span", { class: "fl-dim" }, "not calculated") :
        Object.keys(counts).length ? Object.entries(counts).map(([k, c]) => badge(flagWord(k, c),
          k === "info" ? "" : k === "error" ? "bad" : "warn")) : h("span", { class: "fl-dim" }, "nothing to report")));
    }
    add(el, actionsRow({ id, folded: true, quality: !!isQuality, members, title }));
    for (const [wire, from] of outs) {
      const v = wireValue(from, wire);
      const row = h("div", { class: "fl-out", "data-out": wire }, h("span", { class: "fl-on" }, wire),
        h("span", { class: "fl-os" }, shape(v)), h("i", { class: "fl-dot t-" + dotOf(wireType(from, wire)) }));
      card.attach(row, wireCard(from, wire));
      row.addEventListener("click", (e) => (e.stopPropagation(), openData(from, wire)));
      el.append(row);
      const r = NODE[from]?.result;
      if (r) el.append(resultRow(r, from, wire));
    }
    return el;
  }
  function flagWord(k, c) {
    if (k === "info") return plural(c, "note");
    if (k === "warning") return plural(c, "warning");
    if (k === "error") return plural(c, "error");
    return `${c} ${k}`;
  }
  function flagCounts() {
    if (!G?.run) return null;
    const r = (RUNS || []).find((x) => x.id === G.run.id);
    if (r?.summary?.flags) return r.summary.flags;
    const f = (G.outputs || []).find((o) => o.id === "flags");
    if (f?.value?.kind === "table") return f.value.rows ? { info: f.value.rows } : {};
    return {};
  }

  // ---------------------------------------------------------------- what is drawn (folding) and its wires
  function model() {
    const rep = new Map();
    for (const g of G.groups || []) if (S.folded.has(g.id)) for (const id of g.nodes) rep.set(id, "G:" + g.id);
    if (!S.quality) for (const id of G.quality || []) if (!rep.has(id)) rep.set(id, "Q");
    const R = (id) => rep.get(id) || id;
    const edges = [], seen = new Set();
    for (const e of BASE) {
      const from = R(e.from), to = R(e.to);
      if (from === to) continue;
      const port = rep.has(e.to) ? e.wire : e.port;
      const key = `${from}|${to}|${port}|${e.wire}`;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ from, to, port, wire: e.wire, maker: e.from, misfit: !!NODE[e.to]?.ports.find((p) =>
        p.name === e.port && p.missing?.length) && !rep.has(e.to) });
    }
    const boxes = [];
    for (const i of G.inputs || []) boxes.push({ id: "in:" + i.id, el: renderInput(i) });
    for (const n of G.nodes) if (!rep.has(n.id)) boxes.push({ id: n.id, el: renderNode(n) });
    const reps = new Map();
    for (const [id, r] of rep) (reps.get(r) || reps.set(r, []).get(r)).push(id);
    for (const [r, members] of reps) {
      const ins = new Map(), outs = new Map();
      for (const e of edges) {
        if (e.to === r) ins.set(e.wire, e.maker);
        if (e.from === r) outs.set(e.wire, e.maker);
      }
      for (const m of members) if (NODE[m]?.result) outs.set(NODE[m].outs[0], m);
      const g = r === "Q" ? null : (G.groups || []).find((x) => "G:" + x.id === r);
      boxes.push({ id: r, el: renderFolded(r, g ? g.title : "Quality checks", members, [...ins], [...outs], !g) });
    }
    return { boxes, edges, rep };
  }

  // ---------------------------------------------------------------- the layout (layers left to right)
  function layout(boxes, edges) {
    const GAP = 18, COL = 64;
    const ids = boxes.map((b) => b.id);
    const preds = new Map(ids.map((i) => [i, []])), succs = new Map(ids.map((i) => [i, []]));
    for (const e of edges) {
      if (!preds.has(e.to) || !succs.has(e.from)) continue;
      preds.get(e.to).push(e.from);
      succs.get(e.from).push(e.to);
    }
    const layer = new Map(ids.map((i) => [i, 0]));
    for (let changed = true, guard = 0; changed && guard < 300; guard++) {
      changed = false;
      for (const id of ids)
        for (const p of preds.get(id))
          if (layer.get(id) < layer.get(p) + 1) {
            layer.set(id, layer.get(p) + 1);
            changed = true;
          }
    }
    for (const id of ids)
      if (!preds.get(id).length && succs.get(id).length)
        layer.set(id, Math.max(0, Math.min(...succs.get(id).map((s) => layer.get(s))) - 1));
    const items = new Map();
    for (const b of boxes) {
      const ports = {}, outs = {};
      for (const r of b.el.querySelectorAll("[data-port]")) ports[r.dataset.port] = r.offsetTop + r.offsetHeight / 2;
      for (const r of b.el.querySelectorAll("[data-out]")) outs[r.dataset.out] = r.offsetTop + r.offsetHeight / 2;
      const first = Object.values(outs)[0];
      items.set(b.id, { id: b.id, el: b.el, layer: layer.get(b.id), w: b.el.offsetWidth, h: b.el.offsetHeight, ports,
        outs, out: first ?? 14 });
    }
    const segs = [], segSeen = new Set();
    const seg = (a, b, port, e) => {
      const k = `${a}|${b}|${port}|${e.wire}`;
      if (segSeen.has(k)) return;
      segSeen.add(k);
      segs.push({ a, b, port, e });
    };
    for (const e of edges) {
      if (!items.has(e.from) || !items.has(e.to)) continue;
      let a = e.from;
      for (let l = layer.get(e.from) + 1; l < layer.get(e.to); l++) {
        const id = `d:${e.from}:${e.wire}:${l}`; // one trunk per wire, shared by all its long edges
        if (!items.has(id)) items.set(id, { id, dummy: true, layer: l, w: 0, h: 8, ports: {}, outs: {}, out: 4 });
        seg(a, id, null, e);
        a = id;
      }
      seg(a, e.to, e.port, e);
    }
    const layers = [];
    for (const it of items.values()) (layers[it.layer] ||= []).push(it);
    for (let i = 0; i < layers.length; i++) layers[i] ||= [];
    layers.forEach((arr) => arr.forEach((it, i) => (it.pos = i)));
    const before = new Map(), after = new Map();
    for (const s of segs) {
      (before.get(s.b) || before.set(s.b, []).get(s.b)).push(s);
      (after.get(s.a) || after.set(s.a, []).get(s.a)).push(s);
    }
    for (let iter = 0; iter < 12; iter++) {
      const fwd = iter % 2 === 0;
      const order = fwd ? layers.slice(1) : layers.slice(0, -1).reverse();
      for (const arr of order) {
        for (const it of arr) {
          const nb = (fwd ? before.get(it.id) : after.get(it.id)) || [];
          it.bary = nb.length ? nb.reduce((t, s) => t + items.get(fwd ? s.a : s.b).pos, 0) / nb.length : it.pos;
        }
        arr.sort((x, y) => x.bary - y.bary || x.pos - y.pos);
        arr.forEach((it, i) => (it.pos = i));
      }
    }
    const outY = (it, wire) => (it.dummy ? 4 : it.outs[wire] ?? it.out);
    const portY = (it, port) => (it.dummy ? 4 : it.ports[port] ?? it.h / 2);
    for (const arr of layers) {
      let y = 0;
      for (const it of arr) {
        it.y = y;
        y += it.h + GAP;
      }
    }
    for (let iter = 0; iter < 16; iter++) {
      const fwd = iter % 2 === 0;
      for (const arr of fwd ? layers : [...layers].reverse()) {
        if (!arr.length) continue;
        for (const it of arr) {
          const nb = (fwd ? before.get(it.id) : after.get(it.id)) || [];
          const want = nb.map((s) => fwd
            ? items.get(s.a).y + outY(items.get(s.a), s.e.wire) - portY(it, s.port)
            : items.get(s.b).y + portY(items.get(s.b), s.port) - outY(it, s.e.wire));
          it.want = want.length ? want.reduce((a, b) => a + b, 0) / want.length : it.y;
        }
        const down = [], up = [];
        arr.forEach((it, i) => (down[i] = i ? Math.max(it.want, down[i - 1] + arr[i - 1].h + GAP) : it.want));
        for (let i = arr.length - 1; i >= 0; i--)
          up[i] = i < arr.length - 1 ? Math.min(arr[i].want, up[i + 1] - arr[i].h - GAP) : arr[i].want;
        arr.forEach((it, i) => (it.y = (down[i] + up[i]) / 2));
      }
    }
    let top = Infinity;
    for (const it of items.values()) top = Math.min(top, it.y);
    if (!isFinite(top)) top = 0;
    let x = 0;
    const lw = [];
    layers.forEach((arr, i) => {
      lw[i] = Math.max(24, ...arr.map((it) => it.w));
      for (const it of arr) {
        it.x = x;
        it.y -= top;
      }
      x += lw[i] + COL;
    });
    let height = 0;
    for (const it of items.values()) height = Math.max(height, it.y + it.h);
    const paths = segs.map((s) => {
      const a = items.get(s.a), b = items.get(s.b);
      const x1 = a.dummy ? a.x + lw[a.layer] : a.x + a.w, y1 = a.y + outY(a, s.e.wire);
      const x2 = b.x, y2 = b.y + portY(b, s.port);
      const k = Math.max(26, (x2 - x1) / 2);
      const d = (a.dummy ? `M${a.x} ${y1}L${x1} ${y1}` : `M${x1} ${y1}`) +
        `C${x1 + k} ${y1} ${x2 - k} ${y2} ${x2} ${y2}`;
      return { ...s, d };
    });
    return { items, paths, width: Math.max(0, x - COL), height };
  }

  // ---------------------------------------------------------------- drawing
  let MODEL = null;
  function draw() {
    if (destroyed) return;
    if (!root.isConnected || !root.clientWidth) {
      S.drawLater = true;
      return;
    }
    S.drawLater = false;
    card.hide();
    // a form she is filling in stays open when the flow is drawn again (new numbers came while she typed); a menu,
    // which points at boxes that may have moved, closes
    if (pop && !pop.matches("form")) closePop();
    drawBar();
    for (const el of [...world.children]) if (el !== wires) el.remove();
    wires.replaceChildren();
    if (!G) return;
    index();
    const anchor = S.sel && VIEW?.items.get(S.sel);
    const before = anchor ? { x: anchor.x * S.zoom + S.tx, y: anchor.y * S.zoom + S.ty } : null;
    MODEL = model();
    for (const b of MODEL.boxes) world.append(b.el);
    const L = layout(MODEL.boxes, MODEL.edges);
    VIEW = L;
    for (const it of L.items.values()) {
      if (it.dummy) continue;
      it.el.style.left = it.x + "px";
      it.el.style.top = it.y + "px";
    }
    drawGroups(L);
    for (const p of L.paths) {
      const t = wireType(p.e.maker, p.e.wire);
      const final = p.e.to;
      const attrs = { d: p.d, class: `fl-seg s-${dotOf(t)}${p.e.misfit ? " misfit" : ""}`,
        "data-w": `${p.e.from}|${p.e.wire}`, "data-from": p.e.from, "data-to": final };
      const line = svgEl("path", attrs);
      const hit = svgEl("path", { d: p.d, class: "fl-hit" });
      const key = attrs["data-w"];
      const hot = (on) =>
        wires.querySelectorAll(".fl-seg").forEach((s) => s.dataset.w === key && s.classList.toggle("hot", on));
      hit.addEventListener("pointerenter", () => hot(true));
      hit.addEventListener("pointerleave", () => hot(false));
      card.attach(hit, wireCard(p.e.maker, p.e.wire));
      hit.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!p.e.maker.startsWith("in:")) openData(p.e.maker, p.e.wire);
      });
      wires.append(line, hit);
    }
    wires.setAttribute("width", Math.max(1, L.width));
    wires.setAttribute("height", Math.max(1, L.height));
    if (S.sel && !L.items.has(S.sel)) {
      const r = MODEL.rep.get(S.sel);
      if (!r) S.sel = null;
    }
    drawEmpty();
    if (!S.viewSet && L.width > 0 && canvas.clientWidth > 0) {
      S.viewSet = true;
      const cw = canvas.clientWidth, ch = canvas.clientHeight;
      S.zoom = Math.max(0.8, Math.min(1, (cw - 24) / L.width));
      S.tx = L.width * S.zoom < cw ? (cw - L.width * S.zoom) / 2 : 16;
      S.ty = Math.max(12, (ch - L.height * S.zoom) / 2);
    } else if (before && S.sel && L.items.get(S.sel)) {
      const it = L.items.get(S.sel);
      S.tx = before.x - it.x * S.zoom;
      S.ty = before.y - it.y * S.zoom;
    }
    classes();
    place();
    if (S.python) drawPython();
  }
  function drawGroups(L) {
    for (const g of G.groups || []) {
      if (S.folded.has(g.id)) continue;
      const its = g.nodes.map((id) => L.items.get(id)).filter(Boolean);
      if (!its.length) continue;
      const x0 = Math.min(...its.map((i) => i.x)) - 10, y0 = Math.min(...its.map((i) => i.y)) - 30;
      const x1 = Math.max(...its.map((i) => i.x + i.w)) + 10, y1 = Math.max(...its.map((i) => i.y + i.h)) + 10;
      const style = `left:${x0}px;top:${y0}px;width:${x1 - x0}px;height:${y1 - y0}px`;
      const frame = h("div", { class: "fl-group", style },
        h("div", { class: "fl-gh" }, h("span", { class: "fl-gt" }, g.title),
          h("button", { class: "fl-gb", onclick: (e) => (e.stopPropagation(), S.folded.add(g.id), draw()) }, "fold"),
          O.editable ? h("button", { class: "fl-gb", title: "Take the steps out of this group", onclick: (e) =>
            (e.stopPropagation(), edit([{ op: "ungroup", group: g.id }], e.target)) }, "ungroup") : null));
      world.insertBefore(frame, wires.nextSibling);
    }
  }
  function drawEmpty() {
    const nothing = !G.nodes.length && !(G.inputs || []).length;
    empty.classList.toggle("open", nothing);
    if (!nothing) return;
    put(empty, h("div", { class: "fl-empty-in" }, h("b", {}, "Nothing here yet."),
      h("p", {}, O.editable ? "Start with the raw file your instrument gives you; then add the steps that work on it." :
        "This method has no steps yet."),
      O.editable ? h("button", { class: "layr-button primary", onclick: (e) => addRawFile(e) }, "Add a raw file")
        : null));
  }

  /** Selection, trace, running and multi-selection as classes: no new layout. */
  function classes() {
    if (!VIEW) return;
    const keep = S.trace && S.sel ? traced(S.sel) : null;
    const runIds = runningBoxes();
    for (const it of VIEW.items.values()) {
      if (it.dummy) continue;
      it.el.classList.toggle("sel", !!S.sel && it.id === rep(S.sel));
      it.el.classList.toggle("multi", S.multi.has(it.id));
      it.el.classList.toggle("running", runIds.has(it.id));
      if (keep) it.el.classList.toggle("keep", keep.has(it.id));
    }
    for (const s of wires.querySelectorAll(".fl-seg"))
      s.classList.toggle("keep", !!keep && keep.has(s.dataset.from) && keep.has(s.dataset.to));
    world.classList.toggle("fade", !!keep);
    if (S.python) markPython();
    drawMini();
  }
  function rep(id) {
    return MODEL?.rep.get(id) || id;
  }
  function traced(id) {
    const out = new Set();
    const ids = id === "Q" || id.startsWith("G:") ? [...MODEL.rep].filter(([, r]) => r === id).map(([n]) => n) : [id];
    const walk = (i) => {
      if (out.has(i)) return;
      out.add(i);
      for (const e of BASE) if (e.to === i) walk(e.from);
    };
    ids.forEach(walk);
    return new Set([...out].map(rep));
  }
  function runningBoxes() {
    const out = new Set();
    if (!S.running) return out;
    for (const n of G?.nodes || []) if (n.id === S.running || n.fn === S.running) out.add(rep(n.id));
    return out;
  }

  function place() {
    world.style.transform = `translate(${S.tx}px,${S.ty}px) scale(${S.zoom})`;
    drawMini();
  }
  function drawMini() {
    if (!VIEW || !VIEW.width) return mini.replaceChildren();
    const W = mini.clientWidth || 180, H = mini.clientHeight || 64;
    const k = Math.min(W / VIEW.width, H / VIEW.height);
    mini.replaceChildren();
    const rect = (x, y, w, hh, cls) =>
      mini.append(svgEl("rect", { x, y, width: Math.max(w, 1), height: Math.max(hh, 1), class: cls }));
    for (const it of VIEW.items.values())
      if (!it.dummy) rect(it.x * k, it.y * k, it.w * k, it.h * k, it.id === S.sel ? "sel" : it.el.classList.contains(
        "st-failed") ? "bad" : "box");
    rect((-S.tx / S.zoom) * k, (-S.ty / S.zoom) * k, (canvas.clientWidth / S.zoom) * k,
      (canvas.clientHeight / S.zoom) * k, "view");
    mini._k = k;
  }
  mini.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    const k = mini._k;
    if (!k) return;
    const go = (ev) => {
      const b = mini.getBoundingClientRect();
      S.tx = canvas.clientWidth / 2 - ((ev.clientX - b.left) / k) * S.zoom;
      S.ty = canvas.clientHeight / 2 - ((ev.clientY - b.top) / k) * S.zoom;
      place();
    };
    go(e);
    const move = (ev) => go(ev);
    const up = () => (removeEventListener("pointermove", move), removeEventListener("pointerup", up));
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
  });
  function fit() {
    if (!VIEW || !VIEW.width) return;
    const cw = canvas.clientWidth, ch = canvas.clientHeight;
    S.zoom = Math.max(0.2, Math.min(1, (cw - 24) / VIEW.width, (ch - 24) / VIEW.height));
    S.tx = (cw - VIEW.width * S.zoom) / 2;
    S.ty = (ch - VIEW.height * S.zoom) / 2;
    place();
  }
  function centre(id) {
    const it = VIEW?.items.get(rep(id));
    if (!it) return;
    const cw = canvas.clientWidth, ch = canvas.clientHeight;
    const sx = it.x * S.zoom + S.tx, sy = it.y * S.zoom + S.ty;
    if (sx > 10 && sy > 10 && sx + it.w * S.zoom < cw - 10 && sy + Math.min(it.h, 200) * S.zoom < ch - 10) return;
    S.tx = cw / 2 - (it.x + it.w / 2) * S.zoom;
    S.ty = Math.min(16, ch / 2 - (it.y + it.h / 2) * S.zoom);
    if (it.h * S.zoom < ch) S.ty = ch / 2 - (it.y + it.h / 2) * S.zoom;
    place();
  }
  function zoomBy(f, cx, cy) {
    cx ??= canvas.clientWidth / 2;
    cy ??= canvas.clientHeight / 2;
    const z = Math.min(1.8, Math.max(0.2, S.zoom * f));
    S.tx = cx - ((cx - S.tx) / S.zoom) * z;
    S.ty = cy - ((cy - S.ty) / S.zoom) * z;
    S.zoom = z;
    place();
  }

  // ---------------------------------------------------------------- panning, zooming (mouse and touch)
  let drag = null;
  const pointers = new Map();
  canvas.addEventListener("pointerdown", (e) => {
    closePop();
    if (e.target.closest(".fl-mini, .fl-zoom, .fl-group .fl-gh, .fl-empty-in")) return;
    // a finger may pan from anywhere but a control; a mouse pans from the empty canvas
    const touch = e.pointerType !== "mouse";
    if (!touch && e.target.closest(".fl-node, .fl-hit")) return;
    if (touch && e.target.closest("input, select, button, textarea, [data-drag]")) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      drag = { pinch: Math.hypot(a.x - b.x, a.y - b.y), zoom: S.zoom, moved: true };
      return;
    }
    drag = { x: e.clientX - S.tx, y: e.clientY - S.ty, sx: e.clientX, sy: e.clientY, moved: false };
    canvas.classList.add("drag");
    if (!touch) canvas.setPointerCapture?.(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (drag.pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const r = canvas.getBoundingClientRect();
      const f = (Math.hypot(a.x - b.x, a.y - b.y) / drag.pinch) * drag.zoom / S.zoom;
      zoomBy(f, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
      return;
    }
    if (drag.pinch) return;
    if (Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) > 3) drag.moved = true;
    S.tx = e.clientX - drag.x;
    S.ty = e.clientY - drag.y;
    place();
  });
  const endPan = (e) => {
    pointers.delete(e.pointerId);
    if (!drag) return;
    const clicked = !drag.moved && !drag.pinch;
    if (pointers.size === 0) drag = null;
    canvas.classList.remove("drag");
    if (clicked && e.type === "pointerup") {
      // a click on empty space: nothing selected any more
      if (S.sel || S.multi.size) {
        S.multi.clear();
        if (!side.classList.contains("open")) S.sel = null;
        classes();
      }
    }
  };
  canvas.addEventListener("pointerup", endPan);
  canvas.addEventListener("pointercancel", endPan);
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    if (e.ctrlKey || e.metaKey) zoomBy(e.deltaY < 0 ? 1.1 : 0.9, e.clientX - r.left, e.clientY - r.top);
    else {
      S.tx -= e.shiftKey ? e.deltaY : e.deltaX;
      S.ty -= e.shiftKey ? 0 : e.deltaY;
      place();
    }
  }, { passive: false });

  // ---------------------------------------------------------------- selection
  function boxClick(id, e) {
    e?.stopPropagation();
    closePop();
    if (e && (e.shiftKey || e.ctrlKey || e.metaKey) && O.editable && NODE[id]) {
      if (S.multi.has(id)) S.multi.delete(id);
      else S.multi.add(id);
      if (S.sel && NODE[S.sel] && !S.multi.size) S.multi.add(S.sel);
      drawBar();
      return classes();
    }
    S.multi.clear();
    selectBox(id, true);
  }
  function selectBox(id, open) {
    S.sel = id;
    S.stack = [];
    const n = NODE[id];
    classes();
    drawBar();
    if (n) O.onSelect?.({ node: n.id, fn: n.fn || null, line: n.line });
    if (!open) return;
    if (O.inside !== "panel") return;
    if (n) {
      if (n.kind === "call" && n.fn) openInside(n.fn, { node: n });
      else openCodeBox(n);
    } else if (id.startsWith("in:")) openSideInput(INPUT[id.slice(3)]);
    requestAnimationFrame(() => centre(id));
  }

  // ---------------------------------------------------------------- the bar
  function drawBar() {
    const kids = [];
    kids.push(picker());
    kids.push(h("span", { class: "fl-sp" }));
    if ((G?.quality || []).length)
      kids.push(h("button", { class: "layr-button small" + (S.quality ? " active" : ""), title: S.quality ?
        "Fold the quality checks into one box" : "Show every quality check as its own box", onclick: () => {
        S.quality = !S.quality;
        if (!S.quality && G.quality.includes(S.sel)) S.sel = "Q";
        draw();
      } }, S.quality ? "Fold the checks" : `Unfold the checks (${G.quality.length})`));
    if (O.editable && G) {
      kids.push(h("button", { class: "layr-button small", title: "A raw file the method reads (the reader step is " +
        "added with it)", onclick: (e) => addRawFile(e) }, "+ Raw file"));
      kids.push(h("button", { class: "layr-button small", title: "A setting the method's steps can use",
        onclick: (e) => addSetting(e) }, "+ Setting"));
      if (S.multi.size > 1)
        kids.push(h("button", { class: "layr-button small", onclick: (e) => groupSelected(e) },
          `Group ${S.multi.size} steps`));
    }
    kids.push(h("button", { class: "layr-button small" + (S.python ? " active" : ""),
      title: "Show the recipe as Python under the flow", onclick: () => togglePython() }, "Python"));
    // where the flow is too low for the colours' key in its corner, it sits here (see the ResizeObserver)
    kids.push(card.attach(h("span", { class: "fl-legend-bar", role: "img", "aria-label": "Colours: table blue, " +
      "quality flags orange, other data purple, figure green" }, ["table", "flags", "data", "figure"].map((t) =>
      h("i", { class: "fl-dot t-" + t }))), () => cardBox("What the colours mean", h("div", { class: "fl-legend col" },
      legendDot("table", "table"), legendDot("flags", "quality flags"), legendDot("data", "other data (lists, " +
      "records, numbers)"), legendDot("figure", "figure")))));
    put(bar, kids);
    drawNote();
  }
  function picker() {
    const box = h("span", { class: "fl-pick" });
    if (O.picker === "runs") return runPicker(box);
    const methodName = G?.method?.name;
    if (EXPS === null) {
      box.append(h("b", { class: "fl-title" }, methodName || ""));
      return box;
    }
    if (!EXPS.length) {
      add(box, h("b", { class: "fl-title" }, methodName || ""),
        h("span", { class: "fl-dim" }, "· no experiment uses it yet"),
        h("button", { class: "layr-button small", onclick: () => O.onOpen?.({ what: "new-experiment",
          method: G?.method?.id || S.method }) }, "New experiment"));
      return box;
    }
    const exp = h("select", { class: "layr-select fl-sel", title: "Whose data flows through the method" },
      S.experiment ? null : h("option", { value: "", selected: true }, "No data: pick an experiment"),
      EXPS.map((e) => h("option", { value: e.experiment, selected: e.experiment === S.experiment }, e.title)));
    exp.addEventListener("change", () => pick(exp.value || null, null));
    box.append(h("span", { class: "fl-dim fl-lab" }, "Data"), exp);
    return runPicker(box);
  }
  /** Which calculation of the chosen experiment flows through: its latest run, an earlier one, or a trial run. */
  function runPicker(box) {
    if (!S.experiment) return box;
    const runs = (RUNS || []).filter((r) => !r.trial);
    if (!runs.length) box.append(h("span", { class: "fl-dim" }, RUNS === null ? "…" : "not calculated yet"));
    else {
      const trial = S.run && !runs.some((r) => r.id === S.run);
      const sel = h("select", { class: "layr-select fl-sel", title: "Which calculation of it", "aria-label": "Run" },
        trial ? h("option", { value: S.run }, "Your change, calculated just now") : null,
        runs.map((r, i) => h("option", { value: i === 0 ? "" : r.id },
          `${i === 0 ? "Latest · " : ""}${when(r.started)}${statusWord(r.status)}`)));
      sel.value = trial || (S.run && S.run !== runs[0].id) ? S.run : "";
      sel.addEventListener("change", () => pick(S.experiment, sel.value || null));
      box.append(sel);
    }
    return box;
  }
  const statusWord = (s) => (s === "done" ? "" : s === "failed" ? " · failed" : s === "stopped" ? " · stopped" :
    s ? " · " + s : "");
  function pick(experiment, run) {
    if (experiment === S.experiment && run === S.run) return;
    const expChanged = experiment !== S.experiment;
    S.experiment = experiment;
    S.run = run;
    S.pending = {};
    if (expChanged) RUNS = null;
    O.onPick?.({ experiment, run });
    load();
  }
  function drawNote() {
    const parts = [];
    if (G?.stale)
      parts.push(h("span", { class: "fl-warn" }, "The method changed since this calculation: steps marked ",
        h("b", {}, "not calculated"), " have no numbers yet.", O.editable ? "" : " Run it to see them all."));
    if (G?.run?.status === "failed" && O.runNote !== false)
      parts.push(h("span", { class: "fl-bad" }, "This calculation stopped: ",
        String(G.run.error || "a step failed").replace(/\.+$/, ""), "."));
    if (G?.messages?.length) {
      const m = h("span", { class: "fl-dim fl-msgs" },
        `${plural(G.messages.length, "thing")} in the code can't be drawn well`);
      card.attach(m, () => cardBox("What the flow can't show well", G.messages.map((x) =>
        h("p", {}, x.line ? h("span", { class: "fl-dim" }, `line ${x.line}: `) : null, x.message))));
      parts.push(m);
    }
    if (S.problem) {
      const p = S.problem;
      const said = String(p.message || "").replace(/^Python can't read line \d+:\s*/, "").replace(/\.+$/, "");
      parts.push(h("span", { class: "fl-bad" }, "The code can't be read yet",
        p.line ? [" (line ", h("a", { class: "fl-link", onclick: () => O.onOpen?.({ what: "function", fn: null,
          line: p.line }) }, String(p.line)), ")"] : null, `: ${said}. `,
        h("span", { class: "fl-dim" }, "The flow shows it as it was before.")));
    }
    if (S.error) parts.push(h("span", { class: "fl-bad" }, S.error));
    put(note, parts.map((p, i) => [i ? h("span", { class: "fl-dim" }, " · ") : null, p]));
    note.classList.toggle("open", parts.length > 0);
  }

  // ---------------------------------------------------------------- side panel
  let sideOpener = null; // the box that opened the inside panel: it gets the focus back when the panel closes
  function openSide(title, { crumbs, buttons } = {}) {
    destroyEditor();
    if (!side.classList.contains("open")) sideOpener = root.contains(document.activeElement) &&
      !side.contains(document.activeElement) ? document.activeElement : null;
    side.classList.add("open");
    root.classList.add("side-open");
    side.setAttribute("aria-label", title);
    put(sideHead, h("span", { class: "fl-st", title }, title), buttons,
      h("button", { class: "layr-button small ghost icon", title: "Close", "aria-label": "Close",
        onclick: () => closeSide(true) }, "✕"));
    put(sideBody, crumbs);
    sideBody.scrollTop = 0;
    return sideBody;
  }
  function closeSide(restore = false) {
    const had = side.contains(document.activeElement);
    destroyEditor();
    side.classList.remove("open");
    root.classList.remove("side-open");
    S.side = null;
    S.stack = [];
    if (S.trace) S.trace = false;
    classes();
    if (restore || had) {
      const back = sideOpener?.isConnected ? sideOpener : VIEW?.items.get(rep(S.sel))?.el?.querySelector(".fl-nh");
      back?.focus({ preventScroll: true });
    }
  }
  function destroyEditor() {
    if (editor) editor.destroy();
    editor = null;
  }
  function traceButton() {
    if (!S.sel) return null;
    return h("button", { class: "layr-button small" + (S.trace ? " active" : ""), title: "Dim every box that doesn't " +
      "feed this one", onclick: (e) => {
      S.trace = !S.trace;
      e.currentTarget.classList.toggle("active", S.trace);
      classes();
    } }, "Only what feeds it");
  }
  const sec = (text, ...extra) => h("h4", { class: "fl-h4" }, text, ...extra);
  function crumbsEl() {
    if (S.stack.length < 2) return null;
    const back = (s, i) => () => {
      S.stack = S.stack.slice(0, i + 1);
      openInside(s.fn, { call: s.call, keep: true });
    };
    return h("div", { class: "fl-crumbs" }, S.stack.slice(0, -1).map((s, i) =>
      [h("a", { class: "fl-link", onclick: back(s, i) }, human(s.fn)), " › "]),
    h("b", {}, human(S.stack[S.stack.length - 1].fn)));
  }
  function valueChips(values) {
    return h("div", { class: "fl-chips" }, Object.entries(values || {}).map(([k, v]) => {
      const t = kindOf(v);
      const chip = h("span", { class: "fl-chip" + (typeof v === "object" && v ? " v" : "") },
        BOX_TYPES.has(t) ? h("i", { class: "fl-dot t-" + t }) : null, `${k} = ${shape(v)}`);
      if (v && typeof v === "object")
        card.attach(chip, async () => cardBox(h("span", {}, h("span", { class: "fl-mono" }, k), "  ",
          h("span", { class: "fl-dim" }, longShape(v))), await valueBody(v)));
      return chip;
    }));
  }

  /** Inside a function: its code with this run's values, formulas, what it calls. */
  async function openInside(fn, { push = false, node = null, call: callId = null, keep = false } = {}) {
    if (!keep) {
      if (push) S.stack.push({ fn, call: callId });
      else S.stack = [{ fn, call: callId }];
    } else if (S.stack.length) S.stack[S.stack.length - 1].call = callId;
    S.side = { kind: "inside", fn, node: node?.id || null };
    const top = S.stack.length < 2;
    const toolbox = node && (node.fn_kind === "toolbox" || node.fn_kind === "class" || node.fn_kind === "builtin");
    const ownFn = !toolbox;
    const body = openSide(top && node?.fn === fn ? boxTitle(node) : human(fn), {
      crumbs: crumbsEl(),
      buttons: [top ? traceButton() : null, top && node ? actionsOf(node).map((a) => h("button", { class:
        "layr-button small", title: a.title || null, onclick: (e) => a.run(e) }, a.label)) : null,
      ownFn ? h("button", { class: "layr-button small", title: "Open it in the " +
        "code editor", onclick: () => O.onOpen?.({ what: "function", fn, line: S.side?.line ?? node?.line ?? null }) },
      "Edit this step") : null],
    });
    body.append(h("div", { class: "fl-wait" }, "…"));
    const mine = ++S.insideTicket;
    let info = null, err = null;
    try {
      if (runId() && !toolbox) info = await read("function_inside", { run: runId(), function: fn, ...(callId != null ?
        { call: callId } : {}) });
    } catch (e) {
      err = e;
    }
    if (mine !== S.insideTicket || S.side?.fn !== fn) return;
    if (info) return drawInside(body, fn, info, node);
    // a ready-made step, or no run: the code explained without this run's values
    let ex = null;
    try {
      ex = await read("explain_function", { function: fn,
        ...(S.source ? { source: S.source } : { method: G?.method?.id }), ...(runId() ? { run: runId() } : {}) });
    } catch (e) {
      err = err || e;
    }
    if (mine !== S.insideTicket || S.side?.fn !== fn) return;
    put(body, crumbsEl());
    if (!ex) return body.append(h("p", { class: "fl-bad" }, cleanError(err)));
    add(body, h("div", { class: "fl-chips" },
      badge(ex.kind === "toolbox" ? "ready-made (toolbox)" : ex.kind || "function"),
      h("span", { class: "fl-chip" }, fn)),
    ex.sentence ? h("p", {}, h("b", {}, ex.sentence)) : null,
    ex.text ? h("p", { class: "fl-dim" }, ex.text) : null);
    if (node && runId()) {
      const ins = {};
      for (const p of node.ports)
        if (p.source === "wire" && wireValue(p.from, p.wire) !== undefined) ins[p.name] = wireValue(p.from, p.wire);
        else if (p.source === "setting") ins[p.name] = { kind: "number", text: settingText(SET[p.setting],
          SET[p.setting]?.value) };
        else if (p.source === "value") {
          const lit = pyValue(p.code);
          ins[p.name] = { kind: "number", text: lit === undefined ? p.code : plainValue(lit) };
        }
      if (Object.keys(ins).length) add(body, sec("What went in"), valueChips(ins));
    }
    if (ex.formulas?.length) add(body, sec("Formulas"), ex.formulas.map((f) => formulaBox(f.latex, f.code)));
    if (ex.code) {
      add(body, sec(toolbox ? "Its code (read only; copy it into the method to change it)" : "The code"));
      const box = h("div", { class: "fl-editor" });
      body.append(box);
      editor = createEditor(box, { doc: ex.code, readOnly: true, chipsBelow: true,
        firstLine: !toolbox && ex.line ? ex.line : 1 });
    }
    if (node) {
      const outs = node.outs.filter((o) => node.values?.[o] !== undefined);
      for (const o of outs)
        add(body, sec("What came out"), h("p", { class: "fl-dim" }, h("span", { class: "fl-mono" }, o), " · ",
          longShape(node.values[o])), await valueBody(node.values[o], true, o));
    }
    if (!runId())
      body.append(h("p", { class: "fl-dim" }, G?.experiment ? "Not calculated yet: run the experiment to see its " +
        "numbers here." : EXPS?.length ? "Pick an experiment above to see its numbers here." :
        "Its numbers show here once an experiment uses the method and has run."));
  }

  function drawInside(body, fn, f, node) {
    S.side.line = f.line;
    put(body, crumbsEl());
    const kindWord = { step: "step", formula: "formula", helper: "helper", recipe: "recipe" }[f.kind] || f.kind;
    const head = h("div", { class: "fl-chips" }, badge(kindWord), h("span", { class: "fl-chip" }, fn));
    if (f.count > 1) {
      const calls = f.calls || [];
      if (calls.length > 1) {
        const sel = h("select", { class: "layr-select fl-sel small", title: "Which call to show" }, calls.map((id, i) =>
          h("option", { value: id, selected: id === f.call?.id }, `call ${i + 1}`)));
        sel.addEventListener("change", () => openInside(fn, { call: Number(sel.value), keep: true, node }));
        add(head, badge(`called ${f.count}×`), h("span", { class: "fl-dim" }, "showing"), sel,
          calls.length < f.count ? h("span", { class: "fl-dim", title: "Only the first calls keep their values" },
            `(${calls.length} kept)`) : null);
      } else add(head, badge(`called ${f.count}× · showing the first`));
    }
    if (f.call?.cached) add(head, badge("reused from before"));
    add(body, head, f.sentence ? h("p", {}, h("b", {}, f.sentence)) : null);
    if (f.note) body.append(h("p", { class: "fl-dim" }, f.note));
    if (f.error || f.call?.error)
      body.append(h("div", { class: "fl-err" }, stepError(f.error || f.call.error)));
    const args = f.call?.args || {};
    if (Object.keys(args).length) add(body, sec("What went in"), valueChips(args));

    // the code with its values
    const base = f.line;
    const loops = (f.statements || []).filter((s) => s.loop || s.n > 1);
    const kept = [...new Set(loops.flatMap((s) => (s.rounds || []).map((r) => r.round)))].sort((a, b) => a - b);
    const total = Math.max(0, ...loops.map((s) => s.n || 0));
    let round = S.round[fn] && kept.includes(S.round[fn]) ? S.round[fn] : kept[0] || 1;
    const nav = h("span", { class: "fl-rounds" });
    // the line where the step stopped stays open (a check that stopped it is not folded away) and is shown
    const stopped = (f.statements || []).find((st) => (st.rounds || []).some((r) => r.failed || r.error));
    const guards = (f.guards || []).filter((g) => !(stopped && g.line <= stopped.line && stopped.line <= g.end_line));
    const guardsBtn = guards.length ? h("a", { class: "fl-link", onclick: () => editor && editor.toggleFolds() },
      `${plural(guards.length, "check")} folded`) : null;
    add(body, sec("The code, with this run's values", " ", nav, " ", guardsBtn));
    const box = h("div", { class: "fl-editor" });
    body.append(box);
    editor = createEditor(box, { doc: f.code || "", readOnly: true, firstLine: base, chipsBelow: true });
    const lineOf = (n) => n - base + 1;
    const step = (dir) => {
      const i = kept.indexOf(round);
      round = kept[Math.max(0, Math.min(kept.length - 1, i + dir))];
      S.round[fn] = round;
      chips();
    };
    const chips = () => {
      const map = {};
      for (const st of f.statements || []) {
        const rs = st.rounds || [];
        const r = rs.find((x) => x.round === round) || (st.loop || st.n > 1 ? rs.filter((x) => x.round <= round).pop() :
          null) || rs[0];
        if (!r) continue;
        const list = (map[lineOf(st.end_line || st.line)] ||= []);
        for (const [name, v] of Object.entries(r.values || {})) {
          const t = kindOf(v);
          list.push({ name, text: shape(v), type: dotOf(t), title: `${name} · ${longShape(v)}` + (st.n > 1 ?
            ` · round ${r.round} of ${st.n}` : ""), card: v && typeof v === "object" ? () => valueBody(v) : undefined,
          onClick: v && v.kind === "table" && v.file ? () => openValue(name, v) : undefined });
        }
        if (r.error || r.failed) list.push({ name: "", text: "stopped here", type: "none", title:
          stepError(r.error || f.error || f.call?.error) || "The step stopped at this line." });
        if (st.loop && st.n > 1) list.push({ name: "", text: `${ord(r.round)} of ${st.n}`, type: "none",
          title: "Click for the next round that was kept", onClick: () => step(kept.indexOf(round) >= kept.length - 1 ?
            -kept.length : 1) });
      }
      if (f.returned !== undefined) {
        const lines = (f.code || "").split("\n");
        let at = -1;
        for (let i = lines.length - 1; i >= 0; i--) if (/^\s*return\b/.test(lines[i])) { at = i + 1; break; }
        if (at > 0) (map[at] ||= []).push({ name: "return", text: shape(f.returned), type: dotOf(kindOf(f.returned)),
          card: f.returned && typeof f.returned === "object" ? () => valueBody(f.returned) : undefined });
      }
      editor.setValues(map);
      put(nav, kept.length > 1 ? [h("button", { class: "layr-button small icon ghost", title: "Previous round",
        onclick: () => step(-1) }, "‹"), h("span", { class: "fl-dim" }, `round ${round} of ${total}`),
      h("button", { class: "layr-button small icon ghost", title: "Next round", onclick: () => step(1) }, "›")]
        : null);
    };
    chips();
    const folds = [];
    if (f.docstring)
      folds.push({ from: lineOf(f.docstring.line), to: lineOf(f.docstring.end_line), label: "its description",
        kind: "text" });
    for (const g of guards)
      folds.push({ from: lineOf(g.line), to: lineOf(g.end_line), label: `stops with a message if ${g.condition}` });
    if (folds.length) editor.setFolds(folds);
    if (stopped) requestAnimationFrame(() => editor?.reveal(lineOf(stopped.line)));

    // formulas used inside, with this run's numbers
    const own = f.formulas || [];
    const used = (f.uses || []).filter((u) => (u.formulas || []).length);
    if (own.length || used.length) {
      add(body, sec("Formulas"));
      for (const x of own) body.append(formulaBox(x.latex, x.code));
      for (const u of used)
        body.append(h("div", { class: "fl-fm" },
          h("div", {}, h("a", { class: "fl-link", onclick: () => drill(u) }, human(u.function)),
            h("span", { class: "fl-dim" }, `  × ${u.count}`)),
          u.sentence ? h("div", { class: "fl-dim" }, u.sentence) : null,
          u.formulas.map((x) => texEl(x.latex, x.code, true)),
          (u.example?.latex || []).map((x) => h("div", { class: "fl-num" }, texEl(x, null, true)))));
    }
    const calls = (f.uses || []).filter((u) => u.direct);
    if (calls.length)
      add(body, sec("Functions it uses"), h("div", { class: "fl-chips" }, calls.map((u) => {
        const chip = h("a", { class: "fl-chip fl-link", onclick: () => drill(u) }, u.function, u.count > 1 ?
          h("span", { class: "fl-dim" }, ` ×${u.count}`) : null);
        card.attach(chip, () => cardBox(human(u.function), u.sentence ? h("p", {}, u.sentence) : null,
          h("p", { class: "fl-dim" }, `${u.kind || "function"} · called ${u.count}× here · click to look inside`)));
        return chip;
      })));
    if (f.returned !== undefined)
      Promise.resolve(valueBody(f.returned, true)).then((el) => {
        if (S.side?.fn !== fn) return;
        add(body, sec("What came out"), h("p", { class: "fl-dim" }, longShape(f.returned)), el);
      });
    if (f.values_note) body.append(h("p", { class: "fl-dim" }, f.values_note));
  }
  function drill(u) {
    const node = u.kind === "toolbox" ? { fn_kind: "toolbox", ports: [], outs: [] } : null;
    openInside(u.function, { push: true, node });
  }
  function texEl(latex, code, display) {
    if (O.tex && latex) {
      try {
        return O.tex(latex, display);
      } catch {
        /* falls through to the code */
      }
    }
    return h("div", { class: "fl-mono" }, code || latex || "");
  }
  const formulaBox = (latex, code) => h("div", { class: "fl-fm" }, texEl(latex, code, true));

  function openCodeBox(n) {
    S.side = { kind: "code", node: n.id };
    const body = openSide(n.title, { buttons: [traceButton(), h("button", { class: "layr-button small", onclick: () =>
      O.onOpen?.({ what: "function", fn: null, line: n.line }) }, "Edit in the code")] });
    add(body, h("div", { class: "fl-chips" }, badge("code"), h("span", { class: "fl-dim" }, `line ${n.line}`)),
      h("p", { class: "fl-dim" }, "Lines of the recipe that aren't one step: they are shown as they are."),
      h("div", { class: "fl-snippet" }, highlight(n.code, { lineNumbers: true, firstLine: n.line })));
    const ins = {};
    for (const p of n.ports) if (p.source === "wire") ins[p.wire] = wireValue(p.from, p.wire);
    if (Object.keys(ins).length) add(body, sec("What it reads"), valueChips(ins));
    const outs = Object.fromEntries(n.outs.map((o) => [o, n.values?.[o]]).filter(([, v]) => v !== undefined));
    if (Object.keys(outs).length) add(body, sec("What it makes"), valueChips(outs));
    if (n.error) body.append(h("div", { class: "fl-err" }, n.error));
  }
  function openSideInput(i) {
    if (!i) return;
    S.side = { kind: "input" };
    const body = openSide(i.kind === "corrections" ? "Corrections" : "Raw file");
    body.append(inputCard(i));
    if (i.kind === "corrections") body.append(h("button", { class: "layr-button small", onclick: () =>
      O.onOpen?.({ what: "corrections" }) }, "Open the corrections"));
  }

  /** A table in full, with pages. */
  async function openData(from, wire, offset = 0) {
    if (from?.startsWith("in:")) return openSideInput(INPUT[from.slice(3)]);
    const v = wireValue(from, wire);
    S.side = { kind: "data", from, wire };
    const users = (USERS[from + "|" + wire] || []).map(titleOf);
    const n = NODE[from];
    const body = openSide(wire, {
      buttons: n?.result ? h("button", { class: "layr-button small", onclick: () => O.onOpen?.({ what: "results",
        table: n.result.id }) }, "★ Open the result") : null,
    });
    add(body, h("div", { class: "fl-chips" }, badge(n?.result ? "result" : "wire"),
      h("span", { class: "fl-chip" }, wire), badge(longShape(v) || "not calculated")),
    h("p", { class: "fl-dim" }, `made by ${titleOf(from)}` + (users.length ? ` · used by ${users.join(", ")}` : "")));
    await tableInto(body, v, offset, () => openData(from, wire, offset), (o) => openData(from, wire, o), wire);
  }
  async function openValue(name, v, offset = 0) {
    S.side = { kind: "value", name };
    const body = openSide(name, { crumbs: null, buttons: S.stack.length ? h("button", { class: "layr-button small",
      onclick: () => {
        const top = S.stack[S.stack.length - 1];
        openInside(top.fn, { keep: true, call: top.call });
      } },
    "‹ Back to the code") : null });
    add(body, h("div", { class: "fl-chips" }, badge("inside a step"), h("span", { class: "fl-chip" }, name),
      badge(longShape(v))));
    await tableInto(body, v, offset, null, (o) => openValue(name, v, o));
  }
  async function tableInto(body, v, offset, _again, go, wire = null) {
    if (!v || v.kind !== "table" || !v.file || !runId()) return body.append(await valueBody(v, true, wire));
    const LIMIT = 50;
    const wait = h("div", { class: "fl-wait" }, "…");
    body.append(sec("Columns"),
      h("div", { class: "fl-chips" }, (v.columns || []).map((c) => h("span", { class: "fl-chip" }, colName(c)))), wait);
    let r;
    try {
      r = await rowsOf(v.file, offset, LIMIT);
    } catch (e) {
      wait.replaceWith(h("p", { class: "fl-bad" }, cleanError(e)));
      return;
    }
    const total = r.total ?? v.rows;
    const pager = h("div", { class: "fl-pager" },
      h("button", { class: "layr-button small", title: "Earlier rows", disabled: offset <= 0,
        onclick: () => go(Math.max(0, offset - LIMIT)) }, "‹"),
      h("span", { class: "fl-dim" },
        total ? `rows ${offset + 1}–${Math.min(total, offset + LIMIT)} of ${total}` : "no rows"),
      h("button", { class: "layr-button small", title: "Later rows", disabled: offset + LIMIT >= total,
        onclick: () => go(offset + LIMIT) }, "›"),
      r.stored === "sampled" ? h("span", { class: "fl-dim" }, "· only the first rows were kept") : null);
    wait.replaceWith(pager, h("div", { class: "fl-scroll big" }, tableEl(r.columns || v.columns, r.rows || [])));
  }

  // ---------------------------------------------------------------- the Python strip
  function togglePython() {
    S.python = !S.python;
    py.classList.toggle("open", S.python);
    drawBar();
    if (S.python) ensureSource().then(() => drawPython());
    requestAnimationFrame(() => drawMini());
  }
  function drawPython() {
    py.classList.toggle("open", S.python);
    if (!S.python || !G) return;
    const src = S.source;
    if (src == null) return put(pyBody, h("div", { class: "fl-wait" }, "…"));
    const lines = src.split(/\r?\n/);
    const a = G.method.line, b = G.method.end_line;
    const pre = highlight(lines.slice(a - 1, b).join("\n"), { lineNumbers: true, firstLine: a });
    pre.classList.add("fl-pre");
    [...pre.querySelectorAll(".cmx-hl-line")].forEach((el, i) => {
      const line = a + i;
      el.dataset.line = line;
      const n = (G.nodes || []).find((x) => x.line <= line && line <= x.end_line);
      if (!n) return;
      el.classList.add("fl-pl");
      el.dataset.node = n.id;
      el.addEventListener("click", () => {
        S.multi.clear();
        selectBox(n.id, true);
        centre(n.id);
      });
      el.addEventListener("pointerenter", () => VIEW?.items.get(rep(n.id))?.el?.classList.add("hot"));
      el.addEventListener("pointerleave", () => VIEW?.items.get(rep(n.id))?.el?.classList.remove("hot"));
    });
    put(pyBody, pre);
    markPython();
  }
  function markPython() {
    const sel = NODE[S.sel] ? S.sel : null;
    let first = null;
    for (const el of pyBody.querySelectorAll(".fl-pl")) {
      const on = !!sel && el.dataset.node === sel;
      el.classList.toggle("sel", on);
      if (on && !first) first = el;
    }
    if (first) {
      const top = first.offsetTop - pyBody.clientHeight / 3;
      if (first.offsetTop < pyBody.scrollTop || first.offsetTop > pyBody.scrollTop + pyBody.clientHeight - 20)
        pyBody.scrollTop = Math.max(0, top);
    }
  }
  async function ensureSource() {
    if (S.source != null) return S.source;
    const id = G?.method?.id || S.method || S.methodId;
    if (!id) return null;
    try {
      const m = await read("method_get", { method: id, ...(G?.method?.version ? { version: G.method.version } : {}) });
      S.source = m.source;
      S.sourceFetched = true;
      FUNCS = functionsOf(S.source);
    } catch (e) {
      S.error = "Could not read the method's code: " + cleanError(e);
      drawNote();
    }
    return S.source;
  }
  function functionsOf(src) {
    const out = [];
    if (!src) return out;
    const re = /^def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)[^:]*:\s*\n(\s+)(?:[rR]?("""|'''))?([^\n]*)/gm;
    let m;
    while ((m = re.exec(src))) {
      const params = m[2].split(",").map((p) => p.trim().split(/[:=]/)[0].trim())
        .filter((p) => p && !p.startsWith("*"));
      out.push({ name: m[1], params, sentence: m[4] ? m[5].replace(/("""|''').*$/, "").trim() : "" });
    }
    if (G?.method?.recipe) return out.filter((f) => f.name !== G.method.recipe);
    return out;
  }

  // ---------------------------------------------------------------- editing
  async function edit(ops, where, { select: wantSelect } = {}) {
    card.hide();
    const src = await ensureSource();
    if (src == null) {
      bubble(where, "The method's code isn't loaded, so it can't be changed here.");
      return null;
    }
    const before = new Set((G?.nodes || []).map((n) => n.id));
    let res;
    root.classList.add("busy");
    try {
      res = await call("flow_edit", { source: src, ops, ...(S.experiment ? { experiment: S.experiment } : {}),
        ...(S.run ? { run: S.run } : {}) });
    } catch (e) {
      root.classList.remove("busy");
      bubble(where, cleanError(e));
      throw e;
    }
    root.classList.remove("busy");
    const old = G?.method || {};
    S.source = res.source;
    S.sourceFetched = false;
    FUNCS = functionsOf(S.source);
    G = res.flow;
    G.method = { ...old, ...G.method, id: G.method?.id ?? old.id, version: G.method?.version ?? old.version };
    if (!G.experiment && S.experiment) G.experiment = { id: S.experiment };
    const added = (G.nodes || []).filter((n) => !before.has(n.id)).map((n) => n.id);
    if (wantSelect) S.sel = wantSelect;
    else if (added.length) S.sel = added[added.length - 1];
    else if (S.sel && !(G.nodes || []).some((n) => n.id === S.sel) && !S.sel.startsWith("in:")) S.sel = null;
    S.multi.clear();
    index();
    draw();
    if (added.length) centre(S.sel);
    O.onSource?.(res.source, { ops, diff: res.diff, check: res.check, ok: res.ok, added, flow: res.flow });
    return res;
  }
  /** Runs an edit from a form: its error is shown in the form (the form stays open). */
  const tryEdit = (ops, where, opts) => edit(ops, where, opts).then(() => true, () => false);

  function nodeMenu(e, n) {
    const items = [];
    if (n.kind === "call" && O.inside === "panel") items.push(["Look inside", () => selectBox(n.id, true)]);
    items.push(["Only what feeds it", () => {
      selectBox(n.id, O.inside === "panel");
      S.trace = true;
      classes();
      openSideTraceSync();
    }]);
    for (const a of actionsOf(n)) items.push([a.label, (ev) => a.run(ev)]);
    if (n.kind === "call" && (n.fn_kind === "step" || n.fn_kind === "helper"))
      items.push(["Edit this step", () => O.onOpen?.({ what: "function", fn: n.fn, line: null })]);
    else items.push(["Show it in the code", () => O.onOpen?.({ what: "function", fn: null, line: n.line })]);
    if (O.editable) {
      items.push("-");
      for (const o of n.outs)
        items.push([n.outs.length > 1 ? `Rename ${o}…` : "Rename its result…", (ev) => ask(ev, {
          title: `A new name for ${o}`,
          fields: [{ name: "to", label: "Name", value: o,
            help: "Letters, digits and _; a unit at the end (conc_mM)." }],
          submit: "Rename",
          run: (v, where) => tryEdit([{ op: "rename_wire", wire: o, to: v.to.trim(), node: n.id }], where),
        })]);
      if (n.result)
        items.push(["Not a result any more", () =>
          edit([{ op: "remove_result", name: n.result.id }], e.target).catch(() => {})]);
      else if (n.outs.length) items.push(["Make it a result…", (ev) => ask(ev, {
        title: "Return it as a result",
        fields: [{ name: "label", label: "What it is called", value: human(n.outs[0]) }],
        submit: "Make it a result",
        run: (v, where) => tryEdit([{ op: "add_result", wire: n.outs[0], label: v.label.trim() || undefined }], where),
      })]);
      if (n.group) items.push(["Ungroup", () => edit([{ op: "ungroup", group: n.group }], e.target).catch(() => {})]);
      else items.push(["Put in a group…", (ev) => {
        if (!S.multi.size) S.multi.add(n.id);
        groupSelected(ev);
      }]);
      items.push("-", ["Remove this step", () => edit([{ op: "remove_step", node: n.id }], e.target).catch(() => {}),
        "danger"]);
    }
    menu(e, items);
  }
  function openSideTraceSync() {
    const b = sideHead.querySelector(".layr-button.small");
    if (b && /feeds/.test(b.textContent)) b.classList.toggle("active", S.trace);
  }
  function inputMenu(e, i) {
    menu(e, [
      ["Add a step that reads it", (ev) => openAddMenu(ev, { from: "in:" + i.id, wire: i.id, type: "file" })],
      "-",
      ["Remove this raw file", () => edit([{ op: "remove_input", input: i.id, name: i.id }], e.target).catch(() => {}),
        "danger"],
    ]);
  }
  function portMenu(e, n, p) {
    const items = [];
    if (p.source === "wire") items.push(["Show the data", () => openData(p.from, p.wire)]);
    const settings = (G.settings || []).filter((s) => s.id !== p.setting);
    if (settings.length)
      items.push(["Use a setting…", (ev) => ask(ev, {
        title: `${p.name} of ${boxTitle(n)}: use a setting`,
        fields: [{ name: "setting", label: "Setting", kind: "select", options: settings.map((s) => [s.id, s.label ||
          s.id]) }],
        submit: "Use it",
        run: (v, where) => tryEdit([{ op: "connect", node: n.id, port: p.name, setting: v.setting }], where),
      })]);
    items.push(["Make it a setting…", (ev) => ask(ev, {
      title: `${p.name} as a setting of the method`,
      fields: [
        { name: "name", label: "Name in the code", value: SET[p.name] ? p.name + "_2" : p.name },
        { name: "label", label: "What it is", value: human(p.name) },
        { name: "unit", label: "Unit (if any)", value: "" },
      ],
      submit: "Make it a setting",
      run: (v, where) => {
        const name = v.name.trim();
        const op = { op: "add_setting", name, label: v.label.trim() || undefined, unit: v.unit.trim() || undefined };
        if (p.source === "value" && p.code) op.code = p.code;
        else if (p.source === "default" && p.code) op.code = p.code;
        return tryEdit([op, { op: "connect", node: n.id, port: p.name, setting: name }], where);
      },
    })]);
    items.push(["Put a value in place…", (ev) => ask(ev, {
      title: `${p.name} of ${boxTitle(n)}`,
      fields: [{ name: "code", label: "Value (as Python)", value: p.source === "value" || p.source === "default" ?
        p.code ?? "" : "", help: "Text in quotes (\"sample\"), numbers as they are, lists in [ ]." }],
      submit: "Put it in",
      run: (v, where) => tryEdit([{ op: "connect", node: n.id, port: p.name, code: v.code }], where),
    })]);
    if (p.source !== "default" && p.source !== "empty")
      items.push(["Disconnect", () =>
        edit([{ op: "disconnect", node: n.id, port: p.name }], e.target).catch(() => {})]);
    menu(e, items);
  }

  // ---------------------------------------------------------------- popups: menus, forms, bubbles
  let pop = null;
  let popOpener = null; // what had the focus when the first menu or form of a chain opened: it gets it back
  function closePop(restore = false) {
    const had = pop?.contains(document.activeElement);
    pop?.remove();
    pop = null;
    if ((restore || had) && popOpener?.isConnected) popOpener.focus({ preventScroll: true });
  }
  function noteOpener(at) {
    const el = at?.target?.closest?.("button, select, input, [tabindex]") || (at?.focus ? at : null) ||
      (root.contains(document.activeElement) ? document.activeElement : null);
    if (el && root.contains(el) && !el.closest(".fl-menu, .fl-form")) popOpener = el;
  }
  function placePop(el, at) {
    const r = root.getBoundingClientRect();
    const pt = at?.clientX != null ? { x: at.clientX, y: at.clientY } : at?.getBoundingClientRect ? (() => {
      const b = at.getBoundingClientRect();
      return { x: b.left, y: b.bottom };
    })() : at || { x: r.left + r.width / 2 - 140, y: r.top + 60 };
    root.append(el);
    const w = el.offsetWidth, hh = el.offsetHeight;
    el.style.left = Math.max(6, Math.min(pt.x - r.left + 6, r.width - w - 6)) + "px";
    el.style.top = Math.max(6, Math.min(pt.y - r.top + 6, r.height - hh - 6)) + "px";
  }
  function menu(e, items) {
    noteOpener(e);
    closePop();
    card.hide();
    const el = h("div", { class: "fl-menu", role: "menu", onpointerdown: (ev) => ev.stopPropagation() });
    for (const it of items) {
      if (it === "-") el.append(h("div", { class: "fl-msep", role: "separator" }));
      else if (it.head) el.append(h("div", { class: "fl-mh" }, it.head));
      else {
        const [label, run, cls, small] = it;
        el.append(h("button", { type: "button", role: "menuitem", class: "fl-mi " + (cls || ""), onclick: (ev) => {
          ev.stopPropagation();
          closePop();
          run(ev);
        } }, h("span", {}, label), small ? h("small", {}, small) : null));
      }
    }
    el.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp" && ev.key !== "Home" && ev.key !== "End") return;
      ev.preventDefault();
      const all = [...el.querySelectorAll(".fl-mi")];
      const i = all.indexOf(document.activeElement);
      const j = ev.key === "Home" ? 0 : ev.key === "End" ? all.length - 1 :
        (i + (ev.key === "ArrowDown" ? 1 : -1) + all.length) % all.length;
      all[j]?.focus();
    });
    pop = el;
    placePop(el, e);
    el.querySelector(".fl-mi")?.focus({ preventScroll: true });
    return el;
  }
  /**
   * A small form: fields [{name, label, kind (text|select|textarea|checkbox), value, options, help}]; `run(values,
   * where)` returns a promise of true (done: the form closes) or false (an error was shown: it stays).
   */
  function ask(e, { title, text, fields, submit = "OK", run, extra }) {
    noteOpener(e);
    closePop();
    card.hide();
    const controls = {};
    const form = h("form", { class: "fl-form", onpointerdown: (ev) => ev.stopPropagation() },
      h("div", { class: "fl-ft" }, title), text ? h("p", { class: "fl-dim" }, text) : null);
    for (const f of fields) {
      let c;
      if (f.kind === "select")
        c = h("select", { class: "layr-select" },
          f.options.map(([v, l]) => h("option", { value: v, selected: v === f.value }, l)));
      else if (f.kind === "textarea")
        c = h("textarea", { class: "layr-textarea", rows: 3, placeholder: f.placeholder || "" }, f.value || "");
      else if (f.kind === "checkbox") c = h("input", { type: "checkbox", checked: !!f.value });
      else
        c = h("input", { class: "layr-input", value: f.value ?? "", placeholder: f.placeholder || "",
          spellcheck: "false" });
      controls[f.name] = c;
      form.append(h("label", { class: "fl-fl" + (f.kind === "checkbox" ? " inline" : "") }, h("span", {}, f.label), c,
        f.help ? h("small", { class: "fl-dim" }, f.help) : null));
    }
    if (extra) form.append(extra);
    const msg = h("div", { class: "fl-bad fl-fmsg", role: "alert" });
    const ok = h("button", { class: "layr-button primary small", type: "submit" }, submit);
    form.append(msg, h("div", { class: "fl-fb" }, h("button", { class: "layr-button small", type: "button",
      onclick: () => closePop(true) }, "Cancel"), ok));
    form.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Escape") closePop(true);
      // Enter confirms from any field (a text field does it by itself; a list or a tick box doesn't)
      if (ev.key === "Enter" && !ev.shiftKey && ev.target.matches("select, input[type=checkbox]")) {
        ev.preventDefault();
        form.requestSubmit(ok);
      }
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const values = {};
      for (const [k, c] of Object.entries(controls)) values[k] = c.type === "checkbox" ? c.checked : c.value;
      ok.disabled = true;
      msg.textContent = "";
      form._msg = msg;
      const done = await run(values, form);
      ok.disabled = false;
      if (done) closePop();
    });
    form.setAttribute("role", "dialog");
    form.setAttribute("aria-label", typeof title === "string" ? title : "");
    pop = form;
    placePop(form, e);
    (form.querySelector("input:not([type=checkbox]), textarea, select") || form.querySelector("input, button"))
      ?.focus({ preventScroll: true });
    return form;
  }
  /** A message where the action was: inside an open form, or a bubble next to the element. */
  function bubble(where, text) {
    if (where && where._msg) {
      where._msg.textContent = text;
      return;
    }
    const form = where?.closest?.(".fl-form");
    if (form?._msg) {
      form._msg.textContent = text;
      return;
    }
    const el = h("div", { class: "fl-bubble", role: "alert", onclick: () => el.remove() }, text);
    const r = root.getBoundingClientRect();
    root.append(el);
    let x = r.width / 2 - el.offsetWidth / 2, y = 50;
    if (where?.getBoundingClientRect && where.isConnected) {
      const b = where.getBoundingClientRect();
      x = b.left - r.left;
      y = b.bottom - r.top + 6;
    }
    el.style.left = Math.max(6, Math.min(x, r.width - el.offsetWidth - 6)) + "px";
    el.style.top = Math.max(6, Math.min(y, r.height - el.offsetHeight - 6)) + "px";
    setTimeout(() => el.remove(), 9000);
  }
  root.addEventListener("pointerdown", (e) => {
    if (pop && !pop.contains(e.target)) closePop();
  });
  // Keys: Escape closes the newest thing open (a menu or form, the card, the inside panel, the trace, the selection);
  // Delete removes the selected box. They count when the flow has the focus, or nothing else has (a click on a box
  // leaves the focus on the page); never while typing in a field or in the code editor next to the flow.
  const typing = (t) => t?.closest?.("input, textarea, select, [contenteditable=true], .cm-editor");
  function onKey(e) {
    if (destroyed || !root.isConnected || e.defaultPrevented) return;
    const t = e.target;
    const ours = root.contains(t) || t === document.body || t === document.documentElement;
    if (!ours) return;
    if (e.key === "Escape") {
      if (pop) return closePop(true), e.preventDefault();
      if (typing(t) && !t.closest(".fl-side")) return;
      if (cardEl.classList.contains("open")) return card.hide(), e.preventDefault();
      if (side.classList.contains("open")) return closeSide(true), e.preventDefault();
      if (S.trace) return (S.trace = false), classes(), e.preventDefault();
      if (S.sel || S.multi.size) return (S.sel = null), S.multi.clear(), classes(), drawBar(), e.preventDefault();
      return;
    }
    // Delete only when the focus is in the flow (a click on a box puts it there), never from elsewhere on the page
    if (e.key === "Delete" && O.editable && root.contains(t) && !typing(t) && !pop) {
      const n = NODE[S.sel];
      if (!n || S.multi.size > 1) return;
      e.preventDefault();
      const el = VIEW?.items.get(n.id)?.el;
      edit([{ op: "remove_step", node: n.id }], el?.querySelector(".fl-nh") || el).catch(() => {});
    }
  }
  document.addEventListener("keydown", onKey);

  // ---------------------------------------------------------------- adding steps
  function fitsTakes(block, type) {
    const takes = block.takes?.[0];
    if (type === "file") return takes === "file";
    if (type === "table" || type === "flags") return takes === "table";
    return false;
  }
  function openAddMenu(e, src) {
    const type = src.type;
    const word = type === "file" ? "a raw file" : type === "table" || type === "flags" ? "a table" : "this";
    const items = [{ head: `Steps that take ${word} (${src.wire})` }];
    for (const b of BLOCK_LIST.filter((b) => fitsTakes(b, type)))
      items.push([b.title, (ev) => blockForm(ev, b, src), "", b.sentence]);
    const own = FUNCS.filter((f) => f.params.length);
    if (own.length && type !== "file") {
      items.push("-", { head: "This method's own functions" });
      const cols = src.from?.startsWith("in:") ? [] : columnsOf(src.from, src.wire);
      const scored = own.map((f) => ({ f, s: f.params.filter((p) => cols.includes(p) || p === src.wire).length }))
        .sort((a, b) => b.s - a.s).slice(0, 8);
      for (const { f } of scored) items.push([human(f.name), (ev) => ownStep(ev, f, src), "", f.sentence]);
    }
    items.push("-");
    items.push(["A new step of your own", (ev) => newOwnStep(ev, src), "", "Name it and say what it does; then write " +
      "it in the editor"]);
    if (O.claude)
      items.push(["Describe it, Claude writes it", (ev) => describeStep(ev, src), "",
        "Say in a sentence what it should do"]);
    const el = menu(e, items);
    el.classList.add("wide");
  }
  function argOf(src) {
    return src.from?.startsWith("in:") ? { input: src.wire } : { wire: src.wire };
  }
  function blockForm(e, b, src) {
    const cols = src.from?.startsWith("in:") ? [] : columnsOf(src.from, src.wire);
    const firstTable = b.fields.find((f) => f.kind === "table" || f.kind === "file");
    const tableWires = [];
    for (const n of G.nodes) for (const o of n.outs) if (["table", "flags"].includes(n.types?.[o])) tableWires.push(o);
    const fields = [], optional = [];
    for (const f of b.fields) {
      if (f === firstTable) continue;
      (f.optional && b.choose_one?.[0] !== f.name ? optional : fields).push(f);
    }
    const controls = {};
    const control = (f) => {
      let c;
      const dflt = f.default;
      if (f.kind === "column" && cols.length)
        c = h("select", { class: "layr-select" }, f.optional ? h("option", { value: "" }, "—") : null,
          cols.map((x) => h("option", { value: x }, x)));
      else if (f.kind === "columns" && cols.length) {
        c = h("div", { class: "fl-cols" },
          cols.map((x) => h("label", { class: "fl-colpick" }, h("input", { type: "checkbox", value: x }), x)));
        c._multi = true;
      } else if (f.kind === "table")
        c = h("select", { class: "layr-select" }, h("option", { value: "" }, "— pick a table —"),
          tableWires.filter((w) => w !== src.wire).map((w) => h("option", { value: w }, w)));
      else if (f.kind === "file") c = h("select", { class: "layr-select" }, (G.inputs || []).map((i) => h("option",
        { value: i.id }, i.id)));
      else if (f.kind === "boolean") c = h("input", { type: "checkbox", checked: !!dflt });
      else if (f.kind === "choice" && f.choices && !f.multiple)
        c = h("select", { class: "layr-select" }, f.choices.map((x) => h("option", { value: x.value, selected:
          x.value === dflt }, x.label)));
      else if (f.kind === "choice" && f.choices) {
        c = h("div", { class: "fl-cols" }, f.choices.map((x) => h("label", { class: "fl-colpick" }, h("input",
          { type: "checkbox", value: x.value, checked: (dflt || []).includes(x.value) }), x.label)));
        c._multi = true;
      } else if (f.kind === "formula")
        c = h("select", { class: "layr-select" }, FUNCS.map((x) => h("option", { value: x.name, selected:
          x.params.some((p) => cols.includes(p)) }, x.name)));
      else
        c = h("input", { class: "layr-input", value: dflt == null ? "" : Array.isArray(dflt) ? dflt.join(", ") : dflt,
        placeholder: f.kind === "range" ? "from, to" : f.kind === "list" || f.kind === "columns" ? "a, b, c" : "",
        spellcheck: "false" });
      controls[f.name] = { c, f };
      return h("label", { class: "fl-fl" + (f.kind === "boolean" ? " inline" : "") }, h("span", {}, f.label), c);
    };
    const extra = h("div", {}, fields.map(control), optional.length ? h("details", { class: "fl-more-fields" },
      h("summary", {}, "More choices"), optional.map(control)) : null);
    const valueOf = ({ c, f }) => {
      if (c._multi) {
        const v = [...c.querySelectorAll("input:checked")].map((x) => x.value);
        if (Array.isArray(f.default) && JSON.stringify(v) === JSON.stringify(f.default)) return null;
        return v.length ? { value: f.kind === "columns" && v.length === 1 && !f.multiple ? v[0] : v } : null;
      }
      if (c.type === "checkbox") return c.checked === !!f.default ? null : { value: c.checked };
      const t = c.value.trim();
      if (!t) return null;
      if (f.kind === "table") return { wire: t };
      if (f.kind === "file") return { input: t };
      if (f.kind === "formula") return { function: t };
      if (f.kind === "number") return isFinite(Number(t)) ? { value: Number(t) } : { code: t };
      if (f.kind === "list" || f.kind === "range" || f.kind === "columns") {
        const parts = t.split(",").map((x) => x.trim()).filter(Boolean)
          .map((x) => (isFinite(Number(x)) ? Number(x) : x));
        return { value: f.kind === "columns" && parts.length === 1 ? parts[0] : parts };
      }
      if (f.kind === "choice") return t === f.default ? null : { value: t };
      if (t === String(f.default ?? "")) return null;
      return { value: t };
    };
    ask(e, {
      title: b.title,
      text: b.sentence,
      fields: [{ name: "out", label: "Name of what it makes", value: "", placeholder: "chosen for you",
        help: "Letters, digits and _." }],
      extra,
      submit: "Add the step",
      run: (v, where) => {
        const args = {};
        if (firstTable) args[firstTable.name] = argOf(src);
        for (const [name, x] of Object.entries(controls)) {
          const val = valueOf(x);
          if (val) args[name] = val;
        }
        // what it can't do without is asked for here, in the form's words (not as the code's error)
        const missing = b.fields.filter((f) => !f.optional && f !== firstTable && !args[f.name]);
        if (missing.length) {
          bubble(where, `Fill in ${missing.map((f) => `“${f.label}”`).join(" and ")}` + (cols.length ||
            !missing.some((f) => f.kind === "column" || f.kind === "columns") ? "." : ": the table that comes in " +
            "isn't calculated yet, so type the column's name."));
          return Promise.resolve(false);
        }
        const one = b.choose_one || [];
        if (one.length && !one.some((name) => args[name])) {
          const labels = one.map((name) => b.fields.find((f) => f.name === name)?.label || name);
          bubble(where, `Fill in one of: ${labels.join(", ")} (the others are under More choices).`);
          return Promise.resolve(false);
        }
        const op = { op: "add_step", function: b.function, args };
        if (v.out.trim()) op.out = v.out.trim();
        return tryEdit([op], where);
      },
    });
  }
  function ownStep(e, f, src) {
    const args = { [f.params[0]]: argOf(src) };
    edit([{ op: "add_step", function: f.name, args }], e.target).catch(() => {});
  }
  function newOwnStep(e, src) {
    ask(e, {
      title: "A new step of your own",
      text: `It takes ${src.wire}; you write what it does in the editor.`,
      fields: [
        { name: "name", label: "Its name", value: "", placeholder: "e.g. relative_areas",
          help: "Letters, digits and _." },
        { name: "sentence", label: "What it does, in one sentence", kind: "textarea", value: "" },
      ],
      submit: "Make it",
      run: async (v, where) => {
        const name = v.name.trim();
        if (!name) {
          bubble(where, "Give it a name.");
          return false;
        }
        const param = /^[A-Za-z_]\w*$/.test(src.wire) ? src.wire : "table";
        const ok = await tryEdit([
          { op: "new_function", name, params: [param], sentence: v.sentence.trim() || undefined },
          { op: "add_step", function: name, args: { [param]: argOf(src) } },
        ], where);
        if (ok) O.onOpen?.({ what: "function", fn: name, line: null, created: true });
        return ok;
      },
    });
  }
  function describeStep(e, src) {
    const cols = src.from?.startsWith("in:") ? [] : columnsOf(src.from, src.wire);
    ask(e, {
      title: "Describe the step: Claude writes it",
      text: `It takes ${src.wire}${cols.length ? ` (columns: ${cols.slice(0, 12).join(", ")}` +
        `${cols.length > 12 ? ", …" : ""})` : ""}. Claude's code comes back as a draft for you to accept.`,
      fields: [{ name: "sentence", label: "What it should do", kind: "textarea", value: "",
        placeholder: "e.g. Keep only the samples and work out each one's percentage of the total area." }],
      submit: "Ask Claude",
      run: async (v, where) => {
        if (!v.sentence.trim()) {
          bubble(where, "Say what it should do.");
          return false;
        }
        O.onOpen?.({ what: "claude", wire: src.wire, from: src.from, type: src.type, columns: cols,
          sentence: v.sentence.trim() });
        return true;
      },
    });
  }
  function addRawFile(e) {
    // the kinds of file are the readers the toolbox and its add-ons announce (blocks › readers)
    if (!READERS.length) {
      bubble(e.currentTarget || e.target, "The toolbox's readers aren't known yet: try again in a moment.");
      loadBlocks(true);
      return;
    }
    const readerOf = (fn) => READERS.find((r) => r.function === fn) || READERS[0];
    ask(e, {
      title: "Add a raw file",
      text: "The file your instrument or spreadsheet gives you. A step that reads it is added with it.",
      fields: [
        { name: "kind", label: "Kind of file", kind: "select", value: READERS[0].function,
          options: READERS.map((r) => [r.function, r.label]) },
        { name: "name", label: "What it holds (optional)", value: "", placeholder: "e.g. plate reads", help: "A short " +
          "name: it becomes plate_reads_file in the code. Empty: the reader's usual name." },
      ],
      submit: "Add it",
      run: (v, where) => {
        const r = readerOf(v.kind);
        const slug = v.name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")
          .replace(/_file$/, "");
        const free = (n) => !G.nodes.some((x) => x.outs.includes(n)) && !(G.inputs || []).some((i) => i.id === n);
        const base = slug ? (/^\d/.test(slug) ? "f_" + slug : slug) + "_file" : r.param;
        let name = base;
        for (let i = 2; INPUT[name]; i++) name = base.replace(/_file$/, `_${i}_file`);
        const out = slug ? slug.replace(/^\d/, "t_$&") : r.out;
        const readerParam = BLOCKS[r.function]?.fields.find((f) => f.kind === "file")?.name || r.param;
        return tryEdit([{ op: "add_input", name, types: r.extensions, label: v.name.trim() || undefined },
          { op: "add_step", function: r.function, args: { [readerParam]: { input: name } },
            out: out && free(out) ? out : undefined }], where);
      },
    });
  }
  function addSetting(e) {
    ask(e, {
      title: "Add a setting",
      text: "A number or a choice the experiment can change. Connect it to a step's input afterwards.",
      fields: [
        { name: "label", label: "What it is", value: "", placeholder: "e.g. Lowest score kept" },
        { name: "name", label: "Name in the code", value: "", placeholder: "e.g. min_area",
          help: "Letters, digits and _." },
        { name: "value", label: "Its usual value", value: "", placeholder: "e.g. 5, or \"sample\"" },
        { name: "unit", label: "Unit (if any)", value: "" },
      ],
      submit: "Add it",
      run: (v, where) => {
        const name = v.name.trim() || v.label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
        const op = { op: "add_setting", name, label: v.label.trim() || undefined, unit: v.unit.trim() || undefined };
        const t = v.value.trim();
        if (t) {
          if (isFinite(Number(t))) op.default = Number(t);
          else if (/^(true|false|yes|no)$/i.test(t)) op.default = /^(true|yes)$/i.test(t);
          else op.default = t.replace(/^["']|["']$/g, "");
        }
        return tryEdit([op], where);
      },
    });
  }
  function groupSelected(e) {
    const ids = [...S.multi].filter((id) => NODE[id]);
    if (!ids.length) return;
    ask(e, {
      title: `Group ${plural(ids.length, "step")}`,
      text: ids.map(titleOf).join(", "),
      fields: [{ name: "title", label: "The group's name", value: "", placeholder: "e.g. Calibration" }],
      submit: "Group them",
      run: (v, where) => {
        if (!v.title.trim()) {
          bubble(where, "Give the group a name.");
          return Promise.resolve(false);
        }
        return tryEdit([{ op: "group", nodes: ids, title: v.title.trim() }], where);
      },
    });
  }

  // ---------------------------------------------------------------- connecting by dragging
  let connecting = null;
  function startConnect(e, from, wire, type) {
    if (e.button !== 0 || !e.target.closest("[data-drag]")) return;
    e.stopPropagation();
    e.preventDefault();
    const it = VIEW?.items.get(from);
    if (!it) return;
    const sx = e.clientX, sy = e.clientY;
    const x1 = it.x + it.w, y1 = it.y + (it.outs[wire] ?? it.out);
    const label = h("div", { class: "fl-draglabel" });
    const v = wireValue(from, wire);
    label.textContent = `${wire}${v !== undefined ? " · " + longShape(v) : ""}`;
    const temp = svgEl("path", { class: `fl-seg temp s-${dotOf(type)}` });
    connecting = { from, wire, type, moved: false };
    const portOk = (row) => {
      const n = NODE[row.dataset.node];
      const p = n?.ports.find((q) => q.name === row.dataset.port);
      if (!n || n.id === from || n.kind !== "call" || !p) return null;
      if (p.name.startsWith("*") || /\[/.test(p.name)) return null;
      const t = p.type;
      if (!t || type === "none" || type === "file") return true;
      if ((t === "table" || t === "flags") && (type === "table" || type === "flags")) return true;
      return t === type || (t === "data" && type !== "figure");
    };
    const move = (ev) => {
      if (!connecting.moved && Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) < 5) return;
      if (!connecting.moved) {
        connecting.moved = true;
        card.hide();
        root.classList.add("connecting");
        wires.append(temp);
        root.append(label);
        for (const row of world.querySelectorAll(".fl-port[data-node]")) {
          const ok = portOk(row);
          if (ok === null) continue;
          row.classList.add(ok ? "drop-ok" : "drop-misfit");
        }
      }
      const r = canvas.getBoundingClientRect();
      const x2 = (ev.clientX - r.left - S.tx) / S.zoom, y2 = (ev.clientY - r.top - S.ty) / S.zoom;
      const k = Math.max(26, Math.abs(x2 - x1) / 2);
      temp.setAttribute("d", `M${x1} ${y1}C${x1 + k} ${y1} ${x2 - k} ${y2} ${x2} ${y2}`);
      const rr = root.getBoundingClientRect();
      label.style.left = ev.clientX - rr.left + 14 + "px";
      label.style.top = ev.clientY - rr.top + 12 + "px";
      for (const row of world.querySelectorAll(".fl-port.hover")) row.classList.remove("hover");
      const over = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".fl-port[data-node]");
      if (over && (over.classList.contains("drop-ok") || over.classList.contains("drop-misfit"))) {
        over.classList.add("hover");
        label.classList.toggle("misfit", over.classList.contains("drop-misfit"));
      } else label.classList.remove("misfit");
    };
    const up = (ev) => {
      removeEventListener("pointermove", move);
      removeEventListener("pointerup", up);
      const was = connecting;
      connecting = null;
      temp.remove();
      label.remove();
      root.classList.remove("connecting");
      const over = document.elementFromPoint(ev.clientX, ev.clientY);
      for (const row of world.querySelectorAll(".drop-ok, .drop-misfit, .fl-port.hover"))
        row.classList.remove("drop-ok", "drop-misfit", "hover");
      if (!was.moved) {
        if (e.target.closest(".fl-add")) openAddMenu(ev, { from, wire, type });
        return;
      }
      const row = over?.closest(".fl-port[data-node]");
      if (row && portOk(row) !== null) {
        const spec = from.startsWith("in:") ? { input: wire } : { wire };
        edit([{ op: "connect", node: row.dataset.node, port: row.dataset.port, ...spec }], row).catch(() => {});
        return;
      }
      if (over && canvas.contains(over) && !over.closest(".fl-node")) openAddMenu(ev, { from, wire, type });
    };
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
  }

  // ---------------------------------------------------------------- loading
  async function load() {
    const mine = ++S.loadTicket;
    S.error = null;
    root.classList.add("loading", "settling"); // settling: until the picker is loaded too (it may draw again)
    const args = {};
    if (S.source != null && !S.sourceFetched) args.source = S.source;
    else if (S.method) {
      args.method = S.method;
      if (S.version != null) args.version = S.version;
    }
    if (S.experiment) args.experiment = S.experiment;
    if (S.run) args.run = S.run;
    let g;
    try {
      const [graph] = await Promise.all([read("flow_get", args), loadBlocks()]);
      g = graph;
    } catch (e) {
      if (mine !== S.loadTicket) return;
      root.classList.remove("loading", "settling");
      S.error = "This can't be drawn as a flow: " + cleanError(e);
      drawBar();
      if (!G) put(empty, h("div", { class: "fl-empty-in" }, h("b", {}, "No flow to show."), h("p", {}, cleanError(e)))),
      empty.classList.add("open");
      return;
    }
    if (mine !== S.loadTicket || destroyed) return;
    root.classList.remove("loading");
    G = g;
    if (!G.method.id) G.method.id = S.method || S.methodId || null;
    if (G.method.id) S.methodId = G.method.id;
    if (S.source != null) FUNCS = functionsOf(S.source);
    S.pending = {};
    rowsCache.clear();
    figures.clear();
    draw();
    if (S.side?.kind === "inside" && S.stack.length) {
      const top = S.stack[S.stack.length - 1];
      openInside(top.fn, { keep: true, call: null, node: NODE[S.side.node] || null });
    } else if (S.side?.kind === "data") openData(S.side.from, S.side.wire);
    loadPicker(mine).finally(() => mine === S.loadTicket && root.classList.remove("settling"));
    if (S.python) ensureSource().then(() => drawPython());
  }
  async function loadBlocks(again = false) {
    if (BLOCK_LIST.length && !again) return;
    try {
      const r = await call("blocks", {});
      BLOCK_LIST = r.blocks || [];
      BLOCKS = Object.fromEntries(BLOCK_LIST.map((b) => [b.function, b]));
      READERS = r.readers || [];
    } catch {
      /* the palette is optional for drawing */
    }
  }
  async function loadPicker(mine) {
    const methodId = G?.method?.id || S.method;
    try {
      if (O.picker !== "runs" && (EXPS === null || S.refreshExps)) {
        const all = await read("experiments", {});
        EXPS = methodId ? all.filter((e) => e.method === methodId) : [];
        S.refreshExps = false;
      }
      if (S.experiment) RUNS = await read("runs", { experiment: S.experiment });
      else RUNS = [];
    } catch {
      EXPS ??= [];
    }
    if (mine !== S.loadTicket || destroyed) return;
    drawBar();
    if (G?.quality?.length && !S.quality) draw();
  }

  // ---------------------------------------------------------------- size changes
  const ro = new ResizeObserver(() => {
    root.classList.toggle("narrow", root.clientWidth < 700);
    root.classList.toggle("short", root.clientHeight < 360);
    root.classList.toggle("tight", canvas.clientHeight < 440 || canvas.clientWidth < 560);
    if (G && S.drawLater && root.clientWidth > 0) draw();
    else if (G && !S.viewSet && canvas.clientWidth > 0) draw();
    else drawMini();
  });
  ro.observe(root);
  ro.observe(canvas);
  root.classList.toggle("narrow", container.clientWidth < 700);
  drawBar();
  load();

  // ---------------------------------------------------------------- the API
  return {
    /** Draw again with other code or data, keeping the view, the selection and the folds. */
    update(next = {}) {
      let changed = false;
      if ("source" in next && next.source !== undefined && next.source !== S.source) {
        S.source = next.source;
        S.sourceFetched = false;
        changed = true;
      }
      if ("method" in next && next.method !== undefined && next.method !== S.method) {
        S.method = next.method;
        changed = true;
      }
      if ("version" in next && next.version !== S.version) {
        S.version = next.version;
        changed = true;
      }
      if ("experiment" in next && next.experiment !== undefined && next.experiment !== S.experiment) {
        S.experiment = next.experiment;
        RUNS = null;
        changed = true;
      }
      if ("run" in next && next.run !== undefined && next.run !== S.run) {
        S.run = next.run;
        changed = true;
      }
      if (next.refresh || !changed) S.refreshExps = true;
      return load();
    },
    /** A run is in progress: light up the step being calculated (`{step}`: a function name or a node id). */
    setRunning(r) {
      S.running = r?.step || null;
      classes();
    },
    /**
     * The host's code can't be read now (`{line, message}`, or null when it can again): said in the flow's bar, and
     * the flow keeps the last drawing it could make.
     */
    setProblem(p) {
      if (JSON.stringify(p || null) === JSON.stringify(S.problem || null)) return;
      S.problem = p || null;
      drawNote();
    },
    /**
     * Where looking inside a box happens: "panel" (the flow's own side panel) or "host" (the host shows the box's
     * function itself, e.g. in its code editor; the side panel closes).
     */
    setInside(mode) {
      if (mode === O.inside) return;
      O.inside = mode;
      if (mode !== "panel" && side.classList.contains("open")) closeSide();
    },
    /** Select a box by its node id, or by a line of the method file. */
    select(x) {
      if (!G) return;
      let id = x;
      if (typeof x === "number") id = (G.nodes || []).find((n) => n.line <= x && x <= n.end_line)?.id;
      if (!id) return;
      selectBox(rep(id), false);
      centre(id);
    },
    /** For tests and the host: the graph as drawn. */
    get graph() {
      return G;
    },
    get source() {
      return S.source;
    },
    fit,
    destroy() {
      destroyed = true;
      ro.disconnect();
      destroyEditor();
      closePop();
      root.remove();
    },
  };
}

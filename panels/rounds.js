// Every round of a function's loops, as tables under its code: one row per round, one column per name the loop
// gives. Clicking a row makes the code show that round's values (the host draws them: `at(statement)` says what to
// show on a line). Rows come from the plugin's `loop_rounds` tool, 1,000 at a time, and only the rows in view are on
// the page, so a loop of 10,000 rounds stays quick. Used by the flow's "look inside" panel and beside the code editor.
//
//   import { createRounds } from "./rounds.js";
//   const R = createRounds({ call, args: { run }, f /* function_inside */, fmt, label, height, onChange });
//   if (R) box.append(R.el);          // null when the function has no `for` loop with values
//   R.at(statement)                   // what its line shows now (see below)
//
// Load flow.css with it (the .rt classes).
import { resizable } from "./resize.js";
import { splitUnit } from "./units.js";

const PAGE = 1000; // rounds per loop_rounds call
const ROW_H = 22; // px per row (the CSS gives the rows exactly this height)

const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many || one + "s"}`;
const cleanError = (e) =>
  String(e?.message || e || "Something went wrong")
    .replace(/^Error executing tool \w+:\s*/, "")
    .replace(/^LabError:\s*/, "");

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === "") continue;
    el.append(kid.nodeType ? kid : String(kid));
  }
  return el;
}

/**
 * A light value of one round (a number, a text, true/false, the size of a bigger value, or the content of a small
 * list or dict: its preview) as it reads in a cell.
 */
export function cellText(v, fmt = String) {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return fmt(v);
  if (typeof v !== "object") return String(v);
  if (typeof v.preview === "string") return v.preview;
  const cols = Array.isArray(v.columns) ? v.columns.length : v.columns;
  switch (v.kind) {
    case "table":
      return `table ${v.rows} × ${cols}`;
    case "list":
      return plural(v.length ?? 0, "item");
    case "dict":
      return plural(v.length ?? 0, "entry", "entries");
    case "number":
      return v.text ?? fmt(v.value);
    case "loop":
      return plural(v.rounds ?? 0, "round");
    default:
      return v.type || v.text || v.kind || "value";
  }
}

/** The size of a light value in words ("7 entries", "table 3 × 2"), also when it has a preview. */
export function sizeText(v, fmt = String) {
  return v && typeof v === "object" && typeof v.preview === "string" ? cellText({ ...v, preview: undefined }, fmt) : cellText(v, fmt);
}

/**
 * A light value's longer words, for a title: a preview with its size ("7 entries: respondent=17, q1=3, …"), a bigger
 * value's size and that only its size was kept for every round. Null for a plain value.
 */
export function cellTitle(v, fmt = String) {
  if (!v || typeof v !== "object" || v.kind === "number" || v.kind === "loop") return null;
  if (typeof v.preview === "string") return `${sizeText(v, fmt)}: ${v.preview}`;
  return `${cellText(v, fmt)} (only its size is kept for every round)`;
}

/** The dot colour of a light value: table, data, or none (numbers and texts have no dot). */
export function cellKind(v) {
  if (!v || typeof v !== "object") return "none";
  if (v.kind === "table") return "table";
  if (v.kind === "number" || v.kind === "loop") return "none";
  return "data";
}

/**
 * The round a line shows, in words, from `at`'s answer: {chip: "round 5 of 160", title: "… in round 2 of the loop
 * around it"} (null when it shows no round).
 */
export function roundWords(now) {
  if (!now?.n) return null;
  const chip = `round ${now.n.toLocaleString()}` + (now.of ? ` of ${now.of.toLocaleString()}` : "");
  return { chip, title: chip + (now.outer != null ? `, in round ${now.outer.toLocaleString()} of the loop around it` : "") };
}

/** A name with its unit (rate_mM_per_min → rate mM/min), as the flow labels columns. */
function nameEl(name) {
  const [words, unit] = splitUnit(name);
  return unit && words ? [words, " ", h("span", { class: "rt-unit" }, unit)] : name;
}

const copyText = (text) =>
  window.layr?.clipboard?.writeText ? window.layr.clipboard.writeText(text) : navigator.clipboard.writeText(text);

const csvCell = (x) => {
  const s = x == null ? "" : String(x);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/**
 * The rounds tables of one call of a function → {el, at(statement), reveal(line), destroy()}, or null when it has
 * no `for` loop with values (a `while` has no table: its lines keep their own rounds).
 * opts:
 *   call(tool, args) → promise: the plugin's tools.
 *   args: {run} (or {experiment}): which run; the call of `f` is added.
 *   f: function_inside's answer for that function and run.
 *   fmt(number) → text: the host's number format.
 *   height: {get, set}: where the tables' height is remembered (see resize.js).
 *   onChange(): another round was picked: the host draws the code's values again (with `at`).
 *
 * at(statement) says what a line of the code shows now:
 *   null: the line isn't in a `for` loop (the host shows its values as before);
 *   {kind: "full", round, k, n, of, outer?}: its whole values in that round (`round` is one of the statement's
 *     `rounds`); k counts the loop's rounds over the whole call, n of `of` as she counts them (in a loop inside a
 *     loop: round n of the `of` it made in round `outer` of the loop around it; see roundWords);
 *   {kind: "light", values: {name: light value}, k, n, of, outer?}: the values from the table (a table's size only, a small
 *     list's or dict's preview);
 *   {kind: "inner", rounds, outer, open()}: a loop inside the shown round: how many rounds it made there;
 *   {kind: "none", k?, n?, of?}: the line didn't run in the round shown.
 */
export function createRounds(opts) {
  const { call, f } = opts;
  const fmt = opts.fmt || String;
  const stmts = f?.statements || [];
  const heads = new Map(stmts.filter((s) => s.loop).map((s) => [s.line, s]));
  if (!heads.size) return null;
  const args = { ...opts.args, function: f.function, ...(f.call?.id != null ? { call: f.call.id } : {}) };
  const codeLines = String(f.code || "").split("\n");
  const codeOf = (line) => (codeLines[line - (f.line || 1)] || "").trim().replace(/:$/, "");
  const parentOf = (line) => heads.get(line)?.loop_line ?? null;
  const depth = (line) => (parentOf(line) == null ? 0 : 1 + depth(parentOf(line)));
  const lead = (m) => (m?.outer ? 2 : 1); // the round columns before the values: [round] or [outer round, round]

  // ---------------------------------------------------------------- each loop's rows, a page at a time
  const data = new Map();
  function loopData(line) {
    let d = data.get(line);
    if (d) return d;
    d = { meta: null, error: null, pages: new Map(), wait: new Map() };
    d.page = (p) => {
      if (d.pages.has(p)) return Promise.resolve(d.pages.get(p));
      if (!d.wait.has(p))
        d.wait.set(
          p,
          call("loop_rounds", { ...args, line, offset: p * PAGE, limit: PAGE }).then(
            (r) => {
              d.meta ||= r;
              d.pages.set(p, r.rows || []);
              d.wait.delete(p);
              return r.rows || [];
            },
            (e) => {
              d.wait.delete(p);
              d.error = cleanError(e);
              throw e;
            },
          ),
        );
      return d.wait.get(p);
    };
    d.first = () => (d.meta ? Promise.resolve(d.meta) : d.page(0).then(() => d.meta));
    d.row = (a) => d.pages.get(Math.floor(a / PAGE))?.[a % PAGE]; // a: rows to skip (round a + 1)
    d.rows = async (from, to) => {
      // rows from..to-1 are on hand afterwards
      const want = [];
      for (let p = Math.floor(from / PAGE); p <= Math.floor(Math.max(from, to - 1) / PAGE); p++) want.push(d.page(p));
      await Promise.all(want);
    };
    d.rowAt = async (a) => (await d.rows(a, a + 1), d.row(a));
    data.set(line, d);
    return d;
  }

  // ---------------------------------------------------------------- the round each loop shows
  const picks = new Map(); // loop line → {k (its round, counted over the whole call), row (from its table)}
  /** A loop inside a loop shows a round of the outer round shown, or nothing: the others are dropped. */
  function prune() {
    for (let again = true; again; ) {
      again = false;
      for (const [line, p] of picks) {
        const outer = parentOf(line);
        if (outer == null) continue;
        const po = picks.get(outer);
        if (!po || !p.row || p.row[0] !== po.k) {
          picks.delete(line);
          again = true;
        }
      }
    }
  }
  /** Round k of a loop is shown in the code (and the round of each loop around it that holds it). */
  async function pick(line, k, { scroll = false } = {}) {
    const mine = (pick.ticket = (pick.ticket || 0) + 1);
    let row = null;
    try {
      row = await loopData(line).rowAt(k - 1);
    } catch {
      /* said in its table */
    }
    if (mine !== pick.ticket) return;
    picks.set(line, { k, row: row || null });
    // the loops around it show the round that holds it
    let cur = line;
    let curRow = row;
    while (parentOf(cur) != null && curRow && loopData(cur).meta?.outer) {
      const outer = parentOf(cur);
      const o = curRow[0];
      let orow = picks.get(outer)?.k === o ? picks.get(outer).row : null;
      if (!orow) {
        try {
          orow = await loopData(outer).rowAt(o - 1);
        } catch {
          orow = null;
        }
        if (mine !== pick.ticket) return;
        picks.set(outer, { k: o, row: orow || null });
        views.get(outer)?.mark(true);
      }
      cur = outer;
      curRow = orow;
    }
    prune();
    for (const v of views.values()) v.mark(scroll && v.line === line);
    opts.onChange?.();
  }

  /** What a statement's line shows now (see createRounds). */
  function at(st) {
    const line = st.loop ? st.line : st.loop_line;
    const head = line == null ? null : heads.get(line);
    if (!head) return null;
    const p = picks.get(line);
    if (!p) {
      const outer = st.loop ? st.loop_line : null;
      const po = outer != null ? picks.get(outer) : null;
      const m = outer != null ? data.get(outer)?.meta : null;
      if (st.loop && po?.row && m) {
        const i = m.columns.findIndex((c) => c.kind === "loop" && c.line === st.line);
        const cell = i >= 0 ? po.row[i + lead(m)] : null;
        return { kind: "inner", rounds: cell?.rounds || 0, outer: po.k, open: () => openInner(st.line, po.k) };
      }
      return { kind: "none" };
    }
    const shown = { k: p.k, ...roundOf(line, p) };
    if (st === head || st.n === head.n) {
      // it ran once in every round: its own count is the loop's round, so a kept round has its whole values
      const r = (st.rounds || []).find((x) => x.round === p.k);
      if (r) return { kind: "full", round: r, ...shown };
    }
    const m = data.get(line)?.meta;
    if (!p.row || !m) return { kind: "none", ...shown };
    const values = {};
    m.columns.forEach((c, i) => {
      if (c.line !== st.line || c.kind === "loop") return;
      const v = p.row[i + lead(m)];
      if (v !== null && v !== undefined) values[c.name] = v;
    });
    return Object.keys(values).length ? { kind: "light", values, ...shown } : { kind: "none", ...shown };
  }
  /** The round shown as she counts it: {n: round n, of: rounds, outer?: the round of the loop around it}. */
  function roundOf(line, p) {
    const m = data.get(line)?.meta;
    const outer = parentOf(line);
    if (outer == null || !m?.outer || !p.row) return { n: p.k, of: heads.get(line).n };
    const po = picks.get(outer);
    const om = data.get(outer)?.meta;
    const i = om ? om.columns.findIndex((c) => c.kind === "loop" && c.line === line) : -1;
    return { n: p.row[1], of: (i >= 0 && po?.row?.[i + lead(om)]?.rounds) || null, outer: p.row[0] };
  }

  // ---------------------------------------------------------------- the tables
  const views = new Map();
  const handles = [];
  const el = h("div", { class: "rt-all" });
  const order = [...heads.keys()].sort((a, b) => a - b);
  // outer loops first in the order of the code, each followed by the loops inside it
  const tree = (line) => [line, ...order.filter((l) => parentOf(l) === line).flatMap(tree)];
  const shown = order.filter((l) => parentOf(l) == null || !heads.has(parentOf(l))).flatMap(tree);
  el.append(
    h(
      "p",
      { class: "rt-help" },
      shown.length > 1 ? "Every round of the loops: " : "Every round of the loop: ",
      "one row per round, one column per name it gives. Click a row to see that round in the code.",
    ),
  );
  for (const line of shown) {
    const v = loopView(line);
    views.set(line, v);
    el.append(v.sec);
  }

  function loopView(line) {
    const head = heads.get(line);
    const inner = parentOf(line) != null;
    const d = loopData(line);
    const v = { line, open: !inner, filter: null, locked: false };
    const fold = h("button", { class: "rt-fold", "aria-expanded": String(v.open), title: "Show or hide its rounds" });
    const copyBtn = h("button", { class: "layr-button small ghost rt-copy", title: "Copy the rounds as CSV (for Excel)" },
      "Copy as CSV");
    const count = h("span", { class: "rt-count" });
    const notes = h("div", { class: "rt-notes" });
    const thead = h("thead");
    const tbody = h("tbody");
    const colgroup = h("colgroup");
    const table = h("table", { class: "rt-table" }, colgroup, thead, tbody);
    const scroll = h("div", { class: "rt-scroll", tabindex: "0", "aria-label": `The rounds of ${codeOf(line)}` }, table);
    const foot = h("div", { class: "rt-foot" });
    const body = h("div", { class: "rt-body" }, notes, scroll, foot);
    const sec = h(
      "section",
      { class: "rt" + (inner ? " inner" : ""), style: inner ? `--rt-depth:${depth(line)}` : null },
      h(
        "div",
        { class: "rt-head" },
        fold,
        h("code", { class: "rt-code", title: codeOf(line) }, codeOf(line) || `the loop at line ${line}`),
        count,
        h("span", { class: "rt-space" }),
        copyBtn,
      ),
      body,
    );
    v.sec = sec;
    const rz = resizable(scroll, {
      edge: "bottom",
      min: ROW_H * 4 + 26,
      max: () => Math.max(200, innerHeight - 120),
      size: opts.height,
      apply: (px) => {
        scroll.style.height = px == null ? "" : `${px}px`;
        scroll.style.maxHeight = px == null ? "" : "none";
        draw();
      },
      title: "Drag to make the table taller or shorter · double-click for the usual height",
    });
    handles.push(rz);

    const base = () => (v.filter ? v.filter.base : 0);
    const total = () => (v.filter ? v.filter.count : d.meta ? d.meta.kept : 0);
    const roundOf = (row) => (d.meta?.outer ? `${row[0]} · ${row[1]}` : String(row[0]));
    const failedAt = () => {
      const fl = d.meta?.failed;
      if (!fl) return null;
      return d.meta.outer ? (row) => row[0] === fl.outer_round && row[1] === fl.round : (row) => row[0] === fl.round;
    };

    function setOpen(on) {
      v.open = on;
      fold.setAttribute("aria-expanded", String(on));
      body.hidden = !on;
      if (on) start();
    }
    fold.addEventListener("click", () => setOpen(!v.open));
    sec.querySelector(".rt-code").addEventListener("click", () => setOpen(!v.open));

    function countText() {
      const n = head.n || 0;
      const kept = d.meta?.kept;
      if (inner) return `${plural(n, "round")} in all`;
      return kept != null && kept < (d.meta?.total ?? n) ? `${plural(d.meta.total, "round")} · the first ` +
        `${kept.toLocaleString()} kept` : plural(n, "round");
    }
    count.textContent = countText();

    function head1() {
      const m = d.meta;
      const ths = [];
      const cols = [];
      if (m.outer) {
        ths.push(h("th", { class: "rt-k", title: `The round of the loop around it (${m.outer.code.replace(/:$/, "")})` },
          "Outer round"));
        cols.push(h("col", { class: "rt-ck" }));
      }
      ths.push(h("th", { class: "rt-k", title: "The loop's round: 1 is the first time through it" }, "Round"));
      cols.push(h("col", { class: "rt-ck" }));
      for (const c of m.columns) {
        const loop = c.kind === "loop";
        ths.push(
          h(
            "th",
            {
              class: (c.kind === "number" || loop ? "num" : "") + (loop ? " loop" : ""),
              title: loop
                ? `A loop inside this one (${c.name}): how many rounds it made in each round. Click a number to see them.`
                : `${c.name} · line ${c.line}` + (c.cut ? " · kept only for the first rounds" : ""),
            },
            loop ? ["↻ ", h("span", { class: "rt-mono" }, c.name)] : nameEl(c.name),
            c.cut ? h("span", { class: "rt-cutmark" }, " *") : null,
          ),
        );
        cols.push(h("col"));
      }
      thead.replaceChildren(h("tr", {}, ths));
      colgroup.replaceChildren(...cols);
      table.style.tableLayout = "";
      table.style.width = "";
    }

    function drawNotes() {
      const m = d.meta;
      const parts = [];
      if (d.error && !m) parts.push(h("div", { class: "rt-note bad" }, d.error));
      if (m?.note) parts.push(h("div", { class: "rt-note" }, m.note));
      if (m?.cut && m.why) parts.push(h("div", { class: "rt-note warn" }, m.why));
      if (m?.failed) {
        const fl = m.failed;
        const where = m.outer ? `round ${fl.round} (in outer round ${fl.outer_round})` : `round ${fl.round}`;
        parts.push(
          h(
            "div",
            { class: "rt-note bad" },
            `The step stopped in ${where}, line ${fl.line}: `,
            String(fl.error || "an error").replace(/^[A-Z]\w*(Error|Exception):\s*/, ""),
            " ",
            h("a", { class: "rt-link", onclick: () => showFailed() }, "Show that round"),
          ),
        );
      }
      if (v.filter) {
        const o = m?.outer;
        parts.push(
          h(
            "div",
            { class: "rt-filter" },
            `Only the ${plural(v.filter.count, "round")} it made in round ${v.filter.outer} of `,
            h("code", {}, o ? o.code.replace(/:$/, "") : "the loop around it"),
            " ",
            h("a", { class: "rt-link", onclick: () => setFilter(null) }, "Show every round"),
          ),
        );
      }
      notes.replaceChildren(...parts);
    }

    async function showFailed() {
      const fl = d.meta?.failed;
      if (!fl) return;
      if (!d.meta.outer) return pick(line, fl.round, { scroll: true });
      // an inner loop: the round counted over the whole call
      const test = failedAt();
      for (let a = 0; a < d.meta.kept; a += PAGE) {
        await d.rows(a, Math.min(d.meta.kept, a + PAGE));
        for (let i = a; i < Math.min(d.meta.kept, a + PAGE); i++) if (test(d.row(i))) return pick(line, i + 1, { scroll: true });
      }
    }

    function setFilter(fl) {
      v.filter = fl;
      scroll.scrollTop = 0;
      drawNotes();
      draw();
      mark(true);
    }
    v.setFilter = setFilter;

    // only the rows in view are on the page: one empty row above them and one below hold the scroll's height
    let frame = 0;
    function draw() {
      if (!d.meta || !v.open) return;
      const n = total();
      const top = scroll.scrollTop;
      const hgt = scroll.clientHeight || 260;
      const first = Math.max(0, Math.floor(top / ROW_H) - 12);
      const last = Math.min(n, Math.ceil((top + hgt) / ROW_H) + 12);
      const missing = [];
      for (let a = base() + first; a < base() + last; a += 1) {
        if (!d.pages.has(Math.floor(a / PAGE))) {
          missing.push(a);
          a = (Math.floor(a / PAGE) + 1) * PAGE - 1;
        }
      }
      if (missing.length) d.rows(missing[0], base() + last).then(() => draw(), () => drawNotes());
      const m = d.meta;
      const ncol = m.columns.length + lead(m);
      const failed = failedAt();
      const p = picks.get(line);
      const trs = [];
      for (let i = first; i < last; i++) {
        const a = base() + i;
        const row = d.row(a);
        if (!row) {
          trs.push(h("tr", { class: "rt-wait" }, h("td", { colspan: ncol }, "…")));
          continue;
        }
        const bad = failed?.(row);
        const tds = [];
        for (let j = 0; j < lead(m); j++)
          tds.push(h("td", { class: "rt-k" + (j === lead(m) - 1 ? " last" : "") }, j === lead(m) - 1 && bad ? "⚠ " : "",
            String(row[j])));
        m.columns.forEach((c, ci) => {
          const x = row[ci + lead(m)];
          if (c.kind === "loop") {
            const rounds = x?.rounds || 0;
            tds.push(h("td", { class: "num" }, rounds
              ? h("button", { class: "rt-inner", "data-line": c.line, "data-outer": row[lead(m) - 1],
                title: `See the ${plural(rounds, "round")} of ${c.name} in this round` }, plural(rounds, "round"), " ›")
              : h("span", { class: "rt-blank" }, "0 rounds")));
            return;
          }
          if (x === null || x === undefined) {
            tds.push(h("td", { class: "rt-none", title: bad ? "Not reached: the step stopped in this round" :
              "This line didn't run in this round" }, ""));
            return;
          }
          const isNum = typeof x === "number" || x?.kind === "number";
          const preview = typeof x?.preview === "string";
          const shape = typeof x === "object" && !isNum && !preview;
          tds.push(h("td", { class: isNum ? "num" : shape ? "shape" : preview ? "preview" : "",
            title: shape || preview ? cellTitle(x, fmt) : typeof x === "string" && x.length > 18 ? x : null },
          cellText(x, fmt)));
        });
        trs.push(h("tr", { "data-k": a + 1, class: (p?.k === a + 1 ? "on" : "") + (bad ? " failed" : ""),
          title: bad ? `The step stopped in this round: ${m.failed.error || ""}` : null }, tds));
      }
      const pad = (px) => h("tr", { class: "rt-pad", "aria-hidden": "true" }, h("td", { colspan: ncol,
        style: `height:${px}px` }));
      tbody.replaceChildren(pad(first * ROW_H), ...trs, pad((n - last) * ROW_H));
      // the columns keep the widths of the first rows drawn: scrolling doesn't make them jump
      if (!v.locked && trs.some((tr) => tr.dataset.k)) {
        v.locked = true;
        const widths = [...thead.querySelectorAll("th")].map((th) => Math.ceil(th.getBoundingClientRect().width));
        if (widths.every((w) => w > 0)) {
          [...colgroup.children].forEach((col, i) => (col.style.width = `${Math.min(260, widths[i])}px`));
          table.style.tableLayout = "fixed";
          table.style.width = `${widths.reduce((s, w) => s + Math.min(260, w), 0)}px`;
        } else v.locked = false;
      }
      drawFoot();
    }
    scroll.addEventListener("scroll", () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(draw);
    });

    function drawFoot() {
      const p = picks.get(line);
      const m = d.meta;
      if (!m) return foot.replaceChildren();
      const n = total();
      const shownText = !p
        ? inner ? "Pick a round of the loop above (or click a number of rounds there) to see one of these in the code."
          : ""
        : p.row && m.outer
          ? `The code shows round ${p.row[1]} of this loop, in round ${p.row[0]} of the loop around it (the shaded row).`
          : `The code shows round ${p.k.toLocaleString()} (the shaded row).`;
      foot.replaceChildren(h("span", {}, shownText));
      if (n > 40)
        foot.append(h("span", { class: "rt-dim" }, `${plural(n, "row")}${d.meta.kept < d.meta.total && !v.filter ?
          ` of ${d.meta.total.toLocaleString()} rounds` : ""}`));
    }

    /** The picked row is shaded (and scrolled into view when the pick came from elsewhere). */
    function mark(scrollTo = false) {
      if (!d.meta) return;
      const p = picks.get(line);
      for (const tr of tbody.querySelectorAll("tr[data-k]")) tr.classList.toggle("on", p?.k === Number(tr.dataset.k));
      if (scrollTo && p && v.open) {
        const i = p.k - 1 - base();
        if (i >= 0 && i < total()) {
          const y = i * ROW_H;
          const head = thead.offsetHeight || ROW_H;
          if (y < scroll.scrollTop || y + ROW_H > scroll.scrollTop + scroll.clientHeight - head)
            scroll.scrollTop = Math.max(0, y - (scroll.clientHeight - head) / 2);
        }
      }
      draw();
    }
    v.mark = mark;

    tbody.addEventListener("click", (e) => {
      const btn = e.target.closest(".rt-inner");
      if (btn) {
        e.stopPropagation();
        return openInner(Number(btn.dataset.line), Number(btn.dataset.outer));
      }
      const tr = e.target.closest("tr[data-k]");
      if (tr) pick(line, Number(tr.dataset.k));
    });
    // the keyboard: up and down pick the round before or after
    scroll.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      const p = picks.get(line);
      const lo = base() + 1;
      const hi = base() + total();
      const k = p ? Math.min(hi, Math.max(lo, p.k + (e.key === "ArrowDown" ? 1 : -1))) : lo;
      pick(line, k, { scroll: true });
    });

    copyBtn.addEventListener("click", async () => {
      try {
        const m = await d.first();
        const lo = base();
        const n = total();
        await d.rows(lo, lo + n);
        const headRow = [...(m.outer ? ["outer round"] : []), "round", ...m.columns.map((c) => c.name)];
        const lines = [headRow.map(csvCell).join(",")];
        for (let a = lo; a < lo + n; a++) {
          const row = d.row(a) || [];
          lines.push(row.map((x, i) => csvCell(i < lead(m) || typeof x === "number" ? x : cellText(x, String))).join(","));
        }
        await copyText(lines.join("\r\n"));
        copyBtn.textContent = `Copied ${plural(n, "round")}`;
      } catch (e) {
        copyBtn.textContent = "Couldn't copy";
        copyBtn.title = cleanError(e);
      }
      setTimeout(() => (copyBtn.textContent = "Copy as CSV"), 1800);
    });

    let started = false;
    function start() {
      if (started) return;
      started = true;
      d.first().then(
        () => {
          head1();
          count.textContent = countText();
          drawNotes();
          draw();
          mark(true);
        },
        () => {
          started = false;
          drawNotes();
        },
      );
    }
    body.hidden = !v.open;
    if (v.open) start();
    return v;
  }

  /** A number of rounds clicked in a loop's table: the loop inside it opens with only the rounds of that round. */
  async function openInner(innerLine, outerRound) {
    const v = views.get(innerLine);
    const outer = parentOf(innerLine);
    if (!v || outer == null) return;
    const od = loopData(outer);
    try {
      const m = await od.first();
      const ci = m.columns.findIndex((c) => c.kind === "loop" && c.line === innerLine);
      await od.rows(0, outerRound);
      // the inner loop's rows are in the order they ran: the rounds of the earlier outer rounds come first
      let base = 0;
      for (let a = 0; a < outerRound - 1; a++) base += od.row(a)?.[ci + lead(m)]?.rounds || 0;
      const count = od.row(outerRound - 1)?.[ci + lead(m)]?.rounds || 0;
      if (!v.open) v.sec.querySelector(".rt-fold").click();
      await loopData(innerLine).first();
      v.setFilter({ outer: outerRound, base, count });
      v.sec.scrollIntoView({ block: "nearest", behavior: "smooth" });
      if (count) await pick(innerLine, base + 1);
    } catch {
      /* said in its table */
    }
  }

  // ---------------------------------------------------------------- the rounds shown at first
  // where the step stopped, else the first round of each loop
  const failed = stmts.find((s) => s.failed_at && heads.has(s.loop_line));
  for (const line of shown) if (parentOf(line) == null) picks.set(line, { k: 1, row: null });
  if (failed) pick(failed.loop_line, failed.failed_at, { scroll: true });
  for (const line of shown)
    if (parentOf(line) == null && !(failed && (failed.loop_line === line || isInside(failed.loop_line, line))))
      loopData(line).rowAt(0).then((row) => {
        const p = picks.get(line);
        if (p && p.k === 1 && !p.row) {
          p.row = row || null;
          opts.onChange?.();
        }
      }, () => {});
  function isInside(line, outer) {
    for (let l = parentOf(line); l != null; l = parentOf(l)) if (l === outer) return true;
    return false;
  }

  return {
    el,
    at,
    /** How many loops have a table. */
    count: views.size,
    /** Brings a loop's table into view (a click on the round chip of its line in the code). */
    reveal(line) {
      const v = views.get(line);
      if (!v) return;
      if (!v.open) v.sec.querySelector(".rt-fold").click();
      v.sec.scrollIntoView({ block: "nearest", behavior: "smooth" });
      v.sec.querySelector(".rt-scroll")?.focus({ preventScroll: true });
    },
    /** Shows round k of the loop at `line` (for the host and tests). */
    pick: (line, k) => pick(line, k, { scroll: true }),
    /** The round each loop shows: {line: k}. */
    picked: () => Object.fromEntries([...picks].map(([l, p]) => [l, p.k])),
    destroy() {
      for (const r of handles) r.destroy();
    },
  };
}

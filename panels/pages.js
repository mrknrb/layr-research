// An experiment's own pages, made of cards, as its method declares them (PAGES in the method file, lab/pages.py).
// Shown as tabs between the flow and Results; "Edit page" turns a page into a small builder (add, change, move and
// remove cards) whose Save writes PAGES back into the method with method_declare, as a new version of the method.
//
// Cards: text, number, table, figure, chart (drawn here from a table) and review (a curve per item with regions whose
// boundaries can be dragged and saved as a correction: the GC peak review is one, declared by the GC method).
// lab.js gives the helpers it shares (createPages(ctx)).

const CARD_KINDS = [
  ["text", "Text", "Your own words, or a text the method returns (a model's report)"],
  ["number", "Number", "One value the method returns, or a column's mean, sum, count…"],
  ["table", "Table", "A table the method returns, or one step's table, with filters"],
  ["chart", "Chart", "Lines, points or bars drawn from a table's columns"],
  ["figure", "Figure", "A figure the method draws (matplotlib)"],
  ["review", "Review a curve", "One curve per item with regions on it; drag a region's ends to correct it"],
];
const SUMMARIES = [
  ["mean", "Mean"],
  ["median", "Median"],
  ["sum", "Sum"],
  ["count", "Count (filled cells)"],
  ["min", "Lowest"],
  ["max", "Highest"],
  ["first", "First row"],
  ["last", "Last row"],
];
const STATUS_CLASS = { ok: "ok", "<LOQ": "loq", "n.d.": "nd", manual: "manual", excluded: "excl" };
const PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1", "#76b7b2", "#edc948", "#9c755f"];

export function createPages(ctx) {
  const { h, put, call, fmt, table, badge, openDialog, askReason, fail, go, S, save, startRun, figureOf, human, kv } =
    ctx;
  let PE = null; // the page being edited: {exp, index, page (a copy), isNew}

  // ------------------------------------------------------------------------------------------ helpers

  const clone = (x) => JSON.parse(JSON.stringify(x));
  const num = (v) => (v === null || v === undefined || v === "" || !Number.isFinite(+v) ? null : +v);
  const uniq = (xs) => [...new Set(xs.filter((x) => x != null && x !== ""))];
  const svgNS = "http://www.w3.org/2000/svg";
  const s = (tag, attrs) => {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  };
  const same = (a, b) => a != null && b != null && String(a) === String(b);

  /** The tables and values a run has: {tables: {name: columns}, figures: [], values: [], texts: []}. */
  function sourcesOf(run) {
    const out = { tables: {}, figures: [], values: [], texts: [], labels: {} };
    for (const [name, o] of Object.entries(run?.outputs || {})) {
      out.labels[name] = o.label || human(name);
      if (o.kind === "table") out.tables[name] = o.columns || [];
      else if (o.kind === "figure") out.figures.push(name);
      else if (o.kind === "text") out.texts.push(name);
      else if (o.kind === "value") out.values.push(name);
    }
    for (const st of run?.steps || [])
      if (st.table && st.function) {
        out.tables[`step:${st.function}`] = st.table.columns || [];
        out.labels[`step:${st.function}`] = `Step: ${human(st.function)}`;
      }
    return out;
  }

  /** Every row of a table (pages of 2000), only the columns asked for (those it has). */
  async function allRows(args, tableName, columns, have) {
    const cols = columns ? uniq(columns).filter((c) => !have || have.includes(c)) : undefined;
    const rows = [];
    for (let offset = 0; offset < 100000; offset += 2000) {
      const got = await call("result_table", { ...args, table: tableName, columns: cols, limit: 2000, offset });
      rows.push(...got.rows);
      if (rows.length >= got.total || !got.rows.length) return { rows, total: got.total, columns: got.columns };
    }
    return { rows, total: rows.length, columns: cols || [] };
  }

  function summarize(values, how) {
    const xs = values.filter((v) => v !== null && v !== undefined && v !== "");
    const ns = xs.map(Number).filter(Number.isFinite);
    if (how === "count") return xs.length;
    if (how === "first") return xs[0];
    if (how === "last") return xs[xs.length - 1];
    if (!ns.length) return null;
    if (how === "sum") return ns.reduce((a, b) => a + b, 0);
    if (how === "min") return Math.min(...ns);
    if (how === "max") return Math.max(...ns);
    if (how === "median") {
      const o = [...ns].sort((a, b) => a - b);
      const m = o.length >> 1;
      return o.length % 2 ? o[m] : (o[m - 1] + o[m]) / 2;
    }
    return ns.reduce((a, b) => a + b, 0) / ns.length;
  }

  /** A value as run_get gives it: a number, a text, or {kind, ...} for NaN, lists and dicts. */
  function valueText(v) {
    if (v && typeof v === "object") {
      if (v.kind === "number") return v.text ?? "–";
      if (v.kind === "list") return (v.items || []).map(valueText).join(", ");
      if (v.kind === "dict")
        return Object.entries(v.items || {})
          .map(([k, x]) => `${k}: ${valueText(x)}`)
          .join(" · ");
      if (v.kind === "record")
        return Object.entries(v.fields || {})
          .map(([k, x]) => `${k}: ${valueText(x)}`)
          .join(" · ");
      if (v.kind === "column" || v.kind === "array") return (v.values || []).slice(0, 12).map(fmt).join(", ");
      return JSON.stringify(v);
    }
    return fmt(v);
  }

  function textBlock(text) {
    return String(text || "")
      .split(/\n\s*\n/)
      .map((p) =>
        p.startsWith("# ") ? h("h4", {}, p.slice(2)) : h("p", {}, p.split("\n").flatMap((l, i) => (i ? [h("br"), l] : [l]))),
      );
  }

  // ------------------------------------------------------------------------------------------ tabs

  /** The page tabs for the experiment's screen: [{id, label}] and whether a page can be added. */
  function tabs(exp) {
    return (exp.method_pages || []).map((p, i) => ({ id: `page:${i}`, label: p.title }));
  }

  /** The tab to open for an old link (the Peaks tab of 0.4): the first page with a review card. */
  function reviewTab(exp) {
    const i = (exp.method_pages || []).findIndex((p) => (p.cards || []).some((c) => c.card === "review"));
    return i >= 0 ? `page:${i}` : null;
  }

  /** The review card that shows rows about `column` (e.g. injection, respondent), to open from Checks and
   * Corrections: {tab, card} or null. */
  function reviewFor(exp, columns) {
    const pages = exp.method_pages || [];
    for (let i = 0; i < pages.length; i++)
      for (const c of pages[i].cards || [])
        if (c.card === "review" && columns.includes(c.item)) return { tab: `page:${i}`, card: c };
    return null;
  }

  /** The page whose cards show the table `name` (for a box of the flow): {tab, title} or null. */
  function pageShowing(exp, names) {
    const pages = exp.method_pages || [];
    for (let i = 0; i < pages.length; i++)
      if ((pages[i].cards || []).some((c) => names.includes(c.table) || names.includes(c.regions)))
        return { tab: `page:${i}`, title: pages[i].title };
    return null;
  }

  async function addPage(exp) {
    const name = h("input", { class: "layr-input", placeholder: "e.g. Overview" });
    setTimeout(() => name.focus(), 30);
    const title = await openDialog({
      title: "New page",
      body: [
        h(
          "p",
          { class: "layr-muted" },
          "A page of this method's experiments, made of cards: numbers, tables, charts, texts, curves to review. " +
            "It is kept in the method, so every experiment of the method has it.",
        ),
        h("label", { class: "layr-label" }, "Its title"),
        name,
      ],
      buttons: [
        { label: "Cancel", id: null, kind: "ghost" },
        { label: "Make it", kind: "primary", value: () => name.value.trim() || undefined },
      ],
    });
    if (!title) return;
    const index = (exp.method_pages || []).length;
    PE = { exp: exp.id, index, page: { title, cards: [] }, isNew: true };
    go({ tab: `page:${index}` });
  }

  // ------------------------------------------------------------------------------------------ a page

  async function renderPage(body, exp, index, t, runArgs) {
    const editing = PE && PE.exp === exp.id && PE.index === index;
    const page = editing ? PE.page : (exp.method_pages || [])[index];
    if (!page) {
      PE = null;
      go({ tab: "flow" });
      return;
    }
    const done = exp.last_run && exp.last_run.status === "done";
    let run = null;
    if (done || S.run) {
      try {
        run = await call("run_get", runArgs);
      } catch {
        run = null;
      }
      if (t !== ctx.token()) return;
    }
    const src = sourcesOf(run);
    const head = h(
      "div",
      { class: "page-head" },
      editing
        ? [
            h("input", {
              class: "layr-input page-title-input",
              value: page.title,
              oninput: (e) => (page.title = e.target.value),
            }),
            h("span", { class: "layr-spacer" }),
            h("button", { class: "layr-button ghost danger", onclick: () => deletePage(exp, index) }, "Delete page"),
            h(
              "button",
              {
                class: "layr-button ghost",
                onclick: () => {
                  const wasNew = PE?.isNew;
                  PE = null;
                  go({ tab: wasNew ? "flow" : `page:${index}` });
                },
              },
              "Cancel",
            ),
            h("button", { class: "layr-button primary", onclick: () => savePage(exp) }, "Save page"),
          ]
        : [
            h(
              "span",
              { class: "layr-muted small" },
              exp.method_version
                ? `Kept in the method (version ${exp.method_version}, pinned for this experiment).`
                : "Kept in the method: every experiment of it has this page.",
            ),
            h("span", { class: "layr-spacer" }),
            h(
              "button",
              {
                class: "layr-button ghost small",
                onclick: () => {
                  PE = { exp: exp.id, index, page: clone(page), isNew: false };
                  go({});
                },
              },
              "Edit page",
            ),
          ],
    );
    const grid = h("div", { class: "cards-grid" });
    const cards = page.cards || [];
    if (!cards.length)
      grid.append(
        h(
          "div",
          { class: "layr-empty card-full" },
          editing ? "No cards yet: add the first one below." : "This page has no cards yet: press Edit page.",
        ),
      );
    cards.forEach((card, ci) => {
      const box = h("div", { class: "card-body" }, h("span", { class: "layr-spinner" }));
      const tools = editing
        ? h(
            "div",
            { class: "card-tools" },
            h("span", { class: "layr-muted small" }, CARD_KINDS.find((k) => k[0] === card.card)?.[1] || card.card),
            h("span", { class: "layr-spacer" }),
            h(
              "button",
              {
                class: "layr-button small ghost",
                title: card.width === "half" ? "Full width" : "Half width (beside the next half card)",
                onclick: () => {
                  card.width = card.width === "half" ? undefined : "half";
                  if (!card.width) delete card.width;
                  go({});
                },
              },
              card.width === "half" ? "↔ Full" : "↔ Half",
            ),
            h("button", { class: "layr-button small ghost", disabled: ci === 0, onclick: () => move(ci, -1) }, "↑"),
            h(
              "button",
              { class: "layr-button small ghost", disabled: ci === cards.length - 1, onclick: () => move(ci, 1) },
              "↓",
            ),
            h(
              "button",
              {
                class: "layr-button small ghost",
                onclick: async () => {
                  const changed = await cardForm(exp, src, card);
                  if (changed) {
                    cards[ci] = changed;
                    go({});
                  }
                },
              },
              "Change…",
            ),
            h(
              "button",
              {
                class: "layr-button small ghost danger",
                onclick: () => {
                  cards.splice(ci, 1);
                  go({});
                },
              },
              "Remove",
            ),
          )
        : null;
      grid.append(
        h(
          "section",
          { class: `layr-card page-card ${card.width === "half" ? "half" : "full"} card-${card.card}` },
          tools,
          card.title ? h("h3", { class: "card-title" }, card.title) : null,
          box,
        ),
      );
      drawCard(box, exp, card, `${exp.id}:${index}:${ci}`, run, src, runArgs, t).catch((err) =>
        put(box, h("div", { class: "layr-error small" }, err?.message || String(err))),
      );
    });
    function move(ci, by) {
      const [c] = cards.splice(ci, 1);
      cards.splice(ci + by, 0, c);
      go({});
    }
    put(
      body,
      h(
        "div",
        { class: `page wide pages-page ${editing ? "editing" : ""}` },
        head,
        !run && cards.some((c) => c.card !== "text" || c.value)
          ? h("div", { class: "banner" }, "Run the experiment: the cards show its results.")
          : null,
        grid,
        editing
          ? h(
              "div",
              { class: "add-card" },
              h(
                "button",
                {
                  class: "layr-button",
                  onclick: async () => {
                    const kind = await pickKind();
                    if (!kind) return;
                    const card = await cardForm(exp, src, { card: kind });
                    if (card) {
                      cards.push(card);
                      go({});
                    }
                  },
                },
                "+ Add a card",
              ),
              run
                ? null
                : h("span", { class: "layr-muted small" }, " Run the experiment first to pick from its tables and values."),
            )
          : null,
      ),
    );
  }

  async function savePage(exp) {
    if (!PE) return;
    if (!PE.page.title.trim()) return layr.ui.notify("The page needs a title.", "error");
    const all = clone(exp.method_pages || []);
    all[PE.index] = { ...PE.page, title: PE.page.title.trim() };
    await declarePages(exp, all, `${PE.isNew ? "Added" : "Changed"} the page "${PE.page.title.trim()}"`, PE.index);
  }

  async function deletePage(exp, index) {
    const p = (exp.method_pages || [])[index];
    if (PE?.isNew) {
      PE = null;
      go({ tab: "flow" });
      return;
    }
    const ok = await openDialog({
      title: `Delete the page "${p?.title}"?`,
      body: "Its cards go; the method's results stay. The method keeps the page in its earlier versions.",
      buttons: [
        { label: "Keep it", id: false, kind: "ghost" },
        { label: "Delete", id: true, kind: "danger" },
      ],
    });
    if (!ok) return;
    const all = clone(exp.method_pages || []);
    all.splice(index, 1);
    await declarePages(exp, all, `Removed the page "${p?.title}"`, null);
  }

  async function declarePages(exp, all, change, index) {
    try {
      const r = await call("method_declare", { method: exp.method, what: "pages", value: all, change });
      if (r.saved === false && !r.draft)
        return layr.ui.notify(r.note || (r.messages || []).map((m) => m.message).join("; "), "error");
      PE = null;
      if (exp.method_version)
        layr.ui.notify(
          `Saved as version ${r.version} of the method; this experiment is pinned to version ${exp.method_version} ` +
            "(Settings › Method version shows the new pages).",
          "info",
        );
      go({ tab: index == null ? "flow" : `page:${index}` });
    } catch (err) {
      fail(err);
    }
  }

  async function pickKind() {
    let picked = null;
    await openDialog({
      title: "Add a card",
      body: h(
        "div",
        { class: "kind-list" },
        CARD_KINDS.map(([id, label, text]) =>
          h(
            "button",
            {
              class: "kind-pick",
              onclick: () => {
                picked = id;
                document.getElementById("dialog").close();
              },
            },
            h("b", {}, label),
            h("span", { class: "layr-muted small" }, text),
          ),
        ),
      ),
      buttons: [{ label: "Cancel", id: null, kind: "ghost" }],
    });
    return picked;
  }

  // ------------------------------------------------------------------------------------------ a card's form

  /** The form of one card: a copy changed by the user, or null. Fields depend on each other (a table's columns). */
  async function cardForm(exp, src, card0) {
    const c = clone(card0);
    const box = h("div", { class: "card-form" });
    const tableNames = Object.keys(src.tables);
    const kinds = exp.method_corrections || {};
    const fileInputs = (exp.method_inputs || []).map((i) => i.id);
    const opt = (v, label, cur) => h("option", { value: v, selected: v === cur }, label);
    const sel = (key, options, { empty, onchange, target = c } = {}) =>
      h(
        "select",
        {
          class: "layr-select",
          onchange: (e) => {
            if (e.target.value) target[key] = e.target.value;
            else delete target[key];
            if (onchange) onchange();
            draw();
          },
        },
        empty !== undefined ? opt("", empty, target[key] || "") : null,
        options.map(([v, l]) => opt(v, l, target[key])),
        target[key] && !options.some(([v]) => v === target[key]) ? opt(target[key], `${target[key]} (not in this run)`, target[key]) : null,
      );
    const text = (key, placeholder, target = c) =>
      h("input", {
        class: "layr-input",
        value: target[key] ?? "",
        placeholder: placeholder || "",
        oninput: (e) => {
          if (e.target.value.trim()) target[key] = e.target.value;
          else delete target[key];
        },
      });
    const number = (key, placeholder, target = c) =>
      h("input", {
        class: "layr-input",
        type: "number",
        step: "any",
        value: target[key] ?? "",
        placeholder: placeholder || "",
        oninput: (e) => {
          if (e.target.value === "") delete target[key];
          else target[key] = +e.target.value;
        },
      });
    // a list of columns; an entry may also be {column, label, unit} (kept as it is while its column stays ticked)
    const colOf = (x) => (typeof x === "string" ? x : x?.column);
    const checks = (key, options, target = c) =>
      h(
        "div",
        { class: "check-list" },
        options.length
          ? options.map((col) =>
              h(
                "label",
                { class: "check" },
                h("input", {
                  type: "checkbox",
                  checked: (target[key] || []).some((x) => colOf(x) === col),
                  onchange: (e) => {
                    const before = target[key] || [];
                    const cur = new Set(before.map(colOf));
                    if (e.target.checked) cur.add(col);
                    else cur.delete(col);
                    const order = options.filter((o) => cur.has(o)).map((o) => before.find((x) => colOf(x) === o) ?? o);
                    if (order.length) target[key] = order;
                    else delete target[key];
                  },
                }),
                " ",
                col,
              ),
            )
          : h("span", { class: "layr-muted small" }, "Pick the table first."),
      );
    const tableOpts = () => tableNames.map((n) => [n, src.labels[n] && src.labels[n] !== n ? `${src.labels[n]} (${n})` : n]);
    const colOpts = (tname) => (src.tables[tname] || []).map((x) => [x, x]);
    const field = (label, el, help) =>
      h("div", { class: "form-row" }, h("label", { class: "layr-label" }, label), el, help ? h("div", { class: "layr-muted small" }, help) : null);
    const titleRow = () => field("Title (optional)", text("title", ""));

    function draw() {
      const rows = [];
      if (c.card === "text") {
        const mode = c.value !== undefined ? "value" : "text";
        rows.push(
          field(
            "What it shows",
            h(
              "select",
              {
                class: "layr-select",
                onchange: (e) => {
                  if (e.target.value === "value") {
                    delete c.text;
                    c.value = src.texts[0] || src.values[0] || "";
                  } else delete c.value;
                  draw();
                },
              },
              opt("text", "My own words", mode),
              opt("value", "A text or value the method returns", mode),
            ),
          ),
        );
        if (mode === "text") {
          const ta = h("textarea", { class: "layr-textarea", rows: 6, oninput: (e) => (c.text = e.target.value) }, c.text || "");
          rows.push(field("Text", ta, "An empty line starts a paragraph; a line starting with # is a heading."));
        } else rows.push(field("Result", sel("value", [...src.texts, ...src.values].map((v) => [v, src.labels[v] || v]))));
      } else if (c.card === "number") {
        const mode = c.table ? "table" : "value";
        rows.push(
          field(
            "Where it comes from",
            h(
              "select",
              {
                class: "layr-select",
                onchange: (e) => {
                  if (e.target.value === "table") {
                    delete c.value;
                    c.table = tableNames[0];
                    c.summary = "mean";
                  } else {
                    delete c.table;
                    delete c.column;
                    delete c.summary;
                    delete c.where;
                  }
                  draw();
                },
              },
              opt("value", "A value the method returns", mode),
              opt("table", "A column of a table, summarized", mode),
            ),
          ),
        );
        if (mode === "value") rows.push(field("Value", sel("value", src.values.map((v) => [v, src.labels[v] || v]), { empty: "–" })));
        else {
          rows.push(field("Table", sel("table", tableOpts(), { onchange: () => delete c.column })));
          rows.push(field("Column", sel("column", colOpts(c.table), { empty: "–" })));
          rows.push(field("Summary", sel("summary", SUMMARIES)));
        }
        rows.push(field("Unit (optional)", text("unit", "e.g. %")));
      } else if (c.card === "table") {
        rows.push(field("Table", sel("table", tableOpts(), { empty: "–", onchange: () => delete c.columns })));
        rows.push(field("Columns (none ticked: all)", checks("columns", src.tables[c.table] || [])));
      } else if (c.card === "figure") {
        rows.push(field("Figure", sel("figure", src.figures.map((f) => [f, src.labels[f] || f]), { empty: "–" })));
      } else if (c.card === "chart") {
        rows.push(
          field(
            "Table",
            sel("table", tableOpts(), {
              empty: "–",
              onchange: () => {
                delete c.x;
                delete c.y;
                delete c.group;
              },
            }),
          ),
        );
        rows.push(field("Kind", sel("kind", [["line", "Lines"], ["scatter", "Points"], ["bar", "Bars"]])));
        rows.push(field("Across (x)", sel("x", colOpts(c.table), { empty: "–" })));
        rows.push(field("Up (y): one or more columns", checks("y", src.tables[c.table] || [])));
        rows.push(field("One colour per (optional)", sel("group", colOpts(c.table), { empty: "–" })));
      } else if (c.card === "review") {
        c.curve ||= {};
        const cols = colOpts(c.regions);
        rows.push(
          h(
            "p",
            { class: "layr-muted small" },
            "One row per region in a table (a peak, a phase, a window): which item it belongs to (an injection, a " +
              "sample), its name, where it starts and ends. Each item has a curve; the regions are drawn on it.",
          ),
        );
        rows.push(field("Table of regions", sel("regions", tableOpts(), { empty: "–" })));
        rows.push(field("Item (one curve each)", sel("item", cols, { empty: "–" })));
        rows.push(field("Region's name (optional)", sel("label", cols, { empty: "–" })));
        rows.push(field("Start", sel("start", cols, { empty: "–" })));
        rows.push(field("End", sel("end", cols, { empty: "–" })));
        rows.push(field("Item's name to show (optional)", sel("name", cols, { empty: "–" })));
        rows.push(field("Filter items by (optional)", sel("group", cols, { empty: "–" })));
        rows.push(field("Status (optional)", sel("status", cols, { empty: "–" })));
        rows.push(field("Details shown for a region", checks("info", (src.tables[c.regions] || []).filter((x) => ![c.item, c.label, c.start, c.end].includes(x)))));
        const curveMode = c.curve.file ? "file" : "table";
        rows.push(
          field(
            "The curves come from",
            h(
              "select",
              {
                class: "layr-select",
                onchange: (e) => {
                  c.curve = e.target.value === "file" ? { file: fileInputs[0] } : {};
                  draw();
                },
              },
              opt("table", "A table of the run (one row per point)", curveMode),
              opt("file", "A raw file of the experiment (a GC sequence, GAML)", curveMode),
            ),
          ),
        );
        if (curveMode === "file") rows.push(field("Raw file", sel("file", fileInputs.map((f) => [f, f]), { target: c.curve })));
        else {
          const ccols = colOpts(c.curve.table);
          rows.push(field("Table of points", sel("table", tableOpts(), { empty: "–", target: c.curve })));
          rows.push(field("Its item column", sel("item", ccols, { empty: "same name as the item", target: c.curve })));
          rows.push(field("x", sel("x", ccols, { empty: "–", target: c.curve })));
          rows.push(field("y", sel("y", ccols, { empty: "–", target: c.curve })));
        }
        rows.push(field("Shown around a region (x units; empty: three times its width)", number("window", "")));
        rows.push(field("x unit", text("x_label", "e.g. min")));
        rows.push(field("y unit", text("y_label", "e.g. pA")));
        const kindOpts = Object.entries(kinds).map(([k, v]) => [k, v.label]);
        rows.push(
          field(
            "Dragged ends are saved as (a kind of correction)",
            sel("correct", kindOpts, { empty: "nothing: only look", onchange: () => (delete c.start_field, delete c.end_field) }),
            kindOpts.length ? null : "This method has no kinds of correction yet (Corrections › Kinds of correction).",
          ),
        );
        if (c.correct && kinds[c.correct]) {
          const nums = Object.entries(kinds[c.correct].fields)
            .filter(([, f]) => f.type === "number")
            .map(([n]) => [n, n]);
          rows.push(field("Its start field", sel("start_field", nums, { empty: "–" })));
          rows.push(field("Its end field", sel("end_field", nums, { empty: "–" })));
        }
        rows.push(field("Leaving out is saved as (optional)", sel("exclude", kindOpts, { empty: "–" })));
      }
      rows.push(titleRow());
      put(box, rows);
    }
    draw();
    const ok = await openDialog({
      title: CARD_KINDS.find((k) => k[0] === c.card)?.[1] || "Card",
      wide: c.card === "review",
      body: box,
      buttons: [
        { label: "Cancel", id: null, kind: "ghost" },
        { label: "Done", kind: "primary", value: () => (cardMissing(c) ? (layr.ui.notify(cardMissing(c), "error"), undefined) : true) },
      ],
    });
    return ok ? c : null;
  }

  function cardMissing(c) {
    if (c.card === "text" && !c.text && !c.value) return "Write the text, or pick a result.";
    if (c.card === "number" && !c.value && !(c.table && c.column)) return "Pick a value, or a table and its column.";
    if (c.card === "table" && !c.table) return "Pick a table.";
    if (c.card === "figure" && !c.figure) return "Pick a figure.";
    if (c.card === "chart" && !(c.table && c.x && (c.y || []).length)) return "Pick the table, its x and at least one y.";
    if (c.card === "review") {
      if (!(c.regions && c.item && c.start && c.end)) return "Pick the table of regions, the item, the start and the end.";
      if (!c.curve?.file && !(c.curve?.table && c.curve?.x && c.curve?.y)) return "Say where the curves come from.";
      if (c.correct && !(c.start_field && c.end_field)) return "Pick the correction's start and end fields.";
    }
    return null;
  }

  // ------------------------------------------------------------------------------------------ drawing cards

  async function drawCard(box, exp, card, key, run, src, runArgs, t) {
    if (card.card === "text" && !card.value) return put(box, h("div", { class: "card-text" }, textBlock(card.text)));
    if (!run || run.status !== "done") return put(box, h("div", { class: "layr-muted small" }, "No results yet."));
    if (card.card === "text") return drawText(box, card, run, runArgs, t);
    if (card.card === "number") return drawNumber(box, card, run, src, runArgs, t);
    if (card.card === "table") return drawTable(box, exp, card, src, runArgs, t);
    if (card.card === "figure") {
      const img = await figureOf({ run: run.run, output: card.figure });
      if (t !== ctx.token()) return;
      return put(box, img ? h("img", { class: "fig", src: img, alt: card.title || card.figure }) : h("div", { class: "layr-muted" }, `No figure "${card.figure}" in this run.`));
    }
    if (card.card === "chart") return drawChart(box, card, src, runArgs, t);
    if (card.card === "review") return reviewCard(box, exp, card, key, t);
    put(box, h("div", { class: "layr-muted" }, `Unknown card: ${card.card}`));
  }

  async function drawText(box, card, run, runArgs, t) {
    const o = run.outputs?.[card.value];
    if (!o) return put(box, h("div", { class: "layr-muted" }, `The run has no result "${card.value}".`));
    if (o.kind === "text") {
      const got = await call("result_value", { ...runArgs, output: card.value });
      if (t !== ctx.token()) return;
      return put(box, h("pre", { class: "text-result" }, got.text));
    }
    put(box, h("div", { class: "card-text" }, textBlock(valueText(o.value))));
  }

  async function drawNumber(box, card, run, src, runArgs, t) {
    let v, sub;
    if (card.value) {
      const o = run.outputs?.[card.value];
      if (!o) return put(box, h("div", { class: "layr-muted" }, `The run has no value "${card.value}".`));
      v = o.kind === "value" ? o.value : o.preview;
      sub = src.labels[card.value];
    } else {
      const got = await allRows(runArgs, card.table, [card.column, ...Object.keys(card.where || {})], src.tables[card.table]);
      if (t !== ctx.token()) return;
      const rows = got.rows.filter((r) => Object.entries(card.where || {}).every(([k, w]) => same(r[k], w)));
      v = summarize(
        rows.map((r) => r[card.column]),
        card.summary || "mean",
      );
      sub = `${SUMMARIES.find((x) => x[0] === (card.summary || "mean"))?.[1]} of ${card.column} · ${rows.length} rows`;
    }
    put(
      box,
      h("div", { class: "big-number" }, valueText(v), card.unit ? h("span", { class: "unit" }, ` ${card.unit}`) : null),
      sub && sub !== card.title ? h("div", { class: "layr-muted small" }, sub) : null,
    );
  }

  async function drawTable(box, exp, card, src, runArgs, t) {
    if (!src.tables[card.table]) return put(box, h("div", { class: "layr-muted" }, `The run has no table "${card.table}".`));
    const data = await call("result_table", { ...runArgs, table: card.table, limit: 2000, columns: card.columns });
    if (t !== ctx.token()) return;
    ctx.filteredTable(box, exp, card.table, data, card.filters);
  }

  // ------------------------------------------------------------------------------------------ charts

  async function drawChart(box, card, src, runArgs, t) {
    const have = src.tables[card.table];
    if (!have) return put(box, h("div", { class: "layr-muted" }, `The run has no table "${card.table}".`));
    const ys = (Array.isArray(card.y) ? card.y : [card.y]).filter(Boolean);
    const got = await allRows(runArgs, card.table, [card.x, ...ys, card.group], have);
    if (t !== ctx.token()) return;
    put(box, chartSvg(got.rows, { ...card, y: ys }, box.clientWidth || 640));
  }

  /** Lines, points or bars of `y` columns against `x`, one colour per series (a y column, or a group's value). */
  function chartSvg(rows, { x, y, group, kind = "line" }, width) {
    const W = Math.max(360, Math.min(width, 1000)),
      H = 280,
      L = 56,
      R = 12,
      T = 12,
      B = 42;
    const series = [];
    if (group && y.length === 1)
      for (const g of uniq(rows.map((r) => r[group])).sort())
        series.push({ name: String(g), rows: rows.filter((r) => same(r[group], g)), y: y[0] });
    else for (const col of y) series.push({ name: col, rows, y: col });
    const categorical = kind === "bar" || rows.some((r) => r[x] != null && r[x] !== "" && num(r[x]) == null);
    const cats = categorical ? uniq(rows.map((r) => String(r[x]))) : [];
    const pts = series.map((sr) => {
      let p = sr.rows.map((r) => ({ x: categorical ? String(r[x]) : num(r[x]), y: num(r[sr.y]) })).filter((q) => q.x != null && q.y != null);
      if (kind === "bar" || (categorical && kind === "line")) {
        const by = new Map();
        for (const q of p) (by.get(q.x) || by.set(q.x, []).get(q.x)).push(q.y);
        p = [...by].map(([k, v]) => ({ x: k, y: v.reduce((a, b) => a + b, 0) / v.length }));
      }
      if (!categorical) p.sort((a, b) => a.x - b.x);
      return { ...sr, p };
    });
    const allY = pts.flatMap((sr) => sr.p.map((q) => q.y));
    if (!allY.length) return h("div", { class: "layr-muted" }, "No numbers to draw in these columns.");
    let ymin = Math.min(...allY, kind === "bar" ? 0 : Infinity),
      ymax = Math.max(...allY, kind === "bar" ? 0 : -Infinity);
    if (ymin === ymax) (ymin -= 1), (ymax += 1);
    const pad = (ymax - ymin) * 0.06;
    if (kind !== "bar" || ymin < 0) ymin -= pad;
    ymax += pad;
    const allX = categorical ? [] : pts.flatMap((sr) => sr.p.map((q) => q.x));
    let xmin = Math.min(...allX),
      xmax = Math.max(...allX);
    if (!categorical && xmin === xmax) (xmin -= 1), (xmax += 1);
    const band = categorical ? (W - L - R) / Math.max(1, cats.length) : 0;
    const X = (v) => (categorical ? L + band * (cats.indexOf(v) + 0.5) : L + ((v - xmin) / (xmax - xmin)) * (W - L - R));
    const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * (H - T - B);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "chart" });
    for (let i = 0; i <= 4; i++) {
      const v = ymin + ((ymax - ymin) * i) / 4;
      svg.append(s("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "grid" }));
      const lab = s("text", { x: L - 6, y: Y(v) + 4, class: "axis", "text-anchor": "end" });
      lab.textContent = fmt(+v.toPrecision(3));
      svg.append(lab);
    }
    const xt = categorical ? cats : Array.from({ length: 6 }, (_, i) => xmin + ((xmax - xmin) * i) / 5);
    const step = categorical ? Math.ceil(cats.length / Math.max(1, Math.floor((W - L - R) / 70))) : 1;
    xt.forEach((v, i) => {
      if (i % step) return;
      const lab = s("text", { x: X(v), y: H - B + 16, class: "axis", "text-anchor": "middle" });
      lab.textContent = categorical ? String(v).slice(0, 14) : fmt(+v.toPrecision(4));
      svg.append(lab);
    });
    const xl = s("text", { x: W - R, y: H - 6, class: "axis", "text-anchor": "end" });
    xl.textContent = x;
    svg.append(xl);
    pts.forEach((sr, si) => {
      const color = PALETTE[si % PALETTE.length];
      if (kind === "bar") {
        const bw = (band * 0.8) / pts.length;
        for (const q of sr.p) {
          const x0 = X(q.x) - band * 0.4 + si * bw;
          const y0 = Y(Math.max(0, q.y)),
            y1 = Y(Math.min(0, q.y));
          const r = s("rect", { x: x0, y: y0, width: Math.max(1, bw - 1), height: Math.max(1, y1 - y0), fill: color });
          const tt = s("title", {});
          tt.textContent = `${sr.name} · ${q.x}: ${fmt(q.y)}`;
          r.append(tt);
          svg.append(r);
        }
      } else {
        if (kind === "line" && sr.p.length > 1)
          svg.append(s("polyline", { points: sr.p.map((q) => `${X(q.x).toFixed(1)},${Y(q.y).toFixed(1)}`).join(" "), fill: "none", stroke: color, "stroke-width": 1.6 }));
        for (const q of sr.p) {
          const c = s("circle", { cx: X(q.x), cy: Y(q.y), r: kind === "scatter" ? 3.2 : 2.4, fill: color, "fill-opacity": kind === "scatter" ? 0.75 : 1 });
          const tt = s("title", {});
          tt.textContent = `${sr.name} · ${x} ${fmt(q.x)}: ${fmt(q.y)}`;
          c.append(tt);
          svg.append(c);
        }
      }
    });
    const legend =
      pts.length > 1 || group
        ? h(
            "div",
            { class: "chart-legend small" },
            pts.map((sr, si) => h("span", {}, h("i", { style: `background:${PALETTE[si % PALETTE.length]}` }), sr.name)),
          )
        : h("div", { class: "chart-legend small layr-muted" }, y.join(", "));
    return h("div", { class: "chart-box" }, svg, legend);
  }

  // ------------------------------------------------------------------------------------------ review a curve

  const statusClass = (card, v) =>
    STATUS_CLASS[v] || ((card.ok || ["ok"]).includes(v) ? "ok" : v == null || v === "" ? "nd" : "loq");

  async function reviewCard(box, exp, card, key, t) {
    if (!exp.last_run || exp.last_run.status !== "done")
      return put(box, h("div", { class: "layr-empty" }, "Run the experiment first: the regions come from a run."));
    // reviewed (and corrected) on the latest run; an earlier run picked in the flow doesn't change that
    const latest = await call("run_get", { experiment: exp.id });
    if (t !== ctx.token()) return;
    const src = sourcesOf(latest);
    const have = src.tables[card.regions];
    if (!have) return put(box, h("div", { class: "layr-muted" }, `The run has no table "${card.regions}".`));
    const infoCols = (card.info || []).map((i) => (typeof i === "string" ? i : i.column));
    const want = [card.item, card.label, card.start, card.end, card.status, card.name, card.group, card.expected, card.tolerance, card.note, card.marker, ...infoCols, ...(card.baseline || [])];
    const [got, fl] = await Promise.all([
      allRows({ experiment: exp.id }, card.regions, want, have),
      src.tables.flags ? call("result_table", { experiment: exp.id, table: "flags", limit: 2000 }).catch(() => ({ rows: [] })) : { rows: [] },
    ]);
    if (t !== ctx.token()) return;
    const st = ((S.review ||= {})[key] ||= {});
    if (S.focus) {
      st.item = S.focus.item;
      if (S.focus.label) st.label = S.focus.label;
      st.filter = "all";
      S.focus = null;
      save();
    }
    const labels = card.label ? uniq(got.rows.map((r) => r[card.label])) : [""];
    const items = new Map();
    for (const r of got.rows) {
      const id = r[card.item];
      if (id == null || id === "") continue;
      const k = String(id);
      if (!items.has(k)) items.set(k, { item: id, name: card.name ? r[card.name] : null, group: card.group ? r[card.group] : null, regions: {} });
      items.get(k).regions[card.label ? String(r[card.label]) : ""] = r;
    }
    const flagged = new Map();
    for (const f of fl.rows)
      if (f[card.item] != null && f[card.item] !== "") {
        const k = String(f[card.item]);
        if (!flagged.has(k)) flagged.set(k, []);
        flagged.get(k).push(f);
      }
    const all = [...items.values()];
    const ok = card.ok || ["ok"];
    const needsLook = (i) =>
      flagged.has(String(i.item)) || (card.status && Object.values(i.regions).some((r) => !ok.includes(r[card.status])));
    const groups = card.group ? uniq(all.map((i) => i.group)) : [];
    const filters = { all: () => true, look: needsLook };
    for (const g of groups) filters[`g:${g}`] = (i) => same(i.group, g);
    const shown = all.filter(filters[st.filter] || filters.all);
    const fresh = !st.item || !items.has(String(st.item));
    if (fresh) // start where a look is needed
      st.item = (shown.find(needsLook) || shown.find((i) => Object.values(i.regions).some((r) => num(r[card.start]) != null)) ||
        shown[0] || all[0])?.item;
    if (!st.label || !labels.map(String).includes(String(st.label)) || fresh) {
      const first = items.get(String(st.item));
      const odd = first && card.status ? labels.find((l) => first.regions[String(l)] && !ok.includes(first.regions[String(l)][card.status])) : null;
      if (!st.label || !labels.map(String).includes(String(st.label)) || odd != null) st.label = odd ?? labels[0];
    }
    const label = String(st.label ?? "");
    const itemWord = human(card.item).toLowerCase();
    const filterSel = h(
      "select",
      {
        class: "layr-select small",
        onchange: (e) => {
          st.filter = e.target.value;
          save();
          reviewCard(box, exp, card, key, ctx.token());
        },
      },
      [["all", `All (${all.length})`], ...groups.map((g) => [`g:${g}`, String(g)]), ["look", "Needs a look"]].map(([v, l]) =>
        h("option", { value: v, selected: (st.filter || "all") === v }, l),
      ),
    );
    const pick = (patch) => {
      Object.assign(st, patch);
      save();
      reviewCard(box, exp, card, key, ctx.token());
    };
    const list = h(
      "ul",
      { class: "inj-list" },
      shown.map((i) =>
        h(
          "li",
          { class: same(i.item, st.item) ? "active" : "", onclick: () => pick({ item: i.item }) },
          h("span", { class: "inj-num" }, typeof i.item === "number" ? `#${i.item}` : String(i.item)),
          h("span", { class: "inj-name", title: i.name || "" }, i.name ?? ""),
          card.status
            ? h(
                "span",
                { class: "dots" },
                labels.map((l) =>
                  h("i", {
                    class: `dot ${statusClass(card, i.regions[String(l)]?.[card.status])}`,
                    title: `${l ? l + ": " : ""}${i.regions[String(l)]?.[card.status] ?? "–"}`,
                  }),
                ),
              )
            : null,
          flagged.has(String(i.item))
            ? h("span", { class: "flag-mark", title: flagged.get(String(i.item)).map((f) => f.message).join("\n") }, "!")
            : null,
        ),
      ),
    );
    const chips =
      card.label && labels.length > 1
        ? h(
            "div",
            { class: "chips" },
            labels.map((l) => h("button", { class: `chip ${String(l) === label ? "active" : ""}`, onclick: () => pick({ label: l }) }, String(l))),
          )
        : null;
    const idx = shown.findIndex((i) => same(i.item, st.item));
    const nav = h(
      "span",
      {},
      h("button", { class: "layr-button small ghost", disabled: idx <= 0, onclick: () => pick({ item: shown[idx - 1].item }) }, "‹ Previous"),
      h("button", { class: "layr-button small ghost", disabled: idx < 0 || idx >= shown.length - 1, onclick: () => pick({ item: shown[idx + 1].item }) }, "Next ›"),
    );
    const plotBox = h("div", { class: "plot-box" }, h("div", { class: "layr-empty" }, h("span", { class: "layr-spinner" })));
    const statuses = card.status ? uniq(got.rows.map((r) => r[card.status])) : [];
    const legend = statuses.length
      ? h(
          "div",
          { class: "legend layr-muted small" },
          statuses.map((v) => [h("i", { class: `dot ${statusClass(card, v)}` }), ` ${(card.status_labels || {})[v] || v}  `]),
        )
      : null;
    put(
      box,
      h(
        "div",
        { class: "peaks-grid review-grid" },
        h("div", { class: "inj-col" }, filterSel, list),
        h("div", { class: "plot-col" }, h("div", { class: "plot-head" }, chips, h("span", { class: "layr-spacer" }), nav), plotBox, legend),
      ),
    );
    const it = items.get(String(st.item));
    if (!it) return put(plotBox, h("div", { class: "layr-empty" }, `No ${itemWord} to show.`));
    drawReview(plotBox, exp, card, it, label, flagged.get(String(it.item)) || [], st, key, t).catch((err) =>
      put(plotBox, h("div", { class: "layr-error small" }, err?.message || String(err))),
    );
  }

  async function drawReview(box, exp, card, it, label, flags, st, key, t) {
    const r0 = it.regions[label] || {};
    const a0 = num(r0[card.start]),
      b0 = num(r0[card.end]);
    const center = num(r0[card.expected]) ?? (a0 != null && b0 != null ? (a0 + b0) / 2 : null);
    const half = st.zoom || num(card.window) || (a0 != null && b0 != null ? Math.max((b0 - a0) * 1.5, 1e-9) : null);
    const range = center != null && half != null ? { start: center - half, end: center + half } : {};
    const curveArgs = card.curve?.file
      ? { file: card.curve.file }
      : { table: card.curve?.table, item_column: card.curve?.item || card.item, x: card.curve?.x, y: card.curve?.y };
    const data = await call("curve", { experiment: exp.id, item: it.item, ...curveArgs, ...range, points: 1400 });
    if (t !== ctx.token()) return;
    const xs = data.x,
      ys = data.y;
    if (xs.length < 2) return put(box, h("div", { class: "layr-empty" }, "No curve points here."));
    const kinds = exp.method_corrections || {};
    const fieldsOf = (k) => kinds[k]?.fields || {};
    const itemField = (k) => (card.item in fieldsOf(k) ? card.item : null);
    const labelField = (k) => (card.label && card.label in fieldsOf(k) ? card.label : null);
    const corrHere = (exp.corrections || []).filter(
      (c) => same(c[card.item], it.item) && (!card.label || c[card.label] == null || c[card.label] === "" || same(c[card.label], label)),
    );
    const active = corrHere.filter((c) => c.status === "approved");
    const W = Math.max(420, box.clientWidth || 700),
      H = 300,
      L = 54,
      R = 14,
      T = 12,
      B = 30;
    let ymin = Math.min(...ys),
      ymax = Math.max(...ys);
    const pad = (ymax - ymin) * 0.08 || 1;
    ymin -= pad;
    ymax += pad;
    const x0 = xs[0],
      x1 = xs[xs.length - 1];
    const X = (v) => L + ((v - x0) / (x1 - x0)) * (W - L - R);
    const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * (H - T - B);
    const Xinv = (px) => x0 + ((px - L) / (W - L - R)) * (x1 - x0);
    const yAt = (tm) => {
      let k = 0;
      while (k < xs.length - 1 && xs[k] < tm) k++;
      return ys[k];
    };
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "chrom" });
    for (let i = 0; i <= 4; i++) {
      const v = ymin + ((ymax - ymin) * i) / 4;
      svg.append(s("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "grid" }));
      const lab = s("text", { x: L - 6, y: Y(v) + 4, class: "axis", "text-anchor": "end" });
      lab.textContent = fmt(v);
      svg.append(lab);
    }
    const digits = Math.max(0, Math.min(6, 2 - Math.floor(Math.log10(Math.abs(x1 - x0) || 1))));
    for (let i = 0; i <= 5; i++) {
      const v = x0 + ((x1 - x0) * i) / 5;
      const lab = s("text", { x: X(v), y: H - 10, class: "axis", "text-anchor": "middle" });
      lab.textContent = v.toFixed(digits);
      svg.append(lab);
    }
    if (card.y_label) {
      const yl = s("text", { x: 12, y: T + 8, class: "axis" });
      yl.textContent = card.y_label;
      svg.append(yl);
    }
    if (card.x_label) {
      const xl = s("text", { x: W - R, y: H - B - 4, class: "axis", "text-anchor": "end" });
      xl.textContent = card.x_label;
      svg.append(xl);
    }
    const expected = num(r0[card.expected]);
    if (expected != null) {
      const tol = num(r0[card.tolerance]) || 0;
      if (tol) svg.append(s("rect", { x: X(expected - tol), y: T, width: Math.max(1, X(expected + tol) - X(expected - tol)), height: H - T - B, class: "tol" }));
      svg.append(s("line", { x1: X(expected), x2: X(expected), y1: T, y2: H - B, class: "expected" }));
    }
    const [bs, be] = card.baseline || [];
    const baseAt = (r, which, tm) => num(r[which === "start" ? bs : be]) ?? yAt(tm);
    for (const [l, o] of Object.entries(it.regions)) {
      const oa = num(o[card.start]),
        ob = num(o[card.end]);
      if (l !== label && oa != null && ob != null && oa > x0 && ob < x1)
        svg.append(s("polygon", { points: areaPoints(xs, ys, oa, ob, baseAt(o, "start", oa), baseAt(o, "end", ob), X, Y), class: "area other" }));
    }
    const shade = s("polygon", { class: "area" });
    const base = s("line", { class: "baseline" });
    svg.append(shade, base);
    svg.append(s("polyline", { points: xs.map((x, i) => `${X(x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" "), class: "trace" }));
    const marker = num(r0[card.marker]);
    if (marker != null && marker > x0 && marker < x1) {
      const vm = s("text", { x: X(marker), y: T + 10, class: "vendor", "text-anchor": "middle" });
      vm.textContent = `▼ ${card.marker_label || human(card.marker)}`;
      svg.append(vm);
    }
    const canCorrect = !!(card.correct && kinds[card.correct] && card.start_field && card.end_field);
    const sel = { start: a0, end: b0, dirty: false };
    const gap = (x1 - x0) * 0.002;
    const handles = {};
    if (canCorrect)
      for (const which of ["start", "end"]) {
        const g = s("g", { class: "handle" });
        g.append(s("line", { y1: T, y2: H - B, class: "handle-line" }), s("rect", { y: T, width: 12, height: 18, rx: 3, class: "handle-grip" }));
        svg.append(g);
        handles[which] = g;
        g.addEventListener("pointerdown", (e) => {
          g.setPointerCapture(e.pointerId);
          const moveH = (ev) => {
            const r = svg.getBoundingClientRect();
            let tm = Xinv(((ev.clientX - r.left) / r.width) * W);
            tm = Math.max(x0, Math.min(x1, tm));
            if (which === "start") sel.start = Math.min(tm, (sel.end ?? tm) - gap);
            else sel.end = Math.max(tm, (sel.start ?? tm) + gap);
            sel.dirty = true;
            update();
          };
          const up = () => {
            g.removeEventListener("pointermove", moveH);
            g.removeEventListener("pointerup", up);
          };
          g.addEventListener("pointermove", moveH);
          g.addEventListener("pointerup", up);
        });
      }
    const where = `${label ? label + " in " : ""}${typeof it.item === "number" ? "#" : ""}${it.item}`;
    const round = (v) => +v.toPrecision(Math.max(6, digits + 3));
    const saveBtn = canCorrect
      ? h(
          "button",
          {
            class: "layr-button primary small",
            disabled: true,
            onclick: async () => {
              const reason = await askReason(`${kinds[card.correct].label}: ${where}`, "e.g. The valley at the right end was cut off");
              if (!reason) return;
              const fields = { [card.start_field]: round(sel.start), [card.end_field]: round(sel.end) };
              if (itemField(card.correct)) fields[itemField(card.correct)] = it.item;
              if (labelField(card.correct)) fields[labelField(card.correct)] = label;
              await correct(card.correct, fields, reason);
            },
          },
          "Save these ends…",
        )
      : null;
    function update() {
      const okSel = sel.start != null && sel.end != null;
      for (const [which, g] of Object.entries(handles)) {
        const v = sel[which];
        g.style.display = v == null ? "none" : "";
        if (v != null) {
          g.querySelector("line").setAttribute("x1", X(v));
          g.querySelector("line").setAttribute("x2", X(v));
          g.querySelector("rect").setAttribute("x", X(v) - 6);
        }
      }
      if (okSel) {
        const y0 = sel.dirty ? yAt(sel.start) : baseAt(r0, "start", sel.start),
          y1 = sel.dirty ? yAt(sel.end) : baseAt(r0, "end", sel.end);
        shade.setAttribute("points", areaPoints(xs, ys, sel.start, sel.end, y0, y1, X, Y));
        base.setAttribute("x1", X(sel.start));
        base.setAttribute("x2", X(sel.end));
        base.setAttribute("y1", Y(y0));
        base.setAttribute("y2", Y(y1));
        shade.classList.toggle("excluded", statusClass(card, r0[card.status]) === "excl" && !sel.dirty);
      }
      shade.style.display = base.style.display = okSel ? "" : "none";
      if (saveBtn) saveBtn.disabled = !sel.dirty;
    }
    update();
    async function correct(kind, fields, reason) {
      try {
        await call("correction_propose", { experiment: exp.id, kind, fields, reason });
        startRun(exp.id);
      } catch (err) {
        fail(err);
      }
    }
    const setByHand = () => {
      if (sel.start == null) {
        const c = center ?? (x0 + x1) / 2;
        sel.start = c - (x1 - x0) * 0.08;
        sel.end = c + (x1 - x0) * 0.08;
        sel.dirty = true;
        update();
      }
    };
    const excl = card.exclude && kinds[card.exclude] ? card.exclude : null;
    const exclFields = excl ? fieldsOf(excl) : {};
    const info = (card.info || []).map((i) => (typeof i === "string" ? { column: i } : i)).filter((i) => i.column in r0);
    const zoom = (f) => {
      st.zoom = Math.max((x1 - x0) / 400 || 1e-9, (half || (x1 - x0) / 2) * f);
      save();
      drawReview(box, exp, card, it, label, flags, st, key, ctx.token());
    };
    put(
      box,
      svg,
      h(
        "div",
        { class: "peak-info" },
        h("div", { class: "peak-title" }, h("b", {}, where), " ", h("span", { class: "layr-muted" }, it.name ?? "")),
        h(
          "div",
          { class: "kv" },
          card.status ? kv("Status", h("span", { class: `st ${statusClass(card, r0[card.status])}` }, (card.status_labels || {})[r0[card.status]] || r0[card.status] || "–")) : null,
          a0 != null ? kv("From", `${fmt(a0)} to ${fmt(b0)}${card.x_label ? " " + card.x_label : ""}`) : null,
          info.map((i) => kv(i.label || human(i.column), r0[i.column] == null || r0[i.column] === "" ? "–" : `${fmt(r0[i.column])}${i.unit ? " " + i.unit : ""}`)),
          expected != null ? kv("Expected", `${fmt(expected)}${num(r0[card.tolerance]) ? ` ± ${fmt(r0[card.tolerance])}` : ""}${card.x_label ? " " + card.x_label : ""}`) : null,
        ),
        card.note && r0[card.note] ? h("div", { class: "layr-muted small" }, r0[card.note]) : null,
        flags.length
          ? h("ul", { class: "peak-flags" }, flags.map((f) => h("li", { class: f.level }, `${card.label && f[card.label] ? f[card.label] + ": " : ""}${f.message}`)))
          : null,
        active.length
          ? h(
              "div",
              { class: "active-corr" },
              active.map((c) =>
                h(
                  "div",
                  {},
                  badge(kinds[c.kind]?.label || c.kind.replace(/_/g, " "), "accent"),
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
          canCorrect && sel.start == null ? h("button", { class: "layr-button small", onclick: setByHand }, "Set the ends by hand") : null,
          saveBtn,
          excl && card.label && labelField(excl) && statusClass(card, r0[card.status]) !== "excl"
            ? h(
                "button",
                {
                  class: "layr-button small",
                  onclick: async () => {
                    const reason = await askReason(`Leave out ${where}`);
                    if (!reason) return;
                    const f = { [labelField(excl)]: label };
                    if (itemField(excl)) f[itemField(excl)] = it.item;
                    await correct(excl, f, reason);
                  },
                },
                `Leave out this ${human(card.label).toLowerCase()}…`,
              )
            : null,
          excl && itemField(excl) && (!card.label || !labelField(excl) || exclFields[card.label]?.optional)
            ? h(
                "button",
                {
                  class: "layr-button small ghost",
                  onclick: async () => {
                    const reason = await askReason(`Leave out the whole ${human(card.item).toLowerCase()} ${it.item}`);
                    if (reason) await correct(excl, { [itemField(excl)]: it.item }, reason);
                  },
                },
                `Leave out the whole ${human(card.item).toLowerCase()}…`,
              )
            : null,
          h("span", { class: "layr-spacer" }),
          h("button", { class: "layr-button small ghost icon", title: "Zoom in", onclick: () => zoom(1 / 1.6) }, "+"),
          h("button", { class: "layr-button small ghost icon", title: "Zoom out", onclick: () => zoom(1.6) }, "−"),
        ),
        canCorrect
          ? h("div", { class: "layr-muted small" }, "Drag the two handles to move the region's start and end; the base is the straight line between them.")
          : null,
      ),
    );
  }

  function areaPoints(xs, ys, a, b, y0, y1, X, Y) {
    const pts = [];
    for (let i = 0; i < xs.length; i++) if (xs[i] >= a && xs[i] <= b) pts.push(`${X(xs[i]).toFixed(1)},${Y(ys[i]).toFixed(1)}`);
    return [...pts, `${X(b).toFixed(1)},${Y(y1).toFixed(1)}`, `${X(a).toFixed(1)},${Y(y0).toFixed(1)}`].join(" ");
  }

  return {
    tabs,
    reviewTab,
    reviewFor,
    pageShowing,
    addPage,
    renderPage,
    valueText,
    chartSvg,
    editing: () => PE,
    cancelEdit: () => (PE = null),
  };
}

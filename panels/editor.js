// The Research plugin's code editor: CodeMirror 6 (vendor/cm6/cm6.js, built by scripts/vendor-research-editor.mjs)
// set up for method files, with the things the plugin draws into the code: values at the end of a line, folded checks,
// a block under a line, diagnostics, and a merge view to review a draft. It knows nothing about the plugin's tools:
// completion, hover and call signatures come from functions the page passes in. Load editor.css with it.
//
//   import { createEditor, createMerge, highlight } from "./editor.js";
//
// Lines are counted from 1 and columns from 0 everywhere (as Jedi counts them).
import * as cm from "./vendor/cm6/cm6.js";

const {
  EditorState,
  EditorSelection,
  StateField,
  StateEffect,
  Compartment,
  Annotation,
  Prec,
  EditorView,
  Decoration,
  WidgetType,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  drawSelection,
  dropCursor,
  rectangularSelection,
  hoverTooltip,
  showTooltip,
  gutter,
  GutterMarker,
  defaultKeymap,
  history,
  historyKeymap,
  indentMore,
  indentLess,
  indentUnit,
  indentOnInput,
  bracketMatching,
  foldGutter,
  foldKeymap,
  codeFolding,
  syntaxHighlighting,
  syntaxTree,
  searchKeymap,
  highlightSelectionMatches,
  autocompletion,
  completionKeymap,
  closeBrackets,
  closeBracketsKeymap,
  lintGutter,
  MergeView,
  unifiedMergeView,
  getChunks,
  getOriginalDoc,
  python,
  pythonLanguage,
  tags: t,
  tagHighlighter,
  highlightCode,
} = cm;

// -------------------------------------------------------------------------------- shared parts

/** True on a device with only a touch screen (a phone or a tablet without a mouse). */
export const touchOnly = () => matchMedia("(pointer: coarse)").matches && !matchMedia("(any-pointer: fine)").matches;

const el = (tag, cls, ...kids) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) if (k != null && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
};
const asNode = (x) => (x == null ? null : x.nodeType ? x : el("div", "cmx-text", String(x)));

/** Token classes (`tok-*`, coloured by editor.css from the theme), the same for the editor and `highlight`. */
const tokens = tagHighlighter([
  { tag: t.keyword, class: "tok-kw" },
  { tag: [t.bool, t.null], class: "tok-atom" },
  { tag: t.number, class: "tok-num" },
  { tag: [t.string, t.special(t.string)], class: "tok-str" },
  { tag: t.escape, class: "tok-esc" },
  { tag: t.comment, class: "tok-cm" },
  { tag: [t.function(t.variableName), t.function(t.propertyName)], class: "tok-fn" },
  { tag: t.function(t.definition(t.variableName)), class: "tok-def" },
  { tag: [t.className, t.definition(t.className)], class: "tok-type" },
  { tag: t.propertyName, class: "tok-prop" },
  { tag: t.meta, class: "tok-meta" },
  { tag: t.invalid, class: "tok-err" },
]);

// CodeMirror's own parts in layr's colours. Only CSS variables, so a theme change needs no reload.
const SEL = "color-mix(in srgb, var(--accent) 30%, transparent)";
const theme = EditorView.theme({
  "&": { color: "var(--fg)", backgroundColor: "var(--bg)", fontSize: "12.5px" },
  "&.cm-editor.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.6" },
  ".cm-content": { caretColor: "var(--fg)", padding: "4px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--fg)", borderLeftWidth: "1.5px" },
  "&.cm-editor .cm-selectionBackground": { backgroundColor: SEL },
  "&.cm-editor.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground":
    { backgroundColor: SEL },
  ".cm-content ::selection": { backgroundColor: SEL },
  ".cm-gutters": { backgroundColor: "var(--bg)", color: "var(--fg-dim)", border: "none" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 6px 0 10px", minWidth: "28px", opacity: "0.7" },
  ".cm-activeLine": { backgroundColor: "var(--cmx-active)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--cmx-active)", color: "var(--fg)" },
  "&.cm-editor .cm-matchingBracket, &.cm-editor.cm-focused .cm-matchingBracket": {
    backgroundColor: "color-mix(in srgb, var(--accent) 22%, transparent)",
    outline: "1px solid " + SEL,
    color: "inherit",
  },
  "&.cm-editor .cm-nonmatchingBracket": { color: "var(--danger)", backgroundColor: "transparent" },
  ".cm-searchMatch": { backgroundColor: "color-mix(in srgb, var(--warning) 30%, transparent)", outline: "none" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "color-mix(in srgb, var(--warning) 60%, transparent)" },
  ".cm-selectionMatch": { backgroundColor: "color-mix(in srgb, var(--accent) 14%, transparent)" },
  ".cm-foldGutter .cm-gutterElement": { color: "var(--fg-dim)", padding: "0 3px", cursor: "pointer" },
  ".cm-foldPlaceholder": {
    backgroundColor: "var(--bg-3)",
    border: "1px solid var(--border)",
    color: "var(--fg-dim)",
    borderRadius: "4px",
    padding: "0 5px",
    margin: "0 3px",
  },
  ".cm-tooltip": {
    backgroundColor: "var(--bg-2)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    borderRadius: "6px",
    boxShadow: "var(--shadow)",
    overflow: "hidden",
  },
  ".cm-tooltip .cm-tooltip-arrow:before": { borderTopColor: "var(--border)", borderBottomColor: "var(--border)" },
  ".cm-tooltip .cm-tooltip-arrow:after": { borderTopColor: "var(--bg-2)", borderBottomColor: "var(--bg-2)" },
  ".cm-tooltip-autocomplete > ul": { fontFamily: "var(--font-mono)", maxHeight: "16em" },
  ".cm-tooltip-autocomplete > ul > li": { padding: "1px 8px 1px 4px", lineHeight: "1.55" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--accent)", color: "var(--accent-fg)" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected] .cm-completionDetail": { color: "inherit", opacity: "0.8" },
  ".cm-completionDetail": { color: "var(--fg-dim)", fontStyle: "normal", marginLeft: "1.2em" },
  ".cm-completionMatchedText": { textDecoration: "none", fontWeight: "700" },
  ".cm-completionIcon": { opacity: "0.75", width: "1.1em", paddingRight: "0.5em" },
  ".cm-completionInfo": { padding: "6px 9px", maxWidth: "420px", whiteSpace: "pre-wrap", fontSize: "12px" },
  ".cm-tooltip-lint": { padding: "0" },
  ".cm-diagnostic": { padding: "4px 9px", borderLeftWidth: "3px" },
  ".cm-diagnostic-error": { borderLeftColor: "var(--danger)" },
  ".cm-diagnostic-warning": { borderLeftColor: "var(--warning)" },
  ".cm-diagnostic-info": { borderLeftColor: "var(--accent)" },
  ".cm-lintRange-error": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--danger)",
    textDecorationSkipInk: "none",
    textUnderlineOffset: "3px",
  },
  ".cm-lintRange-warning": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--warning)",
    textDecorationSkipInk: "none",
    textUnderlineOffset: "3px",
  },
  ".cm-lintRange-info": {
    backgroundImage: "none",
    textDecoration: "underline dotted var(--accent)",
    textUnderlineOffset: "3px",
  },
  ".cm-panels": { backgroundColor: "var(--bg-2)", color: "var(--fg)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--border)" },
  ".cm-panel.cm-search": { padding: "6px 30px 6px 8px", fontFamily: "var(--font)", fontSize: "12px" },
  ".cm-panel.cm-search label": { fontSize: "12px", color: "var(--fg-dim)" },
  ".cm-panel.cm-search [name=close]": { color: "var(--fg-dim)", fontSize: "18px", right: "8px", top: "4px" },
  ".cm-textfield": {
    backgroundColor: "var(--bg)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    borderRadius: "4px",
    padding: "2px 6px",
    fontSize: "12px",
  },
  ".cm-textfield:focus": { borderColor: "var(--accent)", outline: "none" },
  ".cm-button": {
    backgroundImage: "none",
    backgroundColor: "var(--bg-3)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    borderRadius: "4px",
    padding: "2px 8px",
    fontSize: "12px",
  },
  ".cm-button:hover": { borderColor: "var(--accent)" },
  ".cm-button:active": { backgroundImage: "none", backgroundColor: "var(--bg)" },
});

const pos2lc = (state, pos) => {
  const line = state.doc.lineAt(pos);
  return { line: line.number, column: pos - line.from };
};
const lineOf = (state, n) => state.doc.line(Math.min(Math.max(1, Math.round(n) || 1), state.doc.lines));

/** The common setup of every editor here: Python, the look, 4-space indent. */
const pythonBase = () => [
  python(),
  syntaxHighlighting(tokens),
  theme,
  indentUnit.of("    "),
  EditorState.tabSize.of(4),
  EditorView.editorAttributes.of({ class: "cmx" }),
];
const readOnlyExt = (on) => (on ? [EditorState.readOnly.of(true), EditorView.editable.of(false)] : []);

// -------------------------------------------------------------------------------- the hover card

/**
 * One floating card per editor: shown near an element, filled by `make()` (an element, a string, or a promise of
 * one), open while the pointer is on the element or on the card.
 */
function cardManager() {
  let card = null,
    anchor = null,
    showTimer = 0,
    hideTimer = 0,
    ticket = 0;
  const ensure = () => {
    if (card) return card;
    card = el("div", "cmx-card");
    card.addEventListener("pointerenter", () => clearTimeout(hideTimer));
    card.addEventListener("pointerleave", (e) => e.pointerType === "mouse" && hideSoon());
    document.body.append(card);
    return card;
  };
  const place = () => {
    if (!card || !anchor || !anchor.isConnected) return hide();
    const r = anchor.getBoundingClientRect();
    const w = card.offsetWidth,
      h = card.offsetHeight,
      vw = innerWidth,
      vh = innerHeight;
    let top = r.bottom + 6;
    if (top + h > vh - 6 && r.top - h - 6 > 6) top = r.top - h - 6;
    top = Math.max(6, Math.min(top, vh - h - 6));
    const left = Math.max(6, Math.min(r.right - w, vw - w - 6, Math.max(r.left, 6)));
    card.style.left = `${Math.max(6, left)}px`;
    card.style.top = `${top}px`;
  };
  const open = async (target, make) => {
    clearTimeout(hideTimer);
    if (anchor === target && card?.classList.contains("open")) return;
    anchor = target;
    const mine = ++ticket;
    const c = ensure();
    c.replaceChildren(el("div", "cmx-card-wait", "…"));
    c.classList.add("open");
    place();
    let body;
    try {
      body = await make();
    } catch (err) {
      body = el("div", "cmx-text", `Could not show this: ${err?.message || err}`);
    }
    if (mine !== ticket) return;
    if (!body) return hide();
    c.replaceChildren(asNode(body));
    place();
  };
  const hide = () => {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    ticket++;
    anchor = null;
    card?.classList.remove("open");
  };
  const hideSoon = (ms = 220) => {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, ms);
  };
  /** Shows `make()` when the pointer rests on `target`; a tap opens and closes it on touch screens. */
  const attach = (target, make) => {
    target.addEventListener("pointerenter", (e) => {
      if (e.pointerType !== "mouse") return;
      clearTimeout(hideTimer);
      clearTimeout(showTimer);
      showTimer = setTimeout(() => open(target, make), card?.classList.contains("open") ? 60 : 280);
    });
    target.addEventListener("pointerleave", (e) => e.pointerType === "mouse" && hideSoon());
    target.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse") return;
      if (anchor === target && card?.classList.contains("open")) hide();
      else open(target, make);
    });
  };
  /** Hides the card if it is showing (a scroll moves the chip away from it); a card about to show still shows. */
  const hideOpen = () => card?.classList.contains("open") && hide();
  return { open, hide, hideOpen, hideSoon, attach, destroy: () => (hide(), card?.remove(), (card = null)) };
}

// -------------------------------------------------------------------------------- values at line ends

/** Whether a transaction deleted the whole line `{from, to}` together with a line break next to it. */
const lineRemoved = (tr, { from, to }) =>
  (from > 0 && tr.changes.touchesRange(from - 1, to) === "cover") ||
  (to < tr.startState.doc.length && tr.changes.touchesRange(from, to + 1) === "cover");

const setValuesFx = StateEffect.define();
const DOT_TYPES = new Set(["table", "flags", "data", "figure"]);

class ChipsWidget extends WidgetType {
  constructor(entry, stale, ctx) {
    super();
    this.entry = entry;
    this.stale = stale;
    this.ctx = ctx;
  }
  eq(other) {
    return other.entry.chips === this.entry.chips && other.stale === this.stale;
  }
  toDOM() {
    const box = el("span", "cmx-values" + (this.stale ? " stale" : ""));
    box.setAttribute("aria-hidden", "true");
    for (const c of this.entry.chips) {
      const type = DOT_TYPES.has(c.type) ? c.type : "none";
      const label =
        c.name === "return" ? `→ ${c.text ?? ""}` : c.name ? `${c.name} = ${c.text ?? ""}` : String(c.text ?? "");
      const chip = el(
        "span",
        `cmx-chip t-${type}` + (c.card || c.onClick ? " live" : ""),
        type !== "none" ? el("i", "cmx-dot") : null,
        el("span", "cmx-chip-text", label),
      );
      chip.title = this.stale ? "From before your change: run it again to update" : c.card ? "" : c.title || "";
      if (c.card) {
        this.ctx.cards.attach(chip, async () => {
          const body = await c.card();
          const head = el("div", "cmx-card-head", c.title || label);
          if (this.stale) head.append(el("span", "cmx-card-stale", "from before your change"));
          return el("div", "", head, asNode(body));
        });
      }
      if (c.onClick) {
        chip.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          this.ctx.cards.hide();
          c.onClick(e);
        });
      }
      box.append(chip);
    }
    return box;
  }
  ignoreEvent() {
    return true;
  }
}

/** Values keyed by the line's start; after an edit of a line its chips grey out ("from before your change"). */
const valuesField = (ctx) =>
  StateField.define({
    create: () => ({ entries: [], deco: Decoration.none }),
    update(v, tr) {
      let entries = v.entries;
      let changed = false;
      for (const fx of tr.effects) {
        if (!fx.is(setValuesFx)) continue;
        changed = true;
        entries = [];
        for (const [key, chips] of Object.entries(fx.value || {})) {
          const n = Number(key);
          if (!chips?.length || !(n >= 1 && n <= tr.state.doc.lines)) continue;
          const line = tr.state.doc.line(n);
          entries.push({ from: line.from, to: line.to, text: line.text, chips });
        }
      }
      if (!changed && tr.docChanged) {
        changed = true;
        const next = [];
        for (const e of entries) {
          if (lineRemoved(tr, e)) continue;
          const from = tr.changes.mapPos(e.from, 1);
          const line = tr.state.doc.lineAt(from);
          next.push({ ...e, from: line.from, to: line.to, now: line.text });
        }
        entries = next;
      }
      if (!changed) return v;
      const seen = new Set();
      const ranges = [];
      for (const e of entries.slice().sort((a, b) => a.from - b.from)) {
        if (seen.has(e.from)) continue;
        seen.add(e.from);
        const stale = e.now != null && e.now !== e.text;
        ranges.push(Decoration.widget({ widget: new ChipsWidget(e, stale, ctx), side: 1 }).range(e.to));
      }
      return { entries, deco: Decoration.set(ranges, true) };
    },
    provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
  });

// -------------------------------------------------------------------------------- folded checks

const setFoldsFx = StateEffect.define();
const openFoldFx = StateEffect.define(); // { index, open }

class PillWidget extends WidgetType {
  constructor(label, index, plain) {
    super();
    this.label = label;
    this.index = index;
    this.plain = plain;
  }
  eq(other) {
    return other.label === this.label && other.index === this.index && other.plain === this.plain;
  }
  toDOM(view) {
    const pill = this.plain
      ? el("span", "cmx-guard plain", this.label)
      : el("span", "cmx-guard", el("span", "cmx-guard-icon", "⚠"), this.label);
    pill.title = "Folded: click to show the code";
    pill.addEventListener("mousedown", (e) => {
      e.preventDefault();
      view.dispatch({ effects: openFoldFx.of({ index: this.index, open: true }) });
    });
    return pill;
  }
  ignoreEvent() {
    return true;
  }
}

const foldsField = StateField.define({
  create: () => ({ folds: [], deco: Decoration.none }),
  update(v, tr) {
    let folds = v.folds;
    let changed = false;
    for (const fx of tr.effects) {
      if (fx.is(setFoldsFx)) {
        changed = true;
        folds = [];
        for (const f of fx.value || []) {
          const a = Number(f.from),
            b = Number(f.to ?? f.from);
          if (!(a >= 1 && b >= a && b <= tr.state.doc.lines)) continue;
          const first = tr.state.doc.line(a),
            last = tr.state.doc.line(b);
          folds.push({
            from: first.from,
            to: last.to,
            label: String(f.label ?? `${b - a + 1} lines`),
            open: !!f.open,
            plain: f.kind === "text",
          });
        }
      } else if (fx.is(openFoldFx)) {
        changed = true;
        folds = folds.map((f, i) =>
          fx.value.index == null || fx.value.index === i ? { ...f, open: fx.value.open } : f,
        );
      }
    }
    if (tr.docChanged) {
      changed = true;
      folds = folds
        .map((f) => ({ ...f, from: tr.changes.mapPos(f.from, 1), to: tr.changes.mapPos(f.to, -1) }))
        .filter((f) => f.to > f.from);
    }
    if (!changed) return v;
    const ranges = [];
    folds.forEach((f, i) => {
      if (f.open) return;
      const first = tr.state.doc.lineAt(f.from);
      const start = first.from + /^\s*/.exec(first.text)[0].length;
      if (f.to > start)
        ranges.push(Decoration.replace({ widget: new PillWidget(f.label, i, f.plain) }).range(start, f.to));
    });
    return { folds, deco: Decoration.set(ranges, true) };
  },
  provide: (f) => [
    EditorView.decorations.from(f, (v) => v.deco),
    EditorView.atomicRanges.of((view) => view.state.field(f).deco),
  ],
});

// -------------------------------------------------------------------------------- blocks under lines

const setBelowFx = StateEffect.define();

class BelowWidget extends WidgetType {
  constructor(node, indent) {
    super();
    this.node = node;
    this.indent = indent;
  }
  eq(other) {
    return other.node === this.node && other.indent === this.indent;
  }
  toDOM(view) {
    // Indented like its line: the padding is in the code's own characters (the box's font is the editor's).
    const box = el("div", "cmx-below");
    box.style.paddingLeft = `${this.indent}ch`;
    box.append(el("div", "cmx-below-body", this.node));
    if (typeof ResizeObserver === "function") {
      box._ro = new ResizeObserver(() => view.requestMeasure());
      box._ro.observe(box);
    }
    return box;
  }
  destroy(dom) {
    dom._ro?.disconnect();
  }
  ignoreEvent() {
    return true;
  }
  get estimatedHeight() {
    return 60;
  }
}

const belowField = StateField.define({
  create: () => ({ items: [], deco: Decoration.none }),
  update(v, tr) {
    let items = v.items;
    let changed = false;
    for (const fx of tr.effects) {
      if (!fx.is(setBelowFx)) continue;
      changed = true;
      items = [];
      for (const [key, node] of Object.entries(fx.value || {})) {
        const n = Number(key);
        if (!node || !(n >= 1 && n <= tr.state.doc.lines)) continue;
        const line = tr.state.doc.line(n);
        items.push({ from: line.from, to: line.to, node: asNode(node) });
      }
    }
    if (!changed && tr.docChanged) {
      changed = true;
      items = items
        .filter((x) => !lineRemoved(tr, x))
        .map((x) => {
          const line = tr.state.doc.lineAt(tr.changes.mapPos(x.from, 1));
          return { ...x, from: line.from, to: line.to };
        });
    }
    if (!changed) return v;
    const ranges = items
      .slice()
      .sort((a, b) => a.to - b.to)
      .map((x) => {
        const indent = /^\s*/.exec(tr.state.doc.lineAt(x.to).text)[0].length;
        return Decoration.widget({ widget: new BelowWidget(x.node, indent), block: true, side: 1 }).range(x.to);
      });
    return { items, deco: Decoration.set(ranges, true) };
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
});

// -------------------------------------------------------------------------------- a short flash

const flashFx = StateEffect.define();
const flashField = StateField.define({
  create: () => Decoration.none,
  update(v, tr) {
    v = v.map(tr.changes);
    for (const fx of tr.effects) {
      if (fx.is(flashFx)) {
        v =
          fx.value == null
            ? Decoration.none
            : Decoration.set([Decoration.line({ class: "cmx-flash" }).range(fx.value)]);
      }
    }
    return v;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// -------------------------------------------------------------------------------- call signatures

const setSigFx = StateEffect.define();
const sigField = StateField.define({
  create: () => null,
  update(v, tr) {
    for (const fx of tr.effects) if (fx.is(setSigFx)) v = fx.value;
    if (v && tr.docChanged) v = { ...v, pos: tr.changes.mapPos(v.pos) };
    return v;
  },
  provide: (f) =>
    showTooltip.from(f, (v) =>
      v
        ? {
            pos: v.pos,
            above: true,
            arrow: false,
            create: () => ({ dom: signatureDom(v) }),
          }
        : null,
    ),
});

/** The parameters between the outer parentheses of a signature's label, split at its top-level commas. */
function splitParams(label) {
  const open = label.indexOf("("),
    close = label.lastIndexOf(")");
  if (open < 0 || close < open) return null;
  const parts = [];
  let depth = 0,
    start = open + 1;
  for (let i = open + 1; i < close; i++) {
    const ch = label[i];
    if ("([{".includes(ch)) depth++;
    else if (")]}".includes(ch)) depth--;
    else if (ch === "," && depth === 0) {
      parts.push([start, i]);
      start = i + 1;
    }
  }
  if (close > start) parts.push([start, close]);
  return parts;
}

function signatureDom(sig) {
  const box = el("div", "cmx-signature");
  const label = String(sig.label || "");
  const parts = splitParams(label);
  const line = el("div", "cmx-signature-label");
  const active = typeof sig.active === "number" && parts ? parts[sig.active] : null;
  if (active) {
    let [a, b] = active;
    while (label[a] === " ") a++;
    line.append(label.slice(0, a), el("b", "", label.slice(a, b)), label.slice(b));
  } else line.append(label);
  box.append(line);
  if (sig.doc) box.append(el("div", "cmx-signature-doc", String(sig.doc)));
  return box;
}

/** Where the call whose arguments the cursor is in opens (the position of its `(`), or -1. */
function openCallAt(state, pos) {
  const from = Math.max(0, pos - 3000);
  const text = state.sliceDoc(from, pos);
  let depth = 0;
  for (let i = text.length - 1; i >= 0; i--) {
    const ch = text[i];
    if (ch === ")" || ch === "]" || ch === "}") depth++;
    else if (ch === "[" || ch === "{") {
      if (depth === 0) return -1;
      depth--;
    } else if (ch === "(") {
      if (depth === 0) return /[\w)\]]\s*$/.test(text.slice(Math.max(0, i - 80), i)) ? from + i : -1;
      depth--;
    } else if (ch === "\n" && /^\s*(def|class)\s/.test(text.slice(i + 1))) return -1;
  }
  return -1;
}

// -------------------------------------------------------------------------------- completion

const KINDS = {
  function: "function",
  method: "method",
  class: "class",
  module: "namespace",
  keyword: "keyword",
  instance: "variable",
  statement: "variable",
  param: "variable",
  variable: "variable",
  property: "property",
  column: "property",
  path: "text",
  constant: "constant",
  type: "type",
};

function completionSource(complete) {
  return async (ctx) => {
    const tree = syntaxTree(ctx.state).resolveInner(ctx.pos, -1);
    if (!ctx.explicit && tree.name === "Comment") return null;
    const before = ctx.state.sliceDoc(Math.max(0, ctx.pos - 200), ctx.pos);
    const inKey = /\[\s*["']([^"'\]\n]*)$/.exec(before);
    const word = /[A-Za-z_]\w*$/.exec(before);
    if (!ctx.explicit && !inKey && !word && !/\.$/.test(before)) return null;
    const from = inKey ? ctx.pos - inKey[1].length : word ? ctx.pos - word[0].length : ctx.pos;
    const { line, column } = pos2lc(ctx.state, ctx.pos);
    let items;
    try {
      items = await complete({ source: ctx.state.doc.toString(), line, column, explicit: ctx.explicit });
    } catch {
      return null;
    }
    if (ctx.aborted || !items?.length) return null;
    return {
      from,
      validFor: inKey ? /^[^"'\]\n]*$/ : /^\w*$/,
      options: items.map((x) => ({
        label: x.label,
        type: KINDS[x.kind] || x.kind || undefined,
        detail: x.detail || undefined,
        info: x.doc ? (x.doc.nodeType ? () => x.doc : String(x.doc)) : undefined,
        boost: x.boost,
        apply: x.apply,
      })),
    };
  };
}

// -------------------------------------------------------------------------------- the editor

/**
 * Puts the values of a line on their own row under it when the line's code and its values don't fit in the editor's
 * width together (else they would cover the end of the code). Measured after each layout; the choice depends only on
 * where the code ends and how wide the values are, so it doesn't flip back and forth.
 */
function placeChips(view) {
  view.requestMeasure({
    key: placeChips,
    read(v) {
      const box = v.scrollDOM.getBoundingClientRect();
      const right = box.left + v.scrollDOM.clientWidth - 6;
      const out = [];
      for (const w of v.contentDOM.querySelectorAll(".cmx-values")) {
        const chips = w.querySelectorAll(".cmx-chip");
        if (!chips.length) continue;
        let line;
        try {
          line = v.state.doc.lineAt(v.posAtDOM(w));
        } catch {
          continue;
        }
        const end = v.coordsAtPos(line.to, -1);
        const a = chips[0].getBoundingClientRect(),
          b = chips[chips.length - 1].getBoundingClientRect();
        const indent = /^\s*/.exec(line.text)[0].length;
        out.push({ w, below: !!end && end.right + 40 + (b.right - a.left) > right, indent });
      }
      return out;
    },
    write(list, v) {
      let moved = false;
      for (const { w, below, indent } of list) {
        moved ||= w.classList.contains("below") !== below;
        w.classList.toggle("below", below);
        w.style.paddingLeft = below ? `calc(${indent}ch + 2px)` : "";
      }
      // lines got taller or shorter (the editor became narrower or wider): their heights are measured again, so the
      // line numbers in the gutter stay beside their lines (CodeMirror skips a size change right after an update,
      // and this one always is: its own flag for "measure the lines" is set)
      if (moved) {
        if (v.viewState) v.viewState.mustMeasureContent = true;
        v.requestMeasure();
      }
    },
  });
}

const external = Annotation.define();

/** Tab: indent the selected lines, or insert spaces to the next multiple of 4 at the cursor. */
function tabKey(view) {
  if (view.state.readOnly) return false;
  if (view.state.selection.ranges.some((r) => !r.empty)) return indentMore(view);
  view.dispatch(
    view.state.changeByRange((r) => {
      const n = 4 - ((r.head - view.state.doc.lineAt(r.head).from) % 4);
      return { changes: { from: r.head, insert: " ".repeat(n) }, range: EditorSelection.cursor(r.head + n) };
    }),
    { userEvent: "input.indent", scrollIntoView: true },
  );
  return true;
}

/**
 * A Python editor in `parent`.
 *
 * Options (all optional):
 * - `doc`: the text.
 * - `readOnly`: true, or "touch" for read-only on a touch-only device.
 * - `onChange(text)`: after every edit by the user (not after `setValue`).
 * - `onSave()`: Ctrl+S / Cmd+S.
 * - `onCursor({line, column})`: when the cursor moves.
 * - `complete({source, line, column, explicit})` → promise of `[{label, kind, detail, doc, boost, apply}]`;
 *   `kind`: function, class, module, keyword, instance, statement, param, property, column, path.
 * - `hover({source, line, column})` → promise of an element (or text), or null.
 * - `signature({source, line, column})` → promise of `{label, active, doc}` or null, shown while typing a call's
 *   arguments; `active` is the index of the parameter being typed, made bold in `label`.
 * - `onDefinition({source, line, column})`: F12, or Ctrl-click (Cmd-click) on a name: show where it is defined.
 * - `onRename({source, line, column})`: F2 (not when read-only): rename the name at the cursor.
 * - `firstLine`: the number shown for the first line (the code of one function, numbered as in its file).
 * - `chipsBelow`: values that don't fit at the end of their line go on their own row under it (a narrow editor).
 */
export function createEditor(parent, opts = {}) {
  const ctx = { cards: cardManager() };
  const readOnlyBox = new Compartment();
  let readOnly = opts.readOnly === "touch" ? touchOnly() : !!opts.readOnly;
  const values = valuesField(ctx);
  const firstLine = Math.round(opts.firstLine) || 1;
  let sigTimer = 0,
    sigTicket = 0;

  const askSignature = (view) => {
    clearTimeout(sigTimer);
    const state = view.state;
    const sel = state.selection.main;
    const open = sel.empty ? openCallAt(state, sel.head) : -1;
    if (open < 0) {
      if (state.field(sigField)) view.dispatch({ effects: setSigFx.of(null) });
      return;
    }
    const mine = ++sigTicket;
    sigTimer = setTimeout(async () => {
      const { line, column } = pos2lc(state, sel.head);
      let sig = null;
      try {
        sig = await opts.signature({ source: state.doc.toString(), line, column });
      } catch {
        sig = null;
      }
      if (mine !== sigTicket || view.state.doc !== state.doc) return;
      view.dispatch({ effects: setSigFx.of(sig && sig.label ? { ...sig, pos: open } : null) });
    }, 150);
  };

  const extensions = () => [
    lintGutter(),
    firstLine !== 1 ? lineNumbers({ formatNumber: (n) => String(n + firstLine - 1) }) : lineNumbers(),
    foldGutter({ openText: "⌄", closedText: "›" }),
    codeFolding(),
    highlightActiveLineGutter(),
    highlightSpecialChars(),
    history(),
    drawSelection(),
    dropCursor(),
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    rectangularSelection(),
    highlightActiveLine(),
    highlightSelectionMatches(),
    autocompletion({
      override: opts.complete ? [completionSource(opts.complete)] : undefined,
      icons: true,
      activateOnTypingDelay: 80,
    }),
    opts.hover
      ? hoverTooltip(
          async (view, pos) => {
            const word = view.state.wordAt(pos);
            if (!word) return null;
            const { line, column } = pos2lc(view.state, pos);
            let body;
            try {
              body = await opts.hover({ source: view.state.doc.toString(), line, column });
            } catch {
              return null;
            }
            if (!body) return null;
            return {
              pos: word.from,
              end: word.to,
              above: true,
              create: () => ({ dom: el("div", "cmx-hover", asNode(body)) }),
            };
          },
          { hoverTime: 350 },
        )
      : [],
    opts.signature
      ? [
          sigField,
          EditorView.updateListener.of((u) => {
            if (u.docChanged || u.selectionSet) askSignature(u.view);
          }),
        ]
      : [],
    Prec.high(
      keymap.of([
        { key: "Mod-s", preventDefault: true, run: () => (opts.onSave?.(), true) },
        {
          key: "F12",
          preventDefault: true,
          run: (view) => (opts.onDefinition ? (opts.onDefinition(where(view)), true) : false),
        },
        {
          key: "F2",
          preventDefault: true,
          run: (view) => (opts.onRename && !readOnly ? (opts.onRename(where(view)), true) : false),
        },
        {
          key: "Escape",
          run: (view) => {
            if (!opts.signature || !view.state.field(sigField, false)) return false;
            view.dispatch({ effects: setSigFx.of(null) });
            return true;
          },
        },
      ]),
    ),
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...searchKeymap,
      ...historyKeymap,
      ...foldKeymap,
      ...completionKeymap,
      { key: "Tab", run: tabKey, shift: indentLess },
    ]),
    pythonBase(),
    readOnlyBox.of(readOnlyExt(readOnly)),
    values,
    foldsField,
    belowField,
    flashField,
    EditorView.updateListener.of((u) => {
      if (u.docChanged && opts.onChange && !u.transactions.every((tr) => tr.annotation(external)))
        opts.onChange(u.state.doc.toString());
      if ((u.selectionSet || u.docChanged) && opts.onCursor)
        opts.onCursor(pos2lc(u.state, u.state.selection.main.head));
      if (u.docChanged) ctx.cards.hide();
    }),
    opts.chipsBelow
      ? EditorView.updateListener.of((u) => {
          if (u.docChanged || u.viewportChanged || u.geometryChanged || u.transactions.some((t) => t.effects.length))
            placeChips(u.view);
        })
      : [],
    EditorView.domEventHandlers({
      scroll: () => ctx.cards.hideOpen(),
      mousedown: (e, view) => {
        // Ctrl-click (Cmd-click on a Mac) on a name: go to where it is defined (instead of adding a cursor).
        if (!opts.onDefinition || e.button !== 0 || !(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return false;
        const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
        if (pos == null || !view.state.wordAt(pos)) return false;
        e.preventDefault();
        view.dispatch({ selection: { anchor: pos } });
        opts.onDefinition({ source: view.state.doc.toString(), ...pos2lc(view.state, pos) });
        return true;
      },
    }),
  ];
  const where = (view) => ({
    source: view.state.doc.toString(),
    ...pos2lc(view.state, view.state.selection.main.head),
  });

  const view = new EditorView({ parent, state: EditorState.create({ doc: opts.doc ?? "", extensions: extensions() }) });
  let flashTimer = 0;

  const api = {
    view,
    /** The text as it stands. */
    getValue: () => view.state.doc.toString(),
    /**
     * Replaces the text. With `{keepHistory: true}` it is one undoable edit (only the part that differs changes, so
     * the cursor, values and folds on untouched lines stay); else a fresh document without undo history, values,
     * folds, blocks or diagnostics. Never calls `onChange`.
     */
    setValue(text, { keepHistory = false } = {}) {
      text = String(text ?? "");
      const old = view.state.doc.toString();
      if (text === old) return;
      if (!keepHistory) {
        view.setState(EditorState.create({ doc: text, extensions: extensions() }));
        return;
      }
      let a = 0;
      const max = Math.min(old.length, text.length);
      while (a < max && old.charCodeAt(a) === text.charCodeAt(a)) a++;
      let b = 0;
      while (b < max - a && old.charCodeAt(old.length - 1 - b) === text.charCodeAt(text.length - 1 - b)) b++;
      view.dispatch({
        changes: { from: a, to: old.length - b, insert: text.slice(a, text.length - b) },
        annotations: external.of(true),
        userEvent: "set",
      });
    },
    /** Marks in the gutter and under the text: `[{line, level: error|warning|info, message, column?, endColumn?}]`. */
    setDiagnostics(list) {
      const { state } = view;
      const sev = { error: "error", warning: "warning", style: "warning", info: "info", note: "info", hint: "hint" };
      const out = [];
      for (const d of list || []) {
        if (!(d.line >= 1 && d.line <= state.doc.lines)) continue;
        const line = state.doc.line(d.line);
        const from = line.from + Math.min(line.length, d.column ?? /^\s*/.exec(line.text)[0].length);
        let to = d.endColumn != null ? line.from + Math.min(line.length, d.endColumn) : line.to;
        if (to < from) to = from;
        out.push({ from, to, severity: sev[d.level] || "warning", message: String(d.message ?? ""), source: d.source });
      }
      view.dispatch(cm.setDiagnostics(state, out));
    },
    /**
     * Values at the end of lines: `{line: [{name, text, type, title, card, onClick}]}` (replaces all). `type`:
     * table, flags, data, figure or none (the dot's colour); `card()` gives the hover card's content (an element,
     * text, or a promise of one); `onClick(event)` on a click.
     */
    setValues(map) {
      view.dispatch({ effects: setValuesFx.of(map || {}) });
    },
    /**
     * Ranges of lines shown folded as a pill with `label`: `[{from, to, label, open?, kind?}]` (replaces all).
     * `kind: "text"` (a description) gets a plain pill; the others look like a check (⚠).
     */
    setFolds(list) {
      view.dispatch({ effects: setFoldsFx.of(list || []) });
    },
    /** Opens (`true`) or folds (`false`) every range of `setFolds`; with no argument, the opposite of now. */
    toggleFolds(open) {
      const folds = view.state.field(foldsField).folds;
      if (open == null) open = folds.some((f) => !f.open);
      view.dispatch({ effects: openFoldFx.of({ index: null, open: !!open }) });
      return open;
    },
    /** The ranges of `setFolds` as they stand: `[{from, to, label, open}]` in lines. */
    folds: () =>
      view.state.field(foldsField).folds.map((f) => ({
        from: view.state.doc.lineAt(f.from).number,
        to: view.state.doc.lineAt(f.to).number,
        label: f.label,
        open: f.open,
      })),
    /** Blocks under lines: `{line: element}` (replaces all; an element is moved into the editor). */
    setBelow(map) {
      view.dispatch({ effects: setBelowFx.of(map || {}) });
    },
    /** Scrolls a line into the middle, puts the cursor at its start (`{select: false}`: leaves it) and flashes it. */
    reveal(line, { select = true } = {}) {
      const l = lineOf(view.state, line);
      const folds = view.state.field(foldsField).folds;
      const effects = [EditorView.scrollIntoView(l.from, { y: "center" }), flashFx.of(l.from)];
      folds.forEach(
        (f, i) =>
          !f.open && f.from <= l.from && l.from <= f.to && effects.push(openFoldFx.of({ index: i, open: true })),
      );
      view.dispatch({ effects, selection: select ? { anchor: l.from } : undefined });
      clearTimeout(flashTimer);
      flashTimer = setTimeout(() => view.dispatch({ effects: flashFx.of(null) }), 1400);
    },
    /** Where the cursor is: `{line, column}`. */
    cursor: () => pos2lc(view.state, view.state.selection.main.head),
    /** The selected text ("" when nothing is selected). */
    selection: () => view.state.sliceDoc(view.state.selection.main.from, view.state.selection.main.to),
    focus: () => view.focus(),
    setReadOnly(on) {
      readOnly = !!on;
      view.dispatch({ effects: readOnlyBox.reconfigure(readOnlyExt(readOnly)) });
    },
    destroy() {
      clearTimeout(sigTimer);
      clearTimeout(flashTimer);
      ctx.cards.destroy();
      view.destroy();
    },
  };
  return api;
}

// -------------------------------------------------------------------------------- the merge view

/** The editor parts both sides of a merge view share. */
const mergeBase = (readOnly) => [
  lineNumbers(),
  highlightSpecialChars(),
  drawSelection(),
  history(),
  keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, { key: "Tab", run: tabKey, shift: indentLess }]),
  pythonBase(),
  readOnlyExt(readOnly),
];

/** Copies a chunk's lines from one editor over the matching lines of the other (as MergeView's revert does). */
function copyChunk(src, sFrom, sTo, dest, dFrom, dTo, userEvent) {
  let insert = src.state.sliceDoc(sFrom, Math.max(sFrom, sTo - 1));
  if (sFrom !== sTo && dTo <= dest.state.doc.length) insert += src.state.lineBreak;
  dest.dispatch({ changes: { from: dFrom, to: Math.min(dest.state.doc.length, dTo), insert }, userEvent });
}

class ChunkButtons extends GutterMarker {
  toDOM() {
    const box = el("span", "cmx-chunk-buttons");
    const ok = el("button", "cmx-chunk-accept", "✓");
    ok.dataset.act = "accept";
    ok.title = "Accept this change";
    const no = el("button", "cmx-chunk-reject", "✕");
    no.dataset.act = "reject";
    no.title = "Reject this change (keep what was there)";
    box.append(ok, no);
    return box;
  }
}
const chunkButtons = new ChunkButtons();

/**
 * Reviewing a draft: `before` (as it is) against `after` (the proposal), side by side on a wide panel and unified on
 * a narrow one (`sideBySide`: true, false or "auto", the default: 760 pixels or more). Each changed chunk has Accept
 * and Reject; an accepted chunk keeps the new text, a rejected one gets the old text back, both stop being shown as
 * a change. `getResult()` is the text as it stands: undecided chunks count as accepted. The result side can be edited
 * unless `readOnly` (then it only shows the changes, without Accept and Reject). `onChange({accepted, rejected, left})`
 * after every decision or edit.
 */
export function createMerge(parent, { before = "", after = "", sideBySide = "auto", readOnly = false, onChange } = {}) {
  const wrap = el("div", "cmx-merge");
  parent.append(wrap);
  const counts = { accepted: 0, rejected: 0 };
  let mode = null,
    mv = null,
    uv = null;
  let original = String(before),
    current = String(after);

  const chunks = () => (mv ? mv.chunks : uv ? getChunks(uv.state)?.chunks || [] : []);
  const state = () => ({ ...counts, left: chunks().length });
  const notify = () => onChange?.(state());
  const wantSide = () => (sideBySide === "auto" ? wrap.clientWidth >= 760 : !!sideBySide);
  const save = () => {
    if (mv) {
      original = mv.a.state.doc.toString();
      current = mv.b.state.doc.toString();
    } else if (uv) {
      original = getOriginalDoc(uv.state).toString();
      current = uv.state.doc.toString();
    }
  };
  const tellEdits = EditorView.updateListener.of((u) => {
    if (u.docChanged && u.transactions.some((tr) => tr.isUserEvent("input") || tr.isUserEvent("delete"))) notify();
  });

  const decide = (act, chunk) => {
    if (!mv || !chunk) return;
    if (act === "accept") copyChunk(mv.b, chunk.fromB, chunk.toB, mv.a, chunk.fromA, chunk.toA, "accept");
    else copyChunk(mv.a, chunk.fromA, chunk.toA, mv.b, chunk.fromB, chunk.toB, "revert");
    counts[act === "accept" ? "accepted" : "rejected"]++;
    notify();
  };

  const build = (keep = true) => {
    if (keep) save();
    mv?.destroy();
    uv?.destroy();
    mv = uv = null;
    wrap.replaceChildren();
    mode = wantSide() ? "side" : "unified";
    wrap.dataset.mode = mode;
    if (mode === "side") {
      const buttons = gutter({
        class: "cmx-chunk-gutter",
        lineMarker(view, line) {
          const len = view.state.doc.length;
          return (getChunks(view.state)?.chunks || []).some(
            (c) => view.state.doc.lineAt(Math.min(c.fromB, len)).from === line.from,
          )
            ? chunkButtons
            : null;
        },
        lineMarkerChange: (u) => u.docChanged || u.transactions.some((tr) => tr.effects.length),
        initialSpacer: () => chunkButtons,
        domEventHandlers: {
          mousedown(view, line, event) {
            const act = event.target.closest?.("[data-act]")?.dataset.act;
            if (!act) return false;
            event.preventDefault();
            const len = view.state.doc.length;
            decide(
              act,
              mv.chunks.find((c) => view.state.doc.lineAt(Math.min(c.fromB, len)).from === line.from),
            );
            return true;
          },
        },
      });
      mv = new MergeView({
        a: { doc: original, extensions: [mergeBase(true), EditorView.editorAttributes.of({ class: "cmx-before" })] },
        b: { doc: current, extensions: [readOnly ? [] : buttons, mergeBase(readOnly), tellEdits] },
        parent: wrap,
        gutter: true,
        highlightChanges: true,
        collapseUnchanged: { margin: 3, minSize: 8 },
      });
    } else {
      const control = (type, action) => {
        const b = el("button", `cmx-chunk-${type}`, type === "accept" ? "✓ Accept" : "✕ Reject");
        b.title = type === "accept" ? "Accept this change" : "Reject this change (keep what was there)";
        b.addEventListener("mousedown", (e) => {
          counts[type === "accept" ? "accepted" : "rejected"]++;
          action(e);
          notify();
        });
        return b;
      };
      uv = new EditorView({
        parent: wrap,
        state: EditorState.create({
          doc: current,
          extensions: [
            mergeBase(readOnly),
            unifiedMergeView({
              original,
              mergeControls: readOnly ? false : control,
              gutter: true,
              highlightChanges: true,
              syntaxHighlightDeletions: true,
              collapseUnchanged: { margin: 3, minSize: 8 },
            }),
            tellEdits,
          ],
        }),
      });
    }
  };
  build();

  let resizeTimer = 0;
  const ro =
    typeof ResizeObserver === "function" && sideBySide === "auto"
      ? new ResizeObserver(() => {
          clearTimeout(resizeTimer);
          resizeTimer = setTimeout(() => wantSide() !== (mode === "side") && build(), 120);
        })
      : null;
  ro?.observe(wrap);

  return {
    /** The text as it stands (undecided chunks as proposed). */
    getResult: () => (mv ? mv.b.state.doc.toString() : uv ? uv.state.doc.toString() : current),
    /** How many chunks were accepted. */
    accepted: () => counts.accepted,
    /** `{accepted, rejected, left}`: decisions so far and the chunks not decided yet. */
    counts: state,
    /** "side" or "unified". */
    mode: () => mode,
    /** Accepts every chunk not decided yet. */
    acceptAll() {
      save();
      counts.accepted += chunks().length;
      original = current;
      build(false);
      notify();
    },
    /** Rejects every chunk not decided yet. */
    rejectAll() {
      save();
      counts.rejected += chunks().length;
      current = original;
      build(false);
      notify();
    },
    /** The editors: `{a, b}` side by side (b is the result), `{b}` unified. */
    views: () => (mv ? { a: mv.a, b: mv.b } : { b: uv }),
    destroy() {
      ro?.disconnect();
      clearTimeout(resizeTimer);
      mv?.destroy();
      uv?.destroy();
      wrap.remove();
    },
  };
}

// -------------------------------------------------------------------------------- read-only snippets

/**
 * Python code as a highlighted `<pre>` with the editor's colours. `lineNumbers: true` numbers the lines from
 * `firstLine` (default 1).
 */
export function highlight(code, { lineNumbers: numbers = false, firstLine = 1 } = {}) {
  const text = String(code ?? "");
  const pre = el("pre", "cmx-highlight" + (numbers ? " numbered" : ""));
  let line = null,
    n = firstLine;
  const newLine = () => {
    line = numbers ? el("span", "cmx-hl-line", el("span", "cmx-hl-ln", String(n++))) : pre;
    if (numbers) pre.append(line);
  };
  newLine();
  try {
    highlightCode(
      text,
      pythonLanguage.parser.parse(text),
      tokens,
      (part, cls) => line.append(cls ? el("span", cls, part) : part),
      () => (numbers ? newLine() : pre.append("\n")),
    );
  } catch {
    pre.replaceChildren(text);
  }
  return pre;
}

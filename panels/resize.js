// Drag an edge to change a box's width or height: the one helper for every resizable place of the panels (the flow's
// side panel and the boxes in it, the outline and the explanation beside the code, the flow / code divider, the
// Results list, a review card's list, the rounds tables). A double click puts the usual size back; the size is
// remembered where the host keeps it, and a remembered size that doesn't fit the room there is now is ignored.
//
//   import { resizable } from "./resize.js";
//   const r = resizable(side, { edge: "left", min: 320, max: () => host.clientWidth - 240,
//                               size: { get: () => saved.side ?? null, set: (px) => (saved.side = px) } });
//
// Load flow.css with it (the .rz classes).

const live = new Set(); // every handle on the page: one window listener fits them all again after a resize

addEventListener("resize", () => {
  for (const r of live) {
    if (r.target.isConnected) r.refresh();
    else if (r.seen) live.delete(r); // drawn over by the host: forgotten
  }
});

const pick = (v) => (typeof v === "function" ? v() : v);

/**
 * Makes `target` resizable by dragging one of its edges → {handle, refresh(), reset(), destroy()}.
 * opts:
 *   edge: "left" | "right" (the width) or "top" | "bottom" (the height): the edge that is dragged.
 *   min, max: px, or functions → px (read at each drag and refresh: the room there is now). Default 80 and no limit.
 *   size: {get() → px or null, set(px or null)}: where the size is remembered (null: the usual size, from the CSS).
 *   apply(px or null): puts the size on the page (default: target's width or height; null clears it).
 *   measure() → px: the size now (default: target's offsetWidth or offsetHeight).
 *   enabled() → bool: false hides the handle and gives the usual size (e.g. a narrow screen stacks the boxes).
 *   handle: an element of the host to drag (e.g. a bar that also has buttons) instead of a new strip.
 *   ignore(event) → bool: a press on the handle that isn't a drag (a button in it).
 *   place: "inside" (an absolute strip on the edge, inside target; target must not scroll itself) or "after" (a thin
 *     bar under target, for a box that scrolls). Default: "after" for the bottom edge, else "inside".
 *   title: the handle's tooltip. onResize(px): while dragging. onDone(px or null): after a drag or a reset.
 */
export function resizable(target, opts = {}) {
  const edge = opts.edge || "right";
  const wide = edge === "left" || edge === "right";
  const sign = edge === "left" || edge === "top" ? -1 : 1;
  const min = () => pick(opts.min) ?? 80;
  const max = () => Math.max(min(), pick(opts.max) ?? Infinity);
  const enabled = () => (opts.enabled ? !!opts.enabled() : true);
  const apply =
    opts.apply ||
    ((px) => {
      target.style[wide ? "width" : "height"] = px == null ? "" : `${Math.round(px)}px`;
    });
  const measure = opts.measure || (() => (wide ? target.offsetWidth : target.offsetHeight));
  const place = opts.place || (edge === "bottom" ? "after" : "inside");
  const own = !opts.handle;
  const handle =
    opts.handle ||
    Object.assign(document.createElement("div"), {
      className: `rz rz-${edge}${place === "after" ? " rz-bar" : ""}`,
    });
  handle.classList.add("rz-handle", wide ? "rz-x" : "rz-y");
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", wide ? "vertical" : "horizontal");
  if (!handle.hasAttribute("tabindex")) handle.tabIndex = 0;
  if (own || !handle.title)
    handle.title = opts.title || "Drag to change the size · double-click for the usual size";
  if (own) {
    if (place === "after") target.after(handle);
    else target.append(handle); // the strip is placed against target (made `position: relative` once on the page)
  }

  const clamp = (px) => Math.min(max(), Math.max(min(), px));
  const self = {
    target,
    handle,
    seen: false,
    /** The remembered size again (or the usual one when it doesn't fit, or resizing is off here). */
    refresh() {
      // not on the page yet (the host puts it there after making it): measured once it is
      if (!target.isConnected) return void requestAnimationFrame(() => target.isConnected && self.refresh());
      if (own && place !== "after" && getComputedStyle(target).position === "static") target.style.position = "relative";
      const on = enabled();
      handle.hidden = !on && own;
      handle.classList.toggle("rz-off", !on);
      self.seen = true;
      live.add(self); // back on the page after the host took it away for a while
      const px = on ? opts.size?.get?.() : null;
      apply(px != null && Number.isFinite(px) && px >= min() && px <= max() ? px : null);
    },
    /** Forgets the remembered size: the usual one. */
    reset() {
      opts.size?.set?.(null);
      apply(null);
      opts.onDone?.(null);
    },
    destroy() {
      live.delete(self);
      handle.removeEventListener("pointerdown", down);
      handle.removeEventListener("dblclick", dbl);
      handle.removeEventListener("keydown", key);
      if (own) handle.remove();
    },
  };

  function down(e) {
    if (e.button !== 0 || !enabled() || opts.ignore?.(e)) return;
    e.preventDefault();
    const start = wide ? e.clientX : e.clientY;
    const from = measure();
    let px = from;
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    document.documentElement.classList.add(wide ? "rz-dragging-x" : "rz-dragging-y");
    const move = (ev) => {
      px = clamp(from + sign * ((wide ? ev.clientX : ev.clientY) - start));
      apply(px);
      opts.onResize?.(px);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      handle.classList.remove("dragging");
      document.documentElement.classList.remove("rz-dragging-x", "rz-dragging-y");
      if (Math.abs(px - from) < 1) return; // a click, not a drag
      opts.size?.set?.(Math.round(px));
      opts.onDone?.(Math.round(px));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  }
  function dbl(e) {
    if (!enabled() || opts.ignore?.(e)) return;
    e.preventDefault();
    self.reset();
  }
  // the keyboard: arrows make it 20 px bigger or smaller (Home: the usual size)
  function key(e) {
    if (e.target !== handle || !enabled()) return;
    const grow = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[e.key];
    if (e.key === "Home") {
      e.preventDefault();
      return self.reset();
    }
    if (!grow || (wide ? /Up|Down/ : /Left|Right/).test(e.key)) return;
    e.preventDefault();
    const px = clamp(measure() + grow * sign * 20);
    apply(px);
    opts.size?.set?.(Math.round(px));
    opts.onDone?.(Math.round(px));
  }
  handle.addEventListener("pointerdown", down);
  handle.addEventListener("dblclick", dbl);
  handle.addEventListener("keydown", key);
  live.add(self);
  self.refresh();
  return self;
}

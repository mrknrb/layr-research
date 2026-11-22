// The units a name can end in (rate_mM_per_min → rate [mM/min]): the same list as the backend's (lab/latex.py UNITS),
// so a column is labelled as the formulas label it. Used by the flow and the rounds tables.
export const UNITS = {
  mM: "mM", uM: "µM", nM: "nM", M: "M", mol: "mol", mmol: "mmol", umol: "µmol", nmol: "nmol",
  min: "min", s: "s", h: "h", ms: "ms", d: "d",
  L: "L", l: "L", mL: "mL", ml: "mL", uL: "µL", ul: "µL",
  g: "g", mg: "mg", ug: "µg", kg: "kg", gCDW: "gCDW", gcdw: "gCDW", gDW: "gDW",
  U: "U", pA: "pA", mAU: "mAU", pct: "%", percent: "%", nm: "nm", um: "µm", mm: "mm",
  degC: "°C", K: "K", rpm: "rpm", OD: "OD", Da: "Da", kDa: "kDa", bar: "bar",
};

/** A name's words and its unit, read from the end of the name: rate_mM_per_min → ["rate", "mM/min"]; growth_per_h →
 * ["growth", "1/h"]; [name, null] when it ends in no unit. */
export function splitUnit(name) {
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

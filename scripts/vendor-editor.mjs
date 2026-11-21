#!/usr/bin/env node
// Builds the Research plugin's code editor library: panels/vendor/cm6/cm6.js, one minified ES module
// with CodeMirror 6 (state, view, commands, language, Python, autocomplete, search, lint, merge) and the Lezer
// highlighter, plus a LICENSE file listing every bundled package with its version and licence. layr's panel CSP allows
// no CDN, so the panel loads this file; panels/editor.js is the plugin's own wrapper around it.
//
//   node scripts/vendor-research-editor.mjs          npm install the pinned versions into a temp folder, bundle
//   node scripts/vendor-research-editor.mjs --keep   keep the temp folder (its path is printed)
//
// Needs Node 22 and the network. Nothing is installed in the repository (no node_modules, no lock file).
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = path.join(root, "panels", "vendor", "cm6");
const keep = process.argv.includes("--keep");

// Pinned: change a version here and run the script again to update the bundle.
const PACKAGES = {
  "@codemirror/state": "6.7.6",
  "@codemirror/view": "6.43.15",
  "@codemirror/commands": "6.11.1",
  "@codemirror/language": "6.13.1",
  "@codemirror/lang-python": "6.2.1",
  "@codemirror/autocomplete": "6.20.4",
  "@codemirror/search": "6.7.2",
  "@codemirror/lint": "6.9.8",
  "@codemirror/merge": "6.12.3",
  "@lezer/highlight": "1.2.5",
  esbuild: "0.28.2",
};

// What panels/editor.js imports. `export *` of packages that share a name drops that name (ES modules' rule), so the
// few clashing ones are named explicitly.
const ENTRY = `
export * from "@codemirror/state";
export * from "@codemirror/view";
export * from "@codemirror/commands";
export * from "@codemirror/language";
export * from "@codemirror/search";
export * from "@codemirror/autocomplete";
export * from "@codemirror/lint";
export * from "@codemirror/merge";
export { python, pythonLanguage, localCompletionSource, globalCompletion } from "@codemirror/lang-python";
export { tags, Tag, tagHighlighter, classHighlighter, highlightCode, highlightTree, styleTags } from "@lezer/highlight";
`;

function run(cmd, args, cwd) {
  console.log(`> ${cmd} ${args.join(" ")}`);
  // npm is a .cmd on Windows: go through the shell there.
  execFileSync(cmd, args, { cwd, stdio: "inherit", shell: process.platform === "win32" });
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "layr-research-cm6-"));
try {
  fs.writeFileSync(path.join(tmp, "package.json"), JSON.stringify({ name: "cm6-bundle", private: true }, null, 2));
  run(
    "npm",
    ["install", "--no-audit", "--no-fund", "--save-exact", ...Object.entries(PACKAGES).map(([n, v]) => `${n}@${v}`)],
    tmp,
  );
  fs.writeFileSync(path.join(tmp, "entry.js"), ENTRY);
  const { build, stop } = await import(pathToFileURL(path.join(tmp, "node_modules", "esbuild", "lib", "main.js")).href);
  fs.mkdirSync(out, { recursive: true });
  const result = await build({
    entryPoints: [path.join(tmp, "entry.js")],
    absWorkingDir: tmp,
    bundle: true,
    format: "esm",
    minify: true,
    target: "es2022",
    legalComments: "none",
    outfile: path.join(out, "cm6.js"),
    banner: { js: "// CodeMirror 6 and Lezer (MIT), bundled by scripts/vendor-research-editor.mjs. Do not edit." },
    metafile: true,
    logLevel: "warning",
  });
  await stop(); // esbuild's service keeps its .exe open, and Windows then cannot delete the temp folder

  // The packages that ended up in the bundle (the pinned ones and what they pull in), each with its licence text.
  const used = new Set();
  for (const input of Object.keys(result.metafile.inputs)) {
    const m = input.replace(/\\/g, "/").match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
    if (m) used.add(m[1]);
  }
  const parts = [
    "The Research plugin's code editor (cm6.js) bundles these packages, unchanged, by",
    "scripts/vendor-research-editor.mjs. Their licences follow.",
    "",
  ];
  const texts = [];
  for (const name of [...used].sort()) {
    const dir = path.join(tmp, "node_modules", ...name.split("/"));
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
    parts.push(`- ${name} ${pkg.version} (${pkg.license})`);
    const file = fs.readdirSync(dir).find((f) => /^licen[cs]e/i.test(f));
    const text = file ? fs.readFileSync(path.join(dir, file), "utf8").trim() : pkg.license;
    texts.push(`==== ${name} ${pkg.version} ====\n\n${text}\n`);
  }
  fs.writeFileSync(path.join(out, "LICENSE"), [...parts, "", ...texts].join("\n").replace(/\r\n/g, "\n"));
  const bytes = fs.statSync(path.join(out, "cm6.js")).size;
  console.log(`cm6.js: ${(bytes / 1024).toFixed(0)} KB, ${used.size} packages`);
} finally {
  if (keep) console.log(`kept ${tmp}`);
  else fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}

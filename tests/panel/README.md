# Panel test bench

Opens the Research panels in headless Chromium against the plugin's real backend, without layr: `harness.py` stands
in for layr's core, `lib.mjs` drives it from Node scripts with Playwright, `smoke.e2e.mjs` is the first script.

## Run it

Git Bash on Windows, from the plugin folder:

```bash
cd "C:/Users/mrknr/AppData/Roaming/layr/packages/research/work-w_a55w88blrq"
export UV_PROJECT_ENVIRONMENT="C:/Users/mrknr/AppData/Roaming/layr/plugin-data/claude/work/w_a55w88blrq/venv-research"
export PYTHONDONTWRITEBYTECODE=1
node tests/panel/smoke.e2e.mjs
```

Screenshots go to `tests/panel/out/` (or `$SHOTS`), with the harness's stderr in `out/harness.log`. Playwright is not
in the plugin: it is loaded from `$PWTOOLS` (default `C:/Users/mrknr/pwtools`, a folder whose `node_modules` has
`playwright` and its Chromium). `UV_PROJECT_ENVIRONMENT` defaults to the venv above when it isn't set. The exit code
says pass (0) or fail (1); the last lines say how long it took and that every process it started is gone.

The harness alone (to look at the panel in your own browser, or to drive it with curl):

```bash
uv run python tests/panel/harness.py --port 8765 --no-stdin
```

It prints one JSON line when the panel can be opened and the backend answers:
`{"port", "url" (lab.html with its token), "urls": {"experiments", "main"}, "pid", "backendPid", "data"}`, and stops
(the backend too, by pid; then any process the backend started that is still there) on Ctrl+C, when its stdin closes
(leave out `--no-stdin`), or on `POST /_test/quit`; its last line is `{"stopped": true, "backendPid", "children",
"killed", "left"}`. Flags: `--data <dir>` (keep everything there; default a temporary folder deleted at the end,
`--keep` keeps it), `--layr-static <dir>` (the real `layr.js`/`layr.css`; default the copy in the scratch folder
`layr-static/`), `--answer-wait <s>` (how long a menu or dialog nobody answered waits, default 15).

## What the harness fakes

- **Pages**: `panels/` at `/` (so `lab.html?layr=<token>` loads its relative imports), the real SDK at
  `/_layr/layr.js` and `/_layr/layr.css`. Each HTML page gets `<script src="/_test/shell.js">` first in its head: it
  defines `window.__layrShell`, the shell bridge the SDK uses for what the app draws (see below).
- **The SDK's protocol**: JSON-RPC 2.0 over `ws://<host>/_layr/ws?token=<panel token>`. On connect the SDK
  subscribes its topics, then calls `layr.info` → `{plugin, panel, instance, title, state, params, theme, version}`;
  events are pushed as `{"method": "event", "params": {topic, data}}`. One token per manifest panel
  (`experiments` = lab.html, `main` = main.html); `panel.setState` is kept per panel and given back as
  `layr.info.state` on the next load.
- **Methods answered**: `tools.call` (`layr.tools.run/call`: forwarded to the backend with
  `_meta: {"layr/caller": {kind: "panel", plugin: "research"}}`; the raw MCP result goes back, `tools.run` unwraps it;
  `{plugin: "claude"}` answers per the connection state below), `tools.list`, `events.subscribe/unsubscribe/emit`,
  `panel.setState/setTitle/close`, `ui.notify`, `connections.get`, `links.open`, `panels.open`, `problems.report`,
  `shell.key`, `settings.get/set` (in memory; set pushes `research:settings`), `data.*`, `db.*` (in memory),
  `context.set`, `statusBar.set`, `files.info`, `disk.free`. Anything else answers `-32601 Unknown method` and is
  logged as `unknown` (the error collector fails on it).
- **The shell bridge** (`window.__layrShell.call`, which layr.js uses instead of the socket):
  `ui.contextMenu`, `files.open`, `files.saveAs` are answered from queues the test fills, `clipboard.writeText` is
  logged.
- **Files**: dialogs hand out file ids; `/_layr/files/<id>` reads (GET) and writes (PUT) them with the panel token
  (`X-Layr-Token` header or `?layr=`), in the harness's temp folder. `layr.files.current()` is empty unless a test
  pushes params with `file`.
- **The hub**: the backend gets `LAYR_HUB_URL=http://127.0.0.1:<port>/mcp` and a token; `/mcp` is a minimal MCP
  server (streamable HTTP, JSON answers) with the one tool the backend calls, `layr__events_emit {topic, data}`,
  which pushes the event to every socket subscribed to the topic (the panel redraws on `research:lab`). The same
  address serves `/_layr/files/` to the backend's `file_fetch`.
- **The backend**: `python backend/server.py` from the plugin folder (the venv's Python), MCP on stdio, with
  `LAYR_DATA_DIR=<data>/plugin`, `LAYR_PLUGIN_ID=research`; its stderr is `<data>/backend.log`.

## Control endpoints (plain HTTP JSON, for scripts)

| Endpoint | What |
|---|---|
| `POST /_test/tool {name, args, raw?, caller?}` | a backend tool's value (as `layr.tools.run` gives it; 400 `{error}` when it fails). Called as the user's panel unless `caller` says otherwise (e.g. `{"kind": "agent", "label": "x"}`) |
| `GET /_test/log?since=<seq>&kind=a,b` | `{entries: [{seq, t, kind, ...}], last}`; kinds: `tool` (name, ms, error), `event` (from the backend), `emit` (from a panel), `notify` (text, level), `title`, `link`, `clipboard`, `problem`, `unknown`, `shellUnknown`, `menu` (params.items, answer, picked), `menuMiss`, `open`, `saveAs`, `panelOpen`, `panelClose`, `fileRead`, `fileWrite`, `key` |
| `GET /_test/state` | each panel's state, params, title, sockets and topics; the menu/dialog waiting for an answer (`asking`); queued answers; theme; backend pid |
| `POST /_test/answer {kind, answer}` | answers the menu/dialog waiting now, else queues it for the next one. `kind: "menu"`: a label (exact, else the first containing it, case-insensitive, submenus searched too), `"Parent > Child"`, `{"id": "0.1"}`, or `null` (dismissed). `kind: "open"`: `{"path"}`, `{"name", "text"}`, `{"name", "base64"}` or `null`. `kind: "saveAs"`: `true` (the name the panel proposed), `{"name"}` or `null` |
| `POST /_test/params {params, panel?}` | as when a link opens the panel again: `layr:panel.params` to its sockets (`layr.panel.onParams`), and `layr.info.params` from then on |
| `POST /_test/panel-state {state, params?, panel?}` | sets what the next load gets as `layr.info.state` (null: a fresh panel) |
| `POST /_test/theme {mode}` | `layr:theme` to every socket and for the next loads (`"dark"` default, `"light"`) |
| `POST /_test/connections {claude, answer?}` | the claude connection: `"missing"` (default: Explain buttons hidden), `"ungranted"`, `"usable"` (its `ask` tool answers `answer`); pushes `layr:connections` |
| `POST /_test/emit {topic, data}` | pushes an event as if the backend had sent it |
| `POST /_test/file {name, text \| base64 \| path}` | registers a file → its ref (`{id, name, size, writable, exists, modified}`) |
| `GET /_test/files` | every file with its path on disk (what a save wrote) |
| `GET /_test/backend-log?chars=4000` | the end of the backend's stderr (tracebacks of the backend and its workers) |
| `GET /_test/pids` | `{pid, backendPid, children}` |
| `POST /_test/quit` | stops |

## Writing a script

```js
import { bench } from "./lib.mjs";

await bench("my script", async ({ h, open, shot, until, step }) => {
  const { page, errors } = await open();                  // lab.html; open({panel: "main"}) for Quick analysis
  await page.locator("h2", { hasText: "Experiments" }).waitFor({ timeout: 180_000 });
  const exp = await h.tool("lab_sample", { which: "survey" });   // prepare data without clicking
  await h.answer("menu", "A survey");                      // the next context menu picks the item with that label
  await page.getByRole("button", { name: /Try an example/ }).click();
  await until("the card", () => page.locator(".exp-card").count());
  await shot(page, "my-screen");
  await errors.check([/ResizeObserver/]);                  // fails on page errors not in the allow-list
});
```

`bench` starts the harness and Chromium, runs the function, and always stops both; it fails when a process is left.
The handle `h` has `tool`, `log(since, kinds)`, `state`, `answer`, `params`, `setPanelState`, `theme`,
`connections`, `emit`, `addFile`, `files`, `backendLog`, `req(path, body)` and the harness's `base`, `urls`, `pid`,
`backendPid`. When a script fails, `bench` saves `out/FAIL-<n>.png` of each page and prints its collected errors.
`collectErrors` counts uncaught exceptions, `console.error`, failed requests and HTTP errors ≥ 400, problems
reported, unknown layr methods, unmatched menu picks and error notifications (`ui.notify(…, "error")`).

### Notes for scripts

- **Context menus** don't exist in the page (the app's shell draws them): queue the pick with
  `h.answer("menu", "<label>")` *before* the click that opens it. With nothing queued the menu waits up to
  `--answer-wait` seconds: `h.state()` shows its items under `asking.menu`, and `h.answer` then picks. A menu that
  times out or is answered with `null` is dismissed, which the SDK reports as `null` to the panel.
- **File dialogs** the same way: `h.answer("open", {name: "a.csv", text: "x,y\n1,2\n"})` before clicking; after a
  save, `h.files()` gives the path of what the panel wrote.
- **Params** (a `layr://research/...` link opening the panel): `h.params({...})` while it is open, or
  `h.setPanelState(null)` + `h.params(...)` before `open()` for a fresh panel opened with them. The manifest has no
  `links` section yet, so the harness doesn't turn a `layr://` URL into params: a script gives the params object the
  link should deliver.
- **Events** from the backend arrive at the panel only while it is subscribed; `h.log(0, "event")` shows what the
  backend sent.
- The theme: `h.theme("light")`, then wait for `document.documentElement.dataset.theme === "light"`.

## Known limits

- Not the real app: menus and dialogs are not drawn (nothing to see in screenshots), the panel has no frame, tabs or
  other plugins, permissions are not checked (every tool is allowed), `links.open` and `panels.open` are only logged.
- `layr.info` and `connections.get` have the fields the Research panels read; the real core may give more.
- Settings, data and db live in memory for one harness run; the backend's data lives in the run's temp folder.
- One instance per panel id (one token each); two pages with the same token share state and events.
- The first backend start in a fresh venv can take minutes; `bench` waits up to 3.
- A backend that dies is not started again (layr would); calls then fail with `-32003`/`-32000`.

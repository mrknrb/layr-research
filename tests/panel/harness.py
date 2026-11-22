"""A stand-in for layr's core, so the Research panels can be opened in a browser and driven by test scripts.

One process serves the plugin's panels/ folder and layr's real panel SDK (/_layr/layr.js, /_layr/layr.css), speaks
the SDK's JSON-RPC over ws://<host>/_layr/ws, starts the plugin's own backend (backend/server.py, MCP on stdio) as
layr does, and is the hub the backend sends its events to (/mcp, the tool layr__events_emit). What only the app's
shell draws (context menus, file dialogs, the clipboard: window.__layrShell) is answered from queues a test fills
through the control endpoints under /_test/ (plain HTTP JSON; see README.md).

    uv run python tests/panel/harness.py [--port 0] [--data <dir>] [--layr-static <dir>]

prints one JSON line {"port", "url", "urls", "pid", "backendPid", "data"} once the panels can be opened and the
backend answers, and stops (the backend too, by its process id) on Ctrl+C, when stdin closes, or on POST /_test/quit.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ctypes
import itertools
import json
import mimetypes
import os
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

PLUGIN = Path(__file__).resolve().parents[2]
PANELS = PLUGIN / "panels"
MANIFEST = json.loads((PLUGIN / "layr.json").read_text("utf-8"))
PLUGIN_ID = MANIFEST["id"]
DEFAULT_STATIC = Path(os.environ.get("LAYR_STATIC") or
                      Path(os.environ.get("APPDATA", "")) / "layr/plugin-data/claude/work/w_a55w88blrq/layr-static")

# The SDK's error codes (layr.js › CODES)
UNKNOWN_METHOD, BAD_PARAMS, NOT_ALLOWED, NOT_FOUND, NOT_AVAILABLE, FAILED = -32601, -32602, -32001, -32002, -32003, -32000

# window.__layrShell: what the app's shell does for a panel (menus, dialogs, clipboard), here a call to the harness.
SHELL_JS = """(function () {
  var token = new URL(location.href).searchParams.get("layr") || sessionStorage.getItem("layr.token");
  window.__layrShell = {
    test: true,
    call: function (method, params) {
      return fetch("/_test/shell", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: token, method: method, params: params || {} }) })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (r.error) { var e = new Error(r.error.message); e.name = "LayrError"; e.code = r.error.code; throw e; }
          return r.result;
        });
    },
  };
})();
"""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def json(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------------------------------------ processes


def process_table() -> dict[int, int]:
    """Every running process as {pid: parent pid} (Windows: a Toolhelp snapshot; elsewhere /proc)."""
    out: dict[int, int] = {}
    if sys.platform == "win32":
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return out
        try:
            e = Entry()
            e.dwSize = ctypes.sizeof(Entry)
            ok = k32.Process32First(snap, ctypes.byref(e))
            while ok:
                out[e.th32ProcessID] = e.th32ParentProcessID
                ok = k32.Process32Next(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out
    for d in Path("/proc").glob("[0-9]*"):
        with contextlib.suppress(OSError, ValueError, IndexError):
            out[int(d.name)] = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
    return out


def descendants(pid: int) -> list[int]:
    """The processes started by `pid`, and by those, ... (not pid itself)."""
    table = process_table()
    found, todo = [], [pid]
    while todo:
        parent = todo.pop()
        for child, ppid in table.items():
            if ppid == parent and child not in found and child != pid:
                found.append(child)
                todo.append(child)
    return found


def alive(pid: int) -> bool:
    return pid in process_table()


def kill_pid(pid: int) -> bool:
    """Ends one process by its id (never a tree, never by name) → whether it was running."""
    if not alive(pid):
        return False
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    return True


# ------------------------------------------------------------------------------------------------ the backend


class Backend:
    """The plugin's backend, started as layr starts it: MCP over the stdin/stdout of `python backend/server.py`, its
    own data folder, the hub's address for events. Own stdio transport (not mcp's stdio_client) to keep the pid."""

    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self.env, self.log_path = env, log_path
        self.session: ClientSession | None = None
        self.proc: anyio.abc.Process | None = None
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self.stopping = asyncio.Event()
        self.stopped = asyncio.Event()
        self.children: list[int] = []
        self.report: dict[str, Any] = {}

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def run(self) -> None:
        entry = PLUGIN / MANIFEST["backend"]["entry"]
        log = open(self.log_path, "ab")
        try:
            self.proc = await anyio.open_process([sys.executable, str(entry)], stdin=-1, stdout=-1, stderr=log,
                                                 cwd=str(PLUGIN), env=self.env)
            read_send, read_recv = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            write_send, write_recv = anyio.create_memory_object_stream[SessionMessage](0)

            async def reader() -> None:
                buf = b""
                async with read_send:
                    async for chunk in self.proc.stdout:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if not line.strip():
                                continue
                            try:
                                msg = types.JSONRPCMessage.model_validate_json(line)
                            except Exception as e:  # noise on stdout: hand it to the session as an error
                                await read_send.send(e)
                                continue
                            await read_send.send(SessionMessage(msg))

            async def writer() -> None:
                async with write_recv:
                    async for sm in write_recv:
                        data = sm.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                        await self.proc.stdin.send(data.encode())

            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                tg.start_soon(writer)
                try:
                    async with ClientSession(read_recv, write_send) as session:
                        await session.initialize()
                        self.session = session
                        self.ready.set()
                        await self.stopping.wait()
                except Exception as e:
                    self.failed = describe(e)
                    self.ready.set()
                finally:
                    self.session = None
                    self.children = descendants(self.proc.pid)
                    with contextlib.suppress(Exception):
                        await self.proc.stdin.aclose()  # the server's stdio loop ends: it exits by itself
                    tg.cancel_scope.cancel()
        except Exception as e:
            self.failed = self.failed or describe(e)
            self.ready.set()
        finally:
            await self._end()
            log.close()
            self.stopped.set()

    async def _end(self) -> None:
        """Waits for the backend (and what it started) to exit; ends what is left after a few seconds, by pid."""
        if not self.proc:
            return
        pids = [self.proc.pid, *self.children]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(alive(p) for p in pids):
            await asyncio.sleep(0.2)
        killed = [p for p in pids if kill_pid(p)]
        with contextlib.suppress(Exception):
            with anyio.move_on_after(3):
                await self.proc.wait()
        self.report = {"backendPid": self.proc.pid, "children": self.children, "killed": killed,
                       "left": [p for p in pids if alive(p)]}

    async def call(self, name: str, args: dict, caller: dict | None) -> dict:
        if not self.ready.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.ready.wait(), 600)
        if self.failed or not self.session:
            raise RpcError(NOT_AVAILABLE, f"The plugin's backend isn't running ({self.failed or 'starting'})")
        meta = {"layr/caller": caller} if caller else None
        try:
            res = await self.session.call_tool(name, args or {}, meta=meta)
        except Exception as e:
            raise RpcError(FAILED, f"{type(e).__name__}: {e}") from e
        return res.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def list_tools(self) -> list[dict]:
        if not self.session:
            raise RpcError(NOT_AVAILABLE, "The plugin's backend isn't running")
        res = await self.session.list_tools()
        return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in res.tools]


def describe(e: BaseException) -> str:
    """An error in one line, the first inner one of an exception group."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


def tool_value(result: dict) -> Any:
    """The useful part of an MCP tool result, as layr.tools.run reads it (layr.js › toolValue)."""
    if result.get("isError"):
        raise RpcError(FAILED, text_of(result) or "The tool failed", result)
    sc = result.get("structuredContent")
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and list(sc) == ["result"] else sc
    text = text_of(result)
    if text is None:
        return result.get("content")
    try:
        return json.loads(text)
    except ValueError:
        return text


def text_of(result: dict) -> str | None:
    parts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    return "\n".join(parts) if parts else None


# ------------------------------------------------------------------------------------------------ the core


class Panel:
    """One panel instance (one token): its saved state, params and title, as layr keeps them."""

    def __init__(self, pid: str, page: str, title: str) -> None:
        self.id, self.page, self.title = pid, page, title
        self.token = secrets.token_hex(12)
        self.instance = f"{PLUGIN_ID}:{pid}:1"
        self.state: Any = None
        self.params: dict = {}


class Conn:
    def __init__(self, ws: WebSocket, panel: Panel) -> None:
        self.ws, self.panel = ws, panel
        self.topics: set[str] = set()
        self.lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        async with self.lock:
            with contextlib.suppress(Exception):
                await self.ws.send_text(json.dumps(msg))


class Core:
    def __init__(self, root: Path, static: Path, answer_wait: float) -> None:
        self.root, self.static, self.answer_wait = root, static, answer_wait
        self.hub_token = secrets.token_hex(12)
        self.panels = {p["id"]: Panel(p["id"], p["page"], p.get("title", p["id"])) for p in MANIFEST["panels"]}
        self.by_token = {p.token: p for p in self.panels.values()}
        self.conns: set[Conn] = set()
        self.log: list[dict] = []
        self.seq = itertools.count(1)
        self.theme: dict = {"mode": "dark"}
        self.settings: dict[str, Any] = {}
        self.data: dict[str, Any] = {}
        self.db: dict[str, dict[str, Any]] = {}
        self.claude = {"state": "missing", "answer": "An answer from Claude (test bench)."}
        self.queues: dict[str, list] = {"menu": [], "open": [], "saveAs": []}
        self.asking: dict[str, dict] = {}  # kind -> {params, future}: a menu or dialog waiting for its answer
        self.files: dict[str, dict] = {}
        self.files_dir = root / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.backend: Backend | None = None
        self.quit = asyncio.Event()

    # ---------- log

    def note(self, kind: str, **fields: Any) -> dict:
        entry = {"seq": next(self.seq), "t": now(), "kind": kind, **fields}
        self.log.append(entry)
        if kind in ("unknown", "problem", "shellUnknown"):
            print(f"[harness] {kind}: {json.dumps(fields)[:300]}", file=sys.stderr, flush=True)
        return entry

    # ---------- events

    async def emit(self, topic: str, data: Any, only: Panel | None = None, force: bool = False) -> int:
        """Pushes an event to the sockets subscribed to the topic (force: to every socket of `only`) → how many."""
        n = 0
        for c in list(self.conns):
            if only and c.panel is not only:
                continue
            if force or topic in c.topics:
                await c.send({"jsonrpc": "2.0", "method": "event", "params": {"topic": topic, "data": data}})
                n += 1
        return n

    # ---------- files

    def file_ref(self, fid: str) -> dict:
        f = self.files.get(fid)
        if not f:
            raise RpcError(NOT_FOUND, f"No file {fid}")
        p = Path(f["path"])
        exists = p.exists()
        st = p.stat() if exists else None
        return {"id": fid, "name": f["name"], "size": st.st_size if st else 0, "writable": f["writable"],
                "exists": exists, "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat() if st else None}

    def add_file(self, spec: dict, writable: bool = False) -> dict:
        """A file the 'user' gives the plugin: {path} (copied), {name, text} or {name, base64}; {name} alone makes a
        new empty slot (a save dialog's answer)."""
        fid = "f" + secrets.token_hex(6)
        name = spec.get("name") or (Path(spec["path"]).name if spec.get("path") else "file")
        folder = self.files_dir / fid
        folder.mkdir()
        path = folder / name
        if spec.get("path"):
            shutil.copyfile(spec["path"], path)
        elif "text" in spec:
            path.write_text(spec["text"], "utf-8", newline="")
        elif "base64" in spec:
            path.write_bytes(base64.b64decode(spec["base64"]))
        self.files[fid] = {"name": name, "path": str(path), "writable": writable or not path.exists()}
        return self.file_ref(fid)

    # ---------- menus and dialogs

    async def ask(self, kind: str, params: dict) -> tuple[Any, dict]:
        """The answer to a menu or dialog (and its log entry): the next queued one, else the one a test posts while
        it waits (it shows in GET /_test/state › asking), else null after answer_wait seconds (dismissed)."""
        entry = self.note(kind, params=params)
        if self.queues[kind]:
            answer = self.queues[kind].pop(0)
        else:
            fut = asyncio.get_running_loop().create_future()
            self.asking[kind] = {"params": params, "future": fut, "since": now()}
            try:
                answer = await asyncio.wait_for(fut, self.answer_wait)
            except TimeoutError:
                answer = None
                entry["timedOut"] = True
            finally:
                self.asking.pop(kind, None)
        entry["answer"] = answer
        return answer, entry

    def answer(self, kind: str, answer: Any) -> str:
        waiting = self.asking.get(kind)
        if waiting and not waiting["future"].done():
            waiting["future"].set_result(answer)
            return "answered"
        self.queues[kind].append(answer)
        return "queued"

    @staticmethod
    def pick(items: list[dict], want: Any) -> str | None:
        """The id of the menu item a test asked for: {"id": ...}, or a label (exact, else the first that contains it,
        ignoring case; "Parent > Child" for a submenu)."""
        if want is None:
            return None
        if isinstance(want, dict):
            if "id" in want:
                return want["id"]
            want = want.get("pick") or want.get("label")
            if want is None:
                return None
        want = str(want)
        path = [w.strip() for w in want.split(" > ")]

        def find(level: list[dict], label: str) -> dict | None:
            real = [i for i in level if not i.get("separator")]
            for i in real:
                if i.get("label") == label:
                    return i
            for i in real:
                if label.lower() in str(i.get("label", "")).lower():
                    return i
            return None

        def deep(level: list[dict], label: str) -> dict | None:
            hit = find(level, label)
            if hit:
                return hit
            for i in level:
                if i.get("items"):
                    hit = deep(i["items"], label)
                    if hit:
                        return hit
            return None

        if len(path) == 1:
            hit = deep(items, path[0])
        else:
            level, hit = items, None
            for part in path:
                hit = find(level, part)
                if not hit:
                    break
                level = hit.get("items") or []
        if not hit:
            raise RpcError(BAD_PARAMS, f"No menu item like {want!r}")
        if hit.get("disabled"):
            raise RpcError(BAD_PARAMS, f"The menu item {hit.get('label')!r} is disabled")
        return hit["id"]

    async def shell(self, panel: Panel, method: str, params: dict) -> Any:
        """window.__layrShell.call: what the app's shell draws or does for the panel."""
        if method == "ui.contextMenu":
            want, entry = await self.ask("menu", params)
            try:
                entry["picked"] = self.pick(params.get("items") or [], want)
            except RpcError as e:
                entry["error"] = e.message
                self.note("menuMiss", wanted=want, error=e.message)
                print(f"[harness] context menu: {e.message}", file=sys.stderr, flush=True)
                return None
            return entry["picked"]
        if method == "clipboard.writeText":
            self.note("clipboard", text=params.get("text", ""))
            return None
        if method in ("files.open", "files.saveAs"):
            kind = "open" if method == "files.open" else "saveAs"
            ans, _ = await self.ask(kind, params)
            if not ans:
                return None
            if kind == "saveAs":
                spec = {"name": params.get("name") or "file"} if ans is True else {"name": params.get("name"), **ans}
                return self.add_file(spec, writable=True)
            return self.add_file(ans)
        self.note("shellUnknown", method=method, params=params)
        raise RpcError(UNKNOWN_METHOD, f"Unknown shell method: {method}")

    # ---------- the panel's RPC

    def info(self, panel: Panel) -> dict:
        return {"plugin": PLUGIN_ID, "panel": panel.id, "instance": panel.instance, "title": panel.title,
                "state": panel.state, "params": panel.params, "theme": self.theme, "version": "0.1.0-test",
                "platform": sys.platform}

    def connections(self) -> dict:
        state = self.claude["state"]
        usable = state == "usable"
        return {"blocked": False, "reasons": [], "connections": [{
            "plugin": "claude", "required": False, "installed": state != "missing", "usable": usable,
            "state": "granted" if usable else state, "blocked": False,
            "groups": [{"id": "ask", "exists": state != "missing", "rule": "allow" if usable else "ask"}]}]}

    async def rpc(self, conn: Conn, method: str, params: dict) -> Any:
        panel = conn.panel
        p = params or {}
        if method == "layr.info":
            return self.info(panel)
        if method == "events.subscribe":
            conn.topics.add(str(p.get("topic")))
            return None
        if method == "events.unsubscribe":
            conn.topics.discard(str(p.get("topic")))
            return None
        if method == "events.emit":
            self.note("emit", topic=p.get("topic"), data=p.get("data"))
            await self.emit(str(p.get("topic")), p.get("data"))
            return None
        if method == "tools.call":
            return await self.tool(p.get("tool"), p.get("args") or {}, p.get("plugin"),
                                   {"kind": "panel", "plugin": PLUGIN_ID, "panel": panel.id})
        if method == "tools.list":
            return await self.backend.list_tools()
        if method == "panel.setState":
            panel.state = p.get("state")
            return None
        if method == "panel.setTitle":
            panel.title = str(p.get("title"))
            self.note("title", panel=panel.id, title=panel.title)
            return None
        if method == "panel.close":
            self.note("panelClose", panel=panel.id)
            return None
        if method == "ui.notify":
            self.note("notify", text=p.get("text"), level=p.get("kind") or "info")
            return None
        if method == "connections.get":
            return self.connections()
        if method == "links.open":
            self.note("link", url=p.get("url"))
            return None
        if method == "panels.open":
            self.note("panelOpen", **p)
            if p.get("plugin") not in (None, PLUGIN_ID):
                raise RpcError(NOT_FOUND, f"No plugin {p.get('plugin')} in the test bench")
            return {"instance": f"{PLUGIN_ID}:{p.get('panel')}:1"}
        if method == "problems.report":
            if not p.get("ok"):
                self.note("problem", **{k: p.get(k) for k in ("id", "title", "details", "source", "url")})
            return None
        if method == "shell.key":
            self.note("key", key=p.get("key"))
            return None
        if method == "settings.get":
            return self.settings.get(p["id"]) if p.get("id") else dict(self.settings)
        if method == "settings.set":
            if p.get("value") is None:
                self.settings.pop(p["id"], None)
            else:
                self.settings[p["id"]] = p["value"]
            await self.emit(f"{PLUGIN_ID}:settings", {"id": p["id"], "value": p.get("value"),
                                                       "values": dict(self.settings)}, force=True)
            return None
        if method == "data.get":
            return self.data.get(p.get("key"))
        if method == "data.set":
            self.data[p["key"]] = p.get("value")
            return None
        if method == "data.delete":
            self.data.pop(p.get("key"), None)
            return None
        if method == "data.list":
            prefix = p.get("prefix") or ""
            return [k for k in sorted(self.data) if k.startswith(prefix)]
        if method.startswith("db."):
            return self.db_call(method[3:], p)
        if method in ("context.set", "statusBar.set"):
            self.note(method, **p)
            return None
        if method == "files.info":
            return self.file_ref(p.get("id"))
        if method == "disk.free":
            usage = shutil.disk_usage(p.get("path") or self.root)
            return {"free": usage.free, "total": usage.total, "drive": Path(p.get("path") or self.root).drive}
        self.note("unknown", method=method, params=p)
        raise RpcError(UNKNOWN_METHOD, f"Unknown method: {method}")

    def db_call(self, op: str, p: dict) -> Any:
        coll = self.db.setdefault(p.get("collection") or "", {})
        if op == "insert":
            rid = "r" + secrets.token_hex(5)
            coll[rid] = {**(p.get("value") or {}), "id": rid}
            return coll[rid]
        if op == "update":
            if p.get("id") not in coll:
                raise RpcError(NOT_FOUND, f"No record {p.get('id')}")
            coll[p["id"]] = {**coll[p["id"]], **(p.get("value") or {}), "id": p["id"]}
            return coll[p["id"]]
        if op == "delete":
            return coll.pop(p.get("id"), None) is not None
        if op == "get":
            return coll.get(p.get("id"))
        if op == "list":
            return list(coll.values())
        self.note("unknown", method="db." + op, params=p)
        raise RpcError(UNKNOWN_METHOD, f"Unknown method: db.{op}")

    async def tool(self, name: str, args: dict, plugin: str | None, caller: dict) -> dict:
        started = time.monotonic()
        entry = self.note("tool", name=name, plugin=plugin or PLUGIN_ID)
        try:
            if plugin and plugin != PLUGIN_ID:
                if plugin == "claude" and self.claude["state"] == "usable":
                    text = self.claude["answer"]
                    entry["args"] = args
                    return {"content": [{"type": "text", "text": text}], "isError": False}
                if plugin == "claude" and self.claude["state"] != "missing":
                    raise RpcError(NOT_ALLOWED, "Research isn't allowed to use claude's tools")
                raise RpcError(NOT_FOUND, f"No plugin {plugin} (the test bench has only {PLUGIN_ID})")
            res = await self.backend.call(name, args, caller)
            if res.get("isError"):
                entry["error"] = (text_of(res) or "")[:500]
            return res
        except RpcError as e:
            entry["error"] = e.message
            raise
        finally:
            entry["ms"] = round((time.monotonic() - started) * 1000)


# ------------------------------------------------------------------------------------------------ HTTP


def build_app(core: Core) -> Starlette:
    async def ws_endpoint(ws: WebSocket) -> None:
        panel = core.by_token.get(ws.query_params.get("token") or "")
        if not panel:
            await ws.close(code=4401)
            return
        await ws.accept()
        conn = Conn(ws, panel)
        core.conns.add(conn)
        tasks: set[asyncio.Task] = set()

        async def handle(msg: dict) -> None:
            mid = msg.get("id")
            try:
                result = await core.rpc(conn, str(msg.get("method")), msg.get("params") or {})
                reply = {"jsonrpc": "2.0", "id": mid, "result": result}
            except RpcError as e:
                reply = {"jsonrpc": "2.0", "id": mid, "error": e.json()}
            except Exception as e:  # a bug in the harness: say so to the panel and on stderr
                print(f"[harness] {msg.get('method')} failed: {e!r}", file=sys.stderr, flush=True)
                reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": FAILED, "message": f"harness: {e!r}"}}
            if mid is not None:
                await conn.send(reply)

        try:
            while True:
                text = await ws.receive_text()
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                # subscriptions take effect in order; everything else runs side by side (tools can be slow)
                if msg.get("method") in ("events.subscribe", "events.unsubscribe", "layr.info"):
                    await handle(msg)
                else:
                    t = asyncio.create_task(handle(msg))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            core.conns.discard(conn)

    async def layr_static(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ("layr.js", "layr.css"):
            return Response("Not found", 404)
        path = core.static / name
        if not path.exists():
            return Response(f"{path} is missing (--layr-static)", 404)
        return Response(path.read_bytes(), media_type="text/javascript" if name.endswith(".js") else "text/css",
                        headers={"Cache-Control": "no-store"})

    async def shell_js(_: Request) -> Response:
        return Response(SHELL_JS, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    async def panel_file(request: Request) -> Response:
        rel = request.path_params.get("path") or "lab.html"
        path = (PANELS / rel).resolve()
        if not path.is_relative_to(PANELS) or not path.is_file():
            return Response("Not found", 404)
        body = path.read_bytes()
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".js", ".mjs"):
            media = "text/javascript"
        if path.suffix == ".html":  # the shell's bridge goes in before the page's own scripts
            body = body.replace(b"<head>", b'<head><script src="/_test/shell.js"></script>', 1)
        return Response(body, media_type=media, headers={"Cache-Control": "no-store"})

    def file_token_ok(request: Request) -> bool:
        tok = request.headers.get("x-layr-token") or request.query_params.get("layr")
        return tok in core.by_token

    async def layr_file(request: Request) -> Response:
        fid = request.path_params["fid"]
        if not file_token_ok(request):
            return Response("No panel token", 401)
        f = core.files.get(fid)
        if not f:
            return Response(f"No file {fid}", 404)
        path = Path(f["path"])
        if request.method == "PUT":
            if not f["writable"]:
                return Response("This file can't be written", 403)
            path.write_bytes(await request.body())
            core.note("fileWrite", id=fid, name=f["name"], size=path.stat().st_size)
            return JSONResponse(core.file_ref(fid))
        if not path.exists():
            return Response(f"{f['name']} doesn't exist yet", 404)
        core.note("fileRead", id=fid, name=f["name"])
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(path.read_bytes(), media_type=media)

    async def hub(request: Request) -> Response:
        """layr's hub as the backend sees it: MCP over streamable HTTP (JSON answers, no session), one tool."""
        if request.method != "POST":
            return Response(status_code=405)
        if request.headers.get("authorization") != f"Bearer {core.hub_token}":
            return Response("bad hub token", 401)
        body = await request.json()
        out = []
        for m in body if isinstance(body, list) else [body]:
            if "id" not in m:
                continue  # a notification
            method, p = m.get("method"), m.get("params") or {}
            result: Any = None
            error = None
            if method == "initialize":
                result = {"protocolVersion": p.get("protocolVersion", types.LATEST_PROTOCOL_VERSION),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "layr-test-hub", "version": "0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": "layr__events_emit", "description": "Sends an event to the panels.",
                                     "inputSchema": {"type": "object", "properties": {
                                         "topic": {"type": "string"}, "data": {}}, "required": ["topic"]}}]}
            elif method == "tools/call" and p.get("name") == "layr__events_emit":
                args = p.get("arguments") or {}
                topic = str(args.get("topic"))
                core.note("event", topic=topic, data=args.get("data"))
                n = await core.emit(topic, args.get("data"))
                result = {"content": [{"type": "text", "text": json.dumps({"ok": True, "delivered": n})}],
                          "isError": False}
            else:
                error = {"code": -32601, "message": f"Unknown hub method {method} {p.get('name') or ''}"}
            out.append({"jsonrpc": "2.0", "id": m["id"], **({"error": error} if error else {"result": result})})
        if not out:
            return Response(status_code=202)
        return JSONResponse(out if isinstance(body, list) else out[0])

    # ---------- control endpoints for test scripts

    async def t_shell(request: Request) -> Response:
        body = await request.json()
        panel = core.by_token.get(body.get("token") or "")
        if not panel:
            return JSONResponse({"error": {"code": -32098, "message": "No panel token"}})
        try:
            return JSONResponse({"result": await core.shell(panel, body.get("method"), body.get("params") or {})})
        except RpcError as e:
            return JSONResponse({"error": e.json()})

    async def t_log(request: Request) -> Response:
        since = int(request.query_params.get("since") or 0)
        kind = request.query_params.get("kind")
        entries = [e for e in core.log if e["seq"] > since and (not kind or e["kind"] in kind.split(","))]
        return JSONResponse({"entries": entries, "last": core.log[-1]["seq"] if core.log else 0})

    async def t_state(_: Request) -> Response:
        return JSONResponse({
            "panels": {p.id: {"title": p.title, "state": p.state, "params": p.params, "token": p.token,
                              "connected": sum(1 for c in core.conns if c.panel is p),
                              "topics": sorted({t for c in core.conns if c.panel is p for t in c.topics})}
                       for p in core.panels.values()},
            "asking": {k: {"params": v["params"], "since": v["since"]} for k, v in core.asking.items()},
            "queued": core.queues, "theme": core.theme, "claude": core.claude, "settings": core.settings,
            "backend": {"pid": core.backend.pid, "ready": core.backend.ready.is_set(), "failed": core.backend.failed},
        })

    async def t_tool(request: Request) -> Response:
        body = await request.json()
        caller = body.get("caller") or {"kind": "panel", "plugin": PLUGIN_ID, "panel": "experiments"}
        try:
            res = await core.tool(body["name"], body.get("args") or {}, None, caller)
            if body.get("raw"):
                return JSONResponse(res)
            return JSONResponse(tool_value(res))
        except RpcError as e:
            return JSONResponse({"error": e.message, "code": e.code}, status_code=400)

    async def t_answer(request: Request) -> Response:
        body = await request.json()
        kind = body.get("kind") or "menu"
        if kind not in core.queues:
            return JSONResponse({"error": f"kind is one of {sorted(core.queues)}"}, status_code=400)
        return JSONResponse({"ok": True, "how": core.answer(kind, body.get("answer"))})

    def panel_of(body: dict) -> Panel:
        return core.panels[body.get("panel") or "experiments"]

    async def t_params(request: Request) -> Response:
        body = await request.json()
        panel = panel_of(body)
        panel.params = body.get("params") or {}
        n = await core.emit("layr:panel.params", {"params": panel.params}, only=panel, force=True)
        return JSONResponse({"ok": True, "delivered": n})

    async def t_panel_state(request: Request) -> Response:
        body = await request.json()
        panel = panel_of(body)
        panel.state = body.get("state")
        if "params" in body:
            panel.params = body.get("params") or {}
        return JSONResponse({"ok": True})

    async def t_theme(request: Request) -> Response:
        body = await request.json()
        core.theme = {**body, "mode": "light" if body.get("mode") == "light" else "dark"}
        n = await core.emit("layr:theme", core.theme, force=True)
        return JSONResponse({"ok": True, "delivered": n})

    async def t_connections(request: Request) -> Response:
        body = await request.json()
        if body.get("claude") not in (None, "missing", "ungranted", "usable"):
            return JSONResponse({"error": "claude is missing, ungranted or usable"}, status_code=400)
        if body.get("claude"):
            core.claude["state"] = body["claude"]
        if body.get("answer") is not None:
            core.claude["answer"] = body["answer"]
        n = await core.emit("layr:connections", core.connections(), force=False)
        return JSONResponse({"ok": True, "delivered": n, **core.connections()})

    async def t_emit(request: Request) -> Response:
        body = await request.json()
        return JSONResponse({"delivered": await core.emit(body["topic"], body.get("data"))})

    async def t_file(request: Request) -> Response:
        body = await request.json()
        return JSONResponse(core.add_file(body, writable=bool(body.get("writable"))))

    async def t_files(_: Request) -> Response:
        return JSONResponse([{**core.file_ref(fid), "path": f["path"]} for fid, f in core.files.items()])

    async def t_pids(_: Request) -> Response:
        b = core.backend.pid
        return JSONResponse({"pid": os.getpid(), "backendPid": b, "children": descendants(b) if b else []})

    async def t_backend_log(request: Request) -> Response:
        chars = int(request.query_params.get("chars") or 4000)
        text = core.backend.log_path.read_text("utf-8", errors="replace") if core.backend.log_path.exists() else ""
        return Response(text[-chars:], media_type="text/plain; charset=utf-8")

    async def t_quit(_: Request) -> Response:
        core.quit.set()
        return JSONResponse({"ok": True})

    async def root(_: Request) -> Response:
        return Response(status_code=307, headers={"location": f"/lab.html?layr={core.panels['experiments'].token}"})

    return Starlette(routes=[
        WebSocketRoute("/_layr/ws", ws_endpoint),
        Route("/_layr/files/{fid}", layr_file, methods=["GET", "PUT"]),
        Route("/_layr/{name}", layr_static),
        Route("/mcp", hub, methods=["GET", "POST", "DELETE"]),
        Route("/_test/shell.js", shell_js),
        Route("/_test/shell", t_shell, methods=["POST"]),
        Route("/_test/log", t_log),
        Route("/_test/state", t_state),
        Route("/_test/tool", t_tool, methods=["POST"]),
        Route("/_test/answer", t_answer, methods=["POST"]),
        Route("/_test/params", t_params, methods=["POST"]),
        Route("/_test/panel-state", t_panel_state, methods=["POST"]),
        Route("/_test/theme", t_theme, methods=["POST"]),
        Route("/_test/connections", t_connections, methods=["POST"]),
        Route("/_test/emit", t_emit, methods=["POST"]),
        Route("/_test/file", t_file, methods=["POST"]),
        Route("/_test/files", t_files),
        Route("/_test/pids", t_pids),
        Route("/_test/backend-log", t_backend_log),
        Route("/_test/quit", t_quit, methods=["POST"]),
        Route("/", root),
        Route("/{path:path}", panel_file),
    ])


class Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # the harness handles Ctrl+C itself, so the backend is always stopped first
        yield


async def main(args: argparse.Namespace) -> int:
    static = Path(args.layr_static)
    for name in ("layr.js", "layr.css"):
        if not (static / name).exists():
            print(json.dumps({"error": f"{static / name} is missing: pass --layr-static <dir>"}), flush=True)
            return 2
    temp = not args.data
    root = Path(args.data or tempfile.mkdtemp(prefix="research-panel-")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    core = Core(root, static, args.answer_wait)

    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(64)
    port = sock.getsockname()[1]

    env = {**os.environ, "LAYR_DATA_DIR": str(root / "plugin"), "LAYR_PLUGIN_ID": PLUGIN_ID,
           "LAYR_HUB_URL": f"http://127.0.0.1:{port}/mcp", "LAYR_HUB_TOKEN": core.hub_token,
           "PYTHONIOENCODING": "utf-8"}
    core.backend = Backend(env, root / "backend.log")

    loop = asyncio.get_running_loop()
    stop = core.quit.set
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop))
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(stop))
    if not args.no_stdin:
        def watch_stdin() -> None:
            with contextlib.suppress(Exception):  # os.read: no buffered reader whose lock blocks the exit
                while os.read(sys.stdin.fileno(), 4096):
                    pass
            loop.call_soon_threadsafe(stop)

        threading.Thread(target=watch_stdin, daemon=True).start()

    server = Server(uvicorn.Config(build_app(core), log_level="warning", access_log=False, lifespan="off",
                                   ws_max_size=64 * 1024 * 1024))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    backend_task = asyncio.create_task(core.backend.run())
    code = 0
    try:
        while not server.started and not serving.done():
            await asyncio.sleep(0.05)
        waiting = asyncio.create_task(core.backend.ready.wait())
        quitting = asyncio.create_task(core.quit.wait())
        await asyncio.wait([waiting, quitting], timeout=args.backend_wait, return_when=asyncio.FIRST_COMPLETED)
        waiting.cancel()
        quitting.cancel()
        if core.quit.is_set():
            code = 1
        elif not core.backend.ready.is_set() or core.backend.failed:
            log = (root / "backend.log").read_text("utf-8", errors="replace")[-3000:]
            why = core.backend.failed or "timed out"
            print(json.dumps({"error": f"the backend didn't start: {why}\n{log}"}), flush=True)
            code = 1
        else:
            base = f"http://127.0.0.1:{port}"
            urls = {p.id: f"{base}/{Path(p.page).relative_to('panels').as_posix()}?layr={p.token}"
                    for p in core.panels.values()}
            print(json.dumps({"port": port, "url": urls["experiments"], "urls": urls, "pid": os.getpid(),
                              "backendPid": core.backend.pid, "data": str(root)}), flush=True)
            await core.quit.wait()
    finally:
        stopping = time.monotonic()
        core.backend.stopping.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(core.backend.stopped.wait(), 20)
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(serving, 5)
        backend_task.cancel()
        if temp and not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        print(json.dumps({"stopped": True, "stopMs": round((time.monotonic() - stopping) * 1000),
                          **core.backend.report}), flush=True)
    return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=0, help="0: a free port")
    ap.add_argument("--data", help="a folder for everything (the backend's LAYR_DATA_DIR is <data>/plugin); kept. "
                                   "Default: a fresh temporary folder, deleted at the end")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    ap.add_argument("--layr-static", default=str(DEFAULT_STATIC), help="the folder with the real layr.js/layr.css")
    ap.add_argument("--answer-wait", type=float, default=15.0,
                    help="seconds a menu or dialog with no queued answer waits for one before it is dismissed")
    ap.add_argument("--backend-wait", type=float, default=600.0, help="seconds to wait for the backend to start")
    ap.add_argument("--no-stdin", action="store_true", help="don't stop when stdin closes")
    sys.exit(asyncio.run(main(ap.parse_args())))

"""Sending the plugin's events (research:lab, research:result) from anywhere: the server's event loop, or a worker
thread (a comparison after a method change, a run's progress).

The hub is reached with an async MCP client, so sending needs the server's event loop. A call from the loop's own
thread queues the event there; a call from another thread hands it to the loop with `call_soon_threadsafe` (the loop
is the one the server runs on: `use_loop` at start, or the first call made from it). Best effort: without a loop or
a hub, nothing is sent.

Events are sent one at a time, in order, by one task that lives as long as the loop (so `send` can keep one
connection to the hub open: opening one per event costs a quarter of a second of the loop's time, during which no
tool answers). A run's progress ("calculating step x") that a newer event of the same run already follows is
skipped when the hub is slower than the run.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any


class Emitter:
    def __init__(self, send: Callable[[dict[str, Any], str], Awaitable[None]], enabled: Callable[[], bool]):
        self.send = send
        self.enabled = enabled
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: deque[tuple[dict[str, Any], str]] = deque()
        self.wake: asyncio.Event | None = None
        self.task: asyncio.Task | None = None

    def use_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def emit(self, data: dict[str, Any], topic: str) -> None:
        if not self.enabled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self.loop = self.loop or loop
            self._start(data, topic)
            return
        target = self.loop
        if target is None or target.is_closed():
            return
        try:
            target.call_soon_threadsafe(self._start, data, topic)
        except RuntimeError:  # the loop closed meanwhile (the server is stopping)
            pass

    def _start(self, data: dict[str, Any], topic: str) -> None:
        """On the loop: queue the event, and make sure the sending task runs."""
        self.queue.append((data, topic))
        if self.task is None or self.task.done():
            self.wake = asyncio.Event()
            self.task = asyncio.ensure_future(self._sender())
        self.wake.set()

    async def _sender(self) -> None:
        while True:
            if not self.queue:
                self.wake.clear()
                await self.wake.wait()
                continue
            data, topic = self.queue.popleft()
            if _progress(data) and any(t == topic and d.get("run") == data.get("run") for d, t in self.queue):
                continue  # a newer event of the same run is waiting: this one is out of date
            try:
                await self.send(data, topic)
            except Exception:  # noqa: S110  (best effort: `send` says why itself)
                pass


def _progress(data: dict[str, Any]) -> bool:
    return data.get("what") == "run" and data.get("status") == "running" and not data.get("finished") \
        and data.get("run") is not None

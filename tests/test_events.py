"""Events reach the hub from the server's event loop and from worker threads (a comparison after a method change
finishes in a thread). Run from plugins/research:  uv run pytest tests/test_events.py -q
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lab.events import Emitter  # noqa: E402


async def test_events_from_the_loop_and_from_threads_are_sent():
    sent: list[tuple[str, dict]] = []

    async def send(data: dict, topic: str) -> None:
        sent.append((topic, data))

    events = Emitter(send, lambda: True)
    events.use_loop(asyncio.get_running_loop())
    events.emit({"what": "comparison", "status": "running"}, "research:lab")  # from the loop, as a tool does
    worker = threading.Thread(target=events.emit, args=({"what": "comparison", "status": "done"}, "research:lab"))
    worker.start()  # from a thread, as start_comparison's work() does
    worker.join()
    for _ in range(100):
        if len(sent) == 2:
            break
        await asyncio.sleep(0.01)
    assert sent == [("research:lab", {"what": "comparison", "status": "running"}),
                    ("research:lab", {"what": "comparison", "status": "done"})]


async def test_the_loop_is_learned_from_the_first_call_and_nothing_is_sent_without_a_hub():
    sent: list[str] = []

    async def send(data: dict, topic: str) -> None:
        sent.append(data["n"])

    events = Emitter(send, lambda: True)
    early = threading.Thread(target=events.emit, args=({"n": 0}, "t"))
    early.start()  # no loop known yet: dropped, no error
    early.join()
    events.emit({"n": 1}, "t")  # from the loop: learns it
    t = threading.Thread(target=events.emit, args=({"n": 2}, "t"))
    t.start()
    t.join()
    for _ in range(100):
        if len(sent) == 2:
            break
        await asyncio.sleep(0.01)
    assert sent == [1, 2]
    off = Emitter(send, lambda: False)
    off.emit({"n": 3}, "t")
    await asyncio.sleep(0.02)
    assert sent == [1, 2]

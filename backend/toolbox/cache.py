"""Remembering slow results on disk, so a rerun takes a fraction of the time.

Reading a GC sequence (a GAML file of 100 MB) and integrating every peak are the slow parts of an analysis. Their
results depend only on their inputs, so they are kept: in memory while the calculation process runs, and on disk
(in the folder given to `use`) for the next start. A result is found again by a fingerprint of its inputs; different
inputs give a different fingerprint, so a remembered result is never one for other inputs.

Without `use` (a method run outside layr) results are remembered in memory only.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import threading
from pathlib import Path
from typing import Any
from collections.abc import Callable

_lock = threading.RLock()
_folder: Path | None = None
_big: dict[str, Any] = {}  # name -> object; at most BIG_IN_MEMORY of them (GAML sequences are large)
_entries: dict[str, dict[str, Any]] = {}  # namespace -> {key: value}
_changed: set[str] = set()
BIG_IN_MEMORY = 2


def use(folder: str | Path | None) -> None:
    """Remember results in this folder (made when missing); None: in memory only."""
    global _folder
    with _lock:
        _folder = Path(folder) if folder else None
        if _folder:
            _folder.mkdir(parents=True, exist_ok=True)


def fingerprint(*parts: Any) -> str:
    """A short text that is the same for the same inputs and different for different ones."""
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:24]


def file_fingerprint(path: str | Path) -> str:
    """A fingerprint of a file: its full path, size and last change (raw files in layr's store never change)."""
    p = Path(path).resolve()
    st = p.stat()
    return fingerprint(str(p), st.st_size, st.st_mtime_ns)


def _write(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    with open(path, "rb") as f:
        return pickle.load(f)


def remember_big(name: str, compute: Callable[[], Any]) -> Any:
    """One large result (a parsed GAML file), by name: from memory, from disk, or computed and kept."""
    with _lock:
        if name in _big:
            return _big[name]
        path = _folder / f"{name}.pkl" if _folder else None
        value = None
        if path and path.exists():
            try:
                value = _read(path)
            except Exception:  # a cache file from an older version or cut short: compute again
                value = None
        if value is None:
            value = compute()
            if path:
                _write(path, value)
        while len(_big) >= BIG_IN_MEMORY:
            _big.pop(next(iter(_big)))
        _big[name] = value
        return value


def _namespace(namespace: str) -> dict[str, Any]:
    if namespace not in _entries:
        path = _folder / f"{namespace}.pkl" if _folder else None
        data: dict[str, Any] = {}
        if path and path.exists():
            try:
                data = _read(path)
            except Exception:
                data = {}
        _entries[namespace] = data
        if len(_entries) > 8:  # keep a few sequences' peaks in memory
            oldest = next(iter(_entries))
            if oldest != namespace and oldest not in _changed:
                _entries.pop(oldest)
    return _entries[namespace]


def remember(namespace: str, key: str, compute: Callable[[], Any]) -> Any:
    """One of many small results (a peak), by namespace (the sequence) and key (its inputs' fingerprint)."""
    with _lock:
        data = _namespace(namespace)
        if key in data:
            return data[key]
    value = compute()
    with _lock:
        _namespace(namespace)[key] = value
        _changed.add(namespace)
    return value


def save() -> None:
    """Write the small results remembered since the last save to disk (after a run)."""
    with _lock:
        if _folder:
            for namespace in list(_changed):
                if namespace in _entries:
                    _write(_folder / f"{namespace}.pkl", _entries[namespace])
        _changed.clear()


def forget() -> None:
    """Forget everything kept in memory (the files on disk stay)."""
    with _lock:
        _big.clear()
        _entries.clear()
        _changed.clear()

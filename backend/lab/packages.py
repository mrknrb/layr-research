"""Extra Python packages a method lists in REQUIREMENTS (lab/pages.py): installed once per set of requirements into
<store>/packages/<hash>/ (pip's --target layout) and put at the end of the calculation process's path for the runs
of that method. The plugin's own packages (pandas, numpy, SciPy, ...) are given to the installer as constraints, so an
extra package gets versions that work with them, and because they come first on the path they are never replaced.

Installing uses uv (which layr installs Python with) or else pip, and needs the internet once per set.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from importlib import metadata
from pathlib import Path

from .errors import LabError

ROOT: Path | None = None  # <store>/packages, set when the tools are registered
_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def configure(store_root: Path) -> None:
    global ROOT
    ROOT = Path(store_root) / "packages"


def _key(reqs: list[str]) -> str:
    text = "\n".join(sorted({r.strip().lower() for r in reqs})) + f"\npy{sys.version_info[0]}.{sys.version_info[1]}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def folder(reqs: list[str]) -> Path | None:
    return ROOT / _key(reqs) if ROOT is not None and reqs else None


def ready(reqs: list[str]) -> Path | None:
    """The folder of this set of requirements when it is installed."""
    f = folder(reqs)
    return f if f is not None and (f / ".done.json").exists() else None


def provides(reqs: list[str], module: str) -> bool | None:
    """Whether the installed packages of `reqs` have `module` (None: not installed yet)."""
    f = ready(reqs)
    if f is None:
        return None
    try:
        return importlib.machinery.PathFinder.find_spec(module.split(".")[0], [str(f)]) is not None
    except (ImportError, ValueError):
        return False


def _installer() -> list[str]:
    candidates = [shutil.which("uv")]
    exe_dir = Path(sys.executable).parent
    candidates += [str(exe_dir / n) for n in ("uv.exe", "uv")]
    for c in candidates:
        if c and Path(c).exists():
            return [c, "pip", "install", "--python", sys.executable, "--quiet"]
    try:
        import pip  # noqa: F401

        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
    except ImportError:
        raise LabError("Installing the method's packages needs uv or pip, and neither was found next to the plugin's "
                       "Python.") from None


def _constraints() -> str:
    seen = {}
    for d in metadata.distributions():
        name = (d.metadata.get("Name") or "").strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = f"{name}=={d.version}"
    return "\n".join(sorted(seen.values())) + "\n"


def install(reqs: list[str], progress=None) -> Path:
    """Install the set (once; later calls wait for the first and return its folder). Raises LabError in plain words."""
    from .pages import check_requirements

    reqs = check_requirements(reqs)
    dest = folder(reqs)
    if dest is None:
        raise LabError("No place to install packages yet.")
    with _GUARD:
        lock = _LOCKS.setdefault(dest.name, threading.Lock())
    with lock:
        if (dest / ".done.json").exists():
            return dest
        if progress:
            progress(f"Installing {', '.join(reqs)} (once)…")
        ROOT.mkdir(parents=True, exist_ok=True)
        tmp = ROOT / f"{dest.name}.part"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        cons = ROOT / f"{dest.name}.constraints.txt"
        cons.write_text(_constraints(), "utf-8")
        cmd = [*_installer(), "--target", str(tmp), "--constraint", str(cons), *reqs]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        t0 = time.time()
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=flags,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp, ignore_errors=True)
            raise LabError(f"Installing {', '.join(reqs)} took longer than 15 minutes and was stopped.") from None
        finally:
            cons.unlink(missing_ok=True)
        if done.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            said = (done.stderr or done.stdout or "").strip().splitlines()[-6:]
            raise LabError(f"Couldn't install {', '.join(reqs)}: is the name right, and is the internet on? The "
                           f"installer said: {' '.join(s.strip() for s in said)[:600]}")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        (dest / ".done.json").write_text(json.dumps({"requirements": reqs, "seconds": round(time.time() - t0, 1),
                                                     "python": sys.version.split()[0]}), "utf-8")
        return dest


def install_later(reqs: list[str]) -> None:
    """Start installing in the background (after a save), so the first run doesn't wait; failures show at the run."""
    if not reqs or ready(reqs) is not None:
        return

    def work() -> None:
        try:
            install(reqs)
        except Exception as e:  # reported again, in plain words, when the method runs
            print(f"couldn't install {reqs}: {e}", file=sys.stderr)

    threading.Thread(target=work, daemon=True).start()
